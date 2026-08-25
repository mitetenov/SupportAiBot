"""Official MCP SDK v2 client wrapper for Model Context Protocol (MCP) communication."""

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation, TextContent

logger = logging.getLogger(__name__)


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

    @property
    def server_name(self) -> str:
        """Stable name of the MCP server this client is bound to.

        The router selects the tool allowlist by OWNER from this name, never by
        matching tool names globally.
        """
        ...

    def list_tools(self) -> list[McpTool]:
        """Return cached list of available MCP tools."""
        ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool with given arguments and return JSON string result."""
        ...


type McpSdkClientFactory = Callable[[str], Any]


def _default_client_factory(url: str) -> Client:
    return Client(
        url,
        mode="auto",
        client_info=Implementation(name="vpn-support-bot", version="2.0.1"),
    )


def render_tool_result(result: CallToolResult) -> str:
    """Normalize CallToolResult for LLM presentation according to the precedence rules:

    1. is_error=True -> JSON {"error": "<safe text>"}
    2. non-empty structured_content -> JSON dump
    3. single text content block -> text string verbatim
    4. multiple or non-text blocks -> JSON array of serialized blocks
    """
    if getattr(result, "is_error", False):
        error_msg = "Tool execution failed"
        content = getattr(result, "content", [])
        if content:
            texts = [
                getattr(c, "text", "")
                for c in content
                if (isinstance(c, TextContent) or hasattr(c, "text")) and getattr(c, "text", None)
            ]
            if texts:
                error_msg = ", ".join(texts)
        return json.dumps({"error": error_msg}, ensure_ascii=False)

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    content = getattr(result, "content", [])
    if len(content) == 1 and (
        isinstance(content[0], TextContent)
        or (getattr(content[0], "type", "") == "text" and hasattr(content[0], "text"))
    ):
        return str(content[0].text)

    serialized = []
    for block in content:
        if hasattr(block, "model_dump"):
            serialized.append(block.model_dump(mode="json", by_alias=True, exclude_none=True))
        elif isinstance(block, dict):
            serialized.append(block)
        else:
            serialized.append(block)
    return json.dumps(serialized, ensure_ascii=False)


def _is_recoverable_error(e: Exception) -> bool:
    """Determine if an exception represents a lost session/connection that can be recovered."""
    if isinstance(e, MCPError):
        if getattr(e, "code", None) in (-32601, -32602):
            return False
    err_str = str(e).lower()
    if "unknown tool" in err_str or "invalid argument" in err_str:
        return False
    return True


class HttpMcpClient(McpClientInterface):
    """Client for a single MCP server over HTTP using official Python MCP SDK v2.

    One instance talks to exactly one server: ``server_name`` names it in every
    log line and admin alert (so an operator can tell which MCP is down) and is
    sent to the server in the ``clientInfo`` handshake. The URL is supplied by
    the caller, never derived from settings, so the bot can hold independent
    clients for several MCP servers without one silently borrowing another's
    endpoint.
    """

    def __init__(
        self,
        server_name: str,
        base_url: str,
        admin_notifier: AdminNotifier | None = None,
        client_factory: McpSdkClientFactory | None = None,
    ) -> None:
        self._server_name = server_name
        self.base_url = base_url.rstrip("/")
        self.admin_notifier = admin_notifier
        self._client_factory = client_factory or _default_client_factory
        self._exit_stack: AsyncExitStack | None = None
        self._client: Any = None
        self._protocol_version: str | None = None
        self._initialized = False
        self._cached_tools: list[McpTool] = []
        # Serialises recovery so a burst of tool calls negotiates one reconnection
        # rather than one each; the counter tells a caller whether the session
        # it failed on has already been replaced. Both stay per-instance: a
        # burst of calls to one MCP never blocks another client's recovery.
        self._session_lock = asyncio.Lock()
        self._session_generation = 0

    @property
    def server_name(self) -> str:
        """Stable name of the MCP server this client is bound to."""
        return self._server_name

    @property
    def _label(self) -> str:
        """Log/alert prefix that names this MCP server."""
        return f"MCP[{self.server_name}]"

    @property
    def initialized(self) -> bool:
        """Whether client has completed initialization."""
        return self._initialized

    @property
    def protocol_version(self) -> str | None:
        """Protocol version negotiated with the active MCP server."""
        return self._protocol_version

    def list_tools(self) -> list[McpTool]:
        """Return cached list of available tools."""
        return list(self._cached_tools)

    async def init(self) -> bool:
        """Connect to MCP server, negotiate protocol, and load tool definitions."""
        async with self._session_lock:
            return await self._init_locked()

    async def _init_locked(self) -> bool:
        await self._close_stack_locked()
        stack = AsyncExitStack()
        try:
            client = self._client_factory(self.base_url)
            entered_client = await stack.enter_async_context(client)
            self._client = entered_client
            self._exit_stack = stack

            pv = getattr(entered_client, "protocol_version", None)
            if callable(pv):
                pv = pv()
            self._protocol_version = str(pv) if pv else None

            tools: list[McpTool] = []
            cursor: str | None = None
            while True:
                res = await entered_client.list_tools(cursor=cursor)
                for t in getattr(res, "tools", []):
                    schema = getattr(t, "input_schema", {})
                    if hasattr(schema, "model_dump"):
                        schema_dict = schema.model_dump(
                            mode="json", by_alias=True, exclude_none=True
                        )
                    elif isinstance(schema, dict):
                        schema_dict = schema
                    else:
                        schema_dict = {}
                    desc = getattr(t, "description", "") or ""
                    tools.append(
                        McpTool(name=t.name, description=desc, input_schema=schema_dict)
                    )
                next_cursor = getattr(res, "next_cursor", None)
                if not next_cursor:
                    break
                cursor = next_cursor

            self._cached_tools = tools
            self._initialized = True
            logger.info(
                "%s HTTP client initialized with %d tools at %s (protocol: %s)",
                self._label,
                len(self._cached_tools),
                self.base_url,
                self._protocol_version,
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
            await self._close_stack_locked()
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

    async def _recover_session(self, failed_generation: int) -> bool:
        """Re-establish connection after a broken session or server restart."""
        async with self._session_lock:
            if self._session_generation != failed_generation:
                # Another task already re-initialised while we waited for the lock.
                return self._initialized

            logger.warning(
                "%s connection at %s lost/expired — re-initializing", self._label, self.base_url
            )
            previous_tools = {tool.name for tool in self._cached_tools}

            recovered = await self._init_locked()
            self._session_generation += 1

            if not recovered:
                logger.error(
                    "%s could not re-establish connection at %s", self._label, self.base_url
                )
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

    async def _invoke_tool(self, tool_name: str, arguments: dict[str, Any] | None) -> str:
        if self._client is None:
            raise RuntimeError(f"{self._label} client is not connected")
        result = await self._client.call_tool(
            name=tool_name,
            arguments=arguments if arguments is not None else {},
        )
        rendered = render_tool_result(result)
        if getattr(result, "is_error", False):
            try:
                err_dict = json.loads(rendered)
                err_msg = err_dict.get("error", "tool returned is_error=True")
            except Exception:
                err_msg = "tool returned is_error=True"
            await self._notify_admins(
                f"{self._label} tool error: {tool_name}",
                RuntimeError(f"Tool {tool_name} returned error: {err_msg}"),
            )
        return rendered

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool and return result as JSON/text string.

        A session/connection the server dropped is re-negotiated once and the call is
        replayed: the exposed tools are read-only lookups plus one idempotent
        delete, so a second attempt is safe.
        """
        if not self._initialized or self._client is None:
            return json.dumps({"error": f"{self._label} client not initialized"})

        generation = self._session_generation
        try:
            return await self._invoke_tool(tool_name, arguments)
        except Exception as e:
            if not _is_recoverable_error(e):
                logger.error(
                    "%s failed to call tool: %s: %s", self._label, tool_name, e, exc_info=True
                )
                await self._notify_admins(f"{self._label} tool call failed: {tool_name}", e)
                return json.dumps({"error": str(e) if str(e) else "unknown error"})

            # Try recovery
            if not await self._recover_session(generation):
                return json.dumps(
                    {"error": f"{self._label} session lost and not recovered: {tool_name}"}
                )

            try:
                return await self._invoke_tool(tool_name, arguments)
            except Exception as retry_e:
                logger.error(
                    "%s tool %s failed after reconnect: %s",
                    self._label,
                    tool_name,
                    retry_e,
                    exc_info=True,
                )
                await self._notify_admins(
                    f"{self._label} tool call failed after reconnect: {tool_name}", retry_e
                )
                return json.dumps({"error": str(retry_e) if str(retry_e) else "unknown error"})

    async def _close_stack_locked(self) -> None:
        stack = self._exit_stack
        self._exit_stack = None
        self._client = None
        self._initialized = False
        self._protocol_version = None
        self._cached_tools = []
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as error:
                logger.warning(
                    "%s failed to close MCP client at %s: %s", self._label, self.base_url, error
                )

    async def close(self) -> None:
        """Close the owned MCP client context and release resources idempotently."""
        async with self._session_lock:
            await self._close_stack_locked()
