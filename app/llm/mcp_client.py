"""HTTP client for Model Context Protocol (MCP) JSON-RPC 2.0 communication."""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class McpException(Exception):
    """Failure communicating with an MCP server."""


class McpSessionExpired(McpException):
    """The server no longer recognises our session id.

    Raised when the MCP server was restarted under a running bot: the session we
    hold is gone, and every tool call fails until a new one is negotiated.
    """


#: What a server says when it no longer knows us. mcp-remnawave answers a
#: restarted-out-from-under-us request with 400 "Bad Request: Server not
#: initialized" rather than anything mentioning the session, so matching on the
#: word "session" alone missed the one case this all exists for. "already
#: initialized" — the opposite problem, seen during the handshake — does not
#: contain "not initialized" and is handled in _initialize_session.
_EXPIRED_SESSION_MARKERS: tuple[str, ...] = ("session", "not initialized")


def looks_like_expired_session(response: httpx.Response) -> bool:
    """Whether this error response means "your session is gone", not "bad request"."""
    if response.status_code == 404:
        return True
    if response.status_code in (400, 401):
        body = response.text.lower()
        return any(marker in body for marker in _EXPIRED_SESSION_MARKERS)
    return False


@dataclass(frozen=True)
class McpTool:
    """Representation of an MCP tool descriptor."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.input_schema is None:
            object.__setattr__(self, "input_schema", {})


@runtime_checkable
class AdminNotifier(Protocol):
    """The half of AdminNotifier this client uses."""

    async def notify_error(
        self,
        context: str,
        user_id: int | None = None,
        error: Exception | None = None,
    ) -> None:
        """Tell the support group that something failed."""
        ...


@runtime_checkable
class McpClientInterface(Protocol):
    """Interface for MCP clients."""

    def list_tools(self) -> list[McpTool]:
        """Return cached list of available MCP tools."""
        ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool with given arguments and return JSON string result."""
        ...


def extract_json_from_sse(response_body: str) -> str:
    """Extract first JSON line from SSE payload or return response body as-is."""
    for line in response_body.split("\n"):
        if line.startswith("data: "):
            return line[6:]
    return response_body


