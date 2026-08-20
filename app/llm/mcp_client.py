"""HTTP client for Model Context Protocol (MCP) JSON-RPC 2.0 communication."""

import inspect
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
    """JSON-RPC 2.0 client for Remnawave MCP server over HTTP."""

    PROTOCOL_VERSION = "2024-11-05"
    SESSION_HEADER = "Mcp-Session-Id"
    REQUEST_TIMEOUT = 30.0
    JSONRPC_VERSION = "2.0"

    def __init__(
        self,
        base_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
        admin_notifier: Any | None = None,
    ) -> None:
        if base_url is None and settings is not None:
            base_url = settings.remnawave_mcp_url
        self.base_url = (base_url or "http://localhost:3100").rstrip("/")
        self.admin_notifier = admin_notifier
        self._custom_client = http_client is not None
        self._http_client = http_client
        self._request_id = 0
        self._session_id: str | None = None
        self._initialized = False
        self._cached_tools: list[McpTool] = []

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
        return await self._get_client().post(self.base_url, json=payload, headers=headers)

    def list_tools(self) -> list[McpTool]:
        """Return cached list of available tools."""
        return list(self._cached_tools)

    async def init(self) -> bool:
        """Perform JSON-RPC initialize handshake and load tool definitions."""
        try:
            has_session = await self._initialize_session()
            if not has_session:
                logger.info("MCP retrying initialize at %s", self.base_url)
                has_session = await self._initialize_session()
            if not has_session:
                logger.warning(
                    "MCP server at %s has stale session (no session ID). "
                    "Restart the MCP server to clear. Bot will run without tools from this server.",
                    self.base_url,
                )
                return False
            await self._load_tools()
            self._initialized = True
            logger.info(
                "MCP HTTP client initialized with %d tools at %s",
                len(self._cached_tools),
                self.base_url,
            )
            return True
        except Exception as e:
            logger.error(
                "Failed to initialize MCP HTTP client — bot will run without tools from %s: %s",
                self.base_url,
                e,
                exc_info=True,
            )
            await self._notify_admins(f"MCP HTTP init failed for {self.base_url}", e)
            return False

    async def _notify_admins(self, context: str, error: Exception) -> None:
        """Forward a failure to the support group, tolerating a missing notifier."""
        notify = getattr(self.admin_notifier, "notify_error", None)
        if notify is None:
            return
        try:
            result = notify(context, error=error)
            if inspect.isawaitable(result):
                await result
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
                },
            },
        }

        logger.debug("MCP initialize request [%d]", self._request_id)

        response = await self._post(req)

        # Check for session ID in response headers (case-insensitive)
        session_header = response.headers.get(self.SESSION_HEADER)
        if session_header:
            self._session_id = session_header
            logger.debug("MCP session established: %s", self._session_id)

        if response.is_error:
            body_text = response.text
            if "already initialized" in body_text:
                if self._session_id is not None:
                    logger.info("MCP session reused: %s", self._session_id)
                    return True
                logger.warning(
                    "MCP server returned 'already initialized' without session ID — retrying"
                )
                self._session_id = None
                return False
            raise McpException(f"MCP HTTP error: {response.status_code} - {body_text}")

        json_payload = extract_json_from_sse(response.text)
        message = json.loads(json_payload) if json_payload else {}
        if "error" in message:
            raise McpException(f"MCP initialize error: {message['error']}")

        logger.info("MCP initialize response: %s", message.get("result"))

        if self._session_id is None:
            logger.warning("No MCP session ID in initialize response, subsequent calls may fail")
            return False

        await self._send_notification("notifications/initialized", {})
        logger.info("MCP protocol initialized")
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

    async def _send_request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._request_id += 1
        req: dict[str, Any] = {
            "jsonrpc": self.JSONRPC_VERSION,
            "id": self._request_id,
            "method": method,
            "params": params if params is not None else {},
        }
        logger.debug("MCP request [%d]: %s", self._request_id, method)

        response = await self._post(req)
        if response.is_error:
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
            logger.warning("Failed to send MCP notification %s: %s", method, e)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool and return result as JSON string."""
        if not self._initialized:
            return json.dumps({"error": "MCP client not initialized"})
        try:
            result = await self._send_request(
                "tools/call",
                {"name": tool_name, "arguments": arguments if arguments is not None else {}},
            )
            return json.dumps(result) if not isinstance(result, str) else result
        except Exception as e:
            logger.error("Failed to call tool: %s: %s", tool_name, e, exc_info=True)
            await self._notify_admins(f"MCP tool call failed: {tool_name}", e)
            return json.dumps({"error": str(e) if str(e) else "unknown error"})

    def shutdown(self) -> None:
        """Synchronously reset initialization state and session ID."""
        self._initialized = False
        self._session_id = None

    async def close(self) -> None:
        """Asynchronously send shutdown notification and close HTTP client."""
        if self._initialized:
            await self._send_notification("notifications/cancelled", {"reason": "bot shutdown"})
        self.shutdown()
        if not self._custom_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