class HttpMcpClient(McpClientInterface):
    """JSON-RPC 2.0 client for a single MCP server over HTTP.

    One instance talks to exactly one server: ``server_name`` names it in every
    log line and admin alert (so an operator can tell which MCP is down) and is
    sent to the server in the ``clientInfo`` handshake. The URL is supplied by
    the caller, never derived from settings, so the bot can hold independent
    clients for several MCP servers without one silently borrowing another's
    endpoint.
    """

    PROTOCOL_VERSION = "2025-11-25"
    SESSION_HEADER = "Mcp-Session-Id"
    PROTOCOL_HEADER = "Mcp-Protocol-Version"
    REQUEST_TIMEOUT = 30.0
    JSONRPC_VERSION = "2.0"

    def __init__(
        self,
        server_name: str,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
        admin_notifier: AdminNotifier | None = None,
    ) -> None:
        self.server_name = server_name
        self.base_url = base_url.rstrip("/")
        self.admin_notifier = admin_notifier
        self._custom_client = http_client is not None
        self._http_client = http_client
        self._request_id = 0
        self._session_id: str | None = None
        self._protocol_version: str | None = None
        self._initialized = False
        self._cached_tools: list[McpTool] = []
        # Serialises recovery so a burst of tool calls negotiates one session
        # rather than one each; the counter tells a caller whether the session
        # it failed on has already been replaced. Both stay per-instance: a
        # burst of calls to one MCP never blocks another client's recovery.
        self._session_lock = asyncio.Lock()
        self._session_generation = 0

    @property
    def _label(self) -> str:
        """Log/alert prefix that names this MCP server."""
        return f"MCP[{self.server_name}]"

    @property
    def initialized(self) -> bool:
        """Whether client has completed initialize handshake."""
        return self._initialized

    @initialized.setter
    def initialized(self, value: bool) -> None:
        self._initialized = value

    @property
    def session_id(self) -> str | None:
        """Active MCP session identifier."""
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        self._session_id = value

    @property
    def protocol_version(self) -> str | None:
        """Protocol version negotiated with the active MCP session."""
        return self._protocol_version

    def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.REQUEST_TIMEOUT),
            )
        return self._http_client

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """POST a JSON-RPC envelope to the MCP endpoint.

        The absolute URL and the protocol headers are supplied per request rather
        than baked into the client: an injected client is shared with the rest of
        the application and carries neither.
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers[self.SESSION_HEADER] = self._session_id
        if self._protocol_version:
            headers[self.PROTOCOL_HEADER] = self._protocol_version
        return await self._get_client().post(self.base_url, json=payload, headers=headers)

    def list_tools(self) -> list[McpTool]:
        """Return cached list of available tools."""
        return list(self._cached_tools)

    async def init(self) -> bool:
        """Perform JSON-RPC initialize handshake and load tool definitions."""
        try:
            has_session = await self._initialize_session()
            if not has_session:
                logger.error(
                    "%s initialization at %s did not yield a usable session; "
                    "bot will run without tools from this server",
                    self._label,
                    self.base_url,
                )
                return False
            await self._load_tools()
            self._initialized = True
            logger.info(
                "%s HTTP client initialized with %d tools at %s",
                self._label,
                len(self._cached_tools),
                self.base_url,
            )
            return True
        except Exception as e:
            logger.error(
                "%s initialization failed — bot will run without tools from %s: %s",
                self._label,
                self.base_url,
                e,
                exc_info=True,
            )
            await self._notify_admins(f"{self._label} init failed for {self.base_url}", e)
            return False

    async def _notify_admins(self, context: str, error: Exception) -> None:
        """Forward a failure to the support group, tolerating a missing notifier."""
        if self.admin_notifier is None:
            return
        try:
            await self.admin_notifier.notify_error(context, error=error)
        except Exception as e:
            logger.warning("Failed to notify admins about %s: %s", context, e)

    async def _initialize_session(self) -> bool:
        self._request_id += 1
        req: dict[str, Any] = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": self._request_id,
            "method": "initialize",
            "params": {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "vpn-support-bot",
                    "version": "1.0.0",
                    "instance": uuid.uuid4().hex[:8],
                    "server": self.server_name,
                },
            },
        }

        logger.debug("%s initialize request [%d]", self._label, self._request_id)

        response = await self._post(req)

        # Check for session ID in response headers (case-insensitive)
        session_header = response.headers.get(self.SESSION_HEADER)
        if session_header:
            self._session_id = session_header
            logger.debug("%s session established", self._label)

        if response.is_error:
            body_text = response.text
            if "already initialized" in body_text:
                if self._session_id is not None:
                    logger.info("%s existing session accepted", self._label)
                    return True
                logger.warning(
                    "%s server returned 'already initialized' without session ID", self._label
                )
                self._session_id = None
                return False
            raise McpException(f"MCP HTTP error: {response.status_code} - {body_text}")

        json_payload = extract_json_from_sse(response.text)
        message = json.loads(json_payload) if json_payload else {}
        if "error" in message:
            raise McpException(f"MCP initialize error: {message['error']}")

        logger.info("%s initialize response: %s", self._label, message.get("result"))

        result = message.get("result")
        if not isinstance(result, dict):
            raise McpException("MCP initialize response has no result object")
        protocol_version = result.get("protocolVersion")
        if not isinstance(protocol_version, str) or not protocol_version:
            raise McpException("MCP initialize response has no protocolVersion")
        self._protocol_version = protocol_version

        if self._session_id is None:
            logger.warning(
                "%s no session ID in initialize response, subsequent calls may fail", self._label
            )
            return False

        await self._send_notification("notifications/initialized", {})
        logger.info("%s protocol initialized", self._label)
        return True

    async def _load_tools(self) -> None:
        result = await self._send_request("tools/list", {})
        if isinstance(result, dict) and "tools" in result and isinstance(result["tools"], list):
            tool_list: list[McpTool] = []
            for tool in result["tools"]:
                if isinstance(tool, dict):
                    name = str(tool.get("name", ""))
                    description = str(tool.get("description", ""))
                    input_schema = tool.get("inputSchema")
                    if not isinstance(input_schema, dict):
                        input_schema = {}
                    tool_list.append(
                        McpTool(name=name, description=description, input_schema=input_schema)
                    )
            self._cached_tools = tool_list

    async def _recover_session(self, failed_generation: int) -> bool:
        """Negotiate a new session after the old one stopped being recognised.

        Without this the bot answered without Remnawave data for as long as it
        stayed up: the MCP server can be restarted on its own (a deploy, a crash
        loop), and every tool call after that failed on a session id the server
        had forgotten.
        """
        async with self._session_lock:
            if self._session_generation != failed_generation:
                # Another task already re-initialised while we waited for the lock.
                return self._initialized

            logger.warning("%s session at %s expired — re-initializing", self._label, self.base_url)
            previous_tools = {tool.name for tool in self._cached_tools}
            self._session_id = None
            self._protocol_version = None
            self._initialized = False

            recovered = await self.init()
            self._session_generation += 1

            if not recovered:
                logger.error("%s could not re-establish the session at %s", self._label, self.base_url)
                return False

            current_tools = {tool.name for tool in self._cached_tools}
            if current_tools != previous_tools:
                logger.warning(
                    "%s tool set changed after reconnect: %s -> %s",
                    self._label,
                    sorted(previous_tools),
                    sorted(current_tools),
                )
            return True

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._request_id += 1
        req: dict[str, Any] = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": self._request_id,
            "method": method,
            "params": params if params is not None else {},
        }
        logger.debug("%s request [%d]: %s", self._label, self._request_id, method)

        response = await self._post(req)
        if response.is_error:
            if looks_like_expired_session(response):
                raise McpSessionExpired(
                    f"MCP session rejected: {response.status_code} - {response.text}"
                )
            raise McpException(f"MCP HTTP error: {response.status_code} - {response.text}")

        json_payload = extract_json_from_sse(response.text)
        message = json.loads(json_payload) if json_payload else {}
        if "error" in message:
            raise McpException(f"MCP error: {message['error']}")
        return message.get("result")

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        try:
            notification: dict[str, Any] = {
                "jsonrpc": self.JSONRPC_VERSION,
                "method": method,
                "params": params if params is not None else {},
            }
            await self._post(notification)
        except Exception as e:
            logger.warning("%s failed to send notification %s: %s", self._label, method, e)

    async def _terminate_session(self) -> None:
        if self._session_id is None:
            return
        headers = {
            "Accept": "application/json, text/event-stream",
            self.SESSION_HEADER: self._session_id,
        }
        if self._protocol_version:
            headers[self.PROTOCOL_HEADER] = self._protocol_version

        response = await self._get_client().delete(self.base_url, headers=headers)
        if response.status_code in (200, 202, 204, 404, 405):
            return
        raise McpException(
            f"MCP session termination failed: {response.status_code} - {response.text}"
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool and return result as JSON string.

        A session the server has forgotten is re-negotiated once and the call is
        replayed: the exposed tools are read-only lookups plus one idempotent
        delete, so a second attempt is safe.
        """
        if not self._initialized:
            return json.dumps({"error": f"{self._label} client not initialized"})

        generation = self._session_generation
        try:
            return await self._invoke_tool(tool_name, arguments)
        except McpSessionExpired:
            if not await self._recover_session(generation):
                return json.dumps({"error": f"{self._label} session lost and not recovered: {tool_name}"})
            try:
                return await self._invoke_tool(tool_name, arguments)
            except Exception as e:
                logger.error(
                    "%s tool %s failed after reconnect: %s", self._label, tool_name, e, exc_info=True
                )
                await self._notify_admins(
                    f"{self._label} tool call failed after reconnect: {tool_name}", e
                )
                return json.dumps({"error": str(e) if str(e) else "unknown error"})
        except Exception as e:
            logger.error("%s failed to call tool: %s: %s", self._label, tool_name, e, exc_info=True)
            await self._notify_admins(f"{self._label} tool call failed: {tool_name}", e)
            return json.dumps({"error": str(e) if str(e) else "unknown error"})

    async def _invoke_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> str:
        """One tools/call round trip, with the result rendered as a JSON string."""
        result = await self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments if arguments is not None else {}},
        )
        return json.dumps(result) if not isinstance(result, str) else result

    def shutdown(self) -> None:
        """Synchronously reset initialization state and session ID."""
        self._initialized = False
        self._session_id = None
        self._protocol_version = None

    async def close(self) -> None:
        """Terminate the owned MCP session and close an internally owned HTTP client."""
        try:
            await self._terminate_session()
        except Exception as error:
            logger.warning(
                "%s failed to terminate session at %s: %s", self._label, self.base_url, error
            )
        finally:
            self.shutdown()

        if not self._custom_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
