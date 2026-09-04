"""Official MCP SDK v2 client wrapper for Model Context Protocol (MCP) communication."""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from mcp.client import Client
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, Implementation, TextContent

from app.logging_config import TRACE
from app.logging_redaction import safe_serialize

logger = logging.getLogger(__name__)

# Silence verbose MCP SDK logs to prevent legacy session IDs from leaking at INFO level
logging.getLogger("mcp").setLevel(logging.WARNING)


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
    """Determine if an exception represents a lost session or broken transport.

    Programmatic errors (TypeError, ValueError, etc.) and application/logic errors
    must never trigger session reconnect.
    """
    # Programmatic errors must never trigger reconnection
    if isinstance(
        e,
        (
            TypeError,
            ValueError,
            KeyError,
            AttributeError,
            IndexError,
            json.JSONDecodeError,
        ),
    ):
        return False

    # Standard OS / network transport errors
    if isinstance(e, (ConnectionError, TimeoutError, EOFError)):
        return True

    # HTTP transport errors
    if isinstance(e, httpx.TransportError):
        return True

    # AnyIO stream / resource errors
    anyio_errors: tuple[type[Exception], ...] = ()
    try:
        import anyio

        anyio_errors = (anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError)
    except ImportError:
        pass
    if anyio_errors and isinstance(e, anyio_errors):
        return True

    # MCPError from server
    if isinstance(e, MCPError):
        msg = (getattr(e, "message", None) or "").lower()
        if (
            "session not found" in msg
            or "session expired" in msg
            or "not initialized" in msg
            or "connection closed" in msg
        ):
            return True
        return False

    # Fallback string inspection for wrapped connection/session errors
    err_str = str(e).lower()
    recoverable_phrases = (
        "session not found",
        "session expired",
        "connection closed",
        "connection refused",
        "connection reset",
        "remote protocol error",
        "broken pipe",
        "server not initialized",
    )
    return any(phrase in err_str for phrase in recoverable_phrases)


@dataclass
class _McpCommand:
    """Internal command sent to the owner task worker queue."""

    action: str
    future: asyncio.Future[Any]
    payload: dict[str, Any] = field(default_factory=dict)


class HttpMcpClient(McpClientInterface):
    """Client for a single MCP server over HTTP using official Python MCP SDK v2.

    To strictly avoid AnyIO task-affinity violations ("Attempted to exit cancel scope
    in a different task"), an internal owner task runs context manager entries,
    session recovery, and context exits through an async command queue. Tool calls
    use the active SDK client directly so independent requests are not serialized.
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
        self._session_generation = 0

        self._queue: asyncio.Queue[_McpCommand] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._recovery_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._call_state_lock = asyncio.Lock()
        self._no_active_calls = asyncio.Event()
        self._no_active_calls.set()
        self._active_calls = 0
        self._closing = False

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

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._queue = asyncio.Queue()
            queue = self._queue
            self._worker_task = asyncio.create_task(
                self._worker_loop(queue), name=f"mcp-worker-{self.server_name}"
            )

    async def _worker_loop(self, queue: asyncio.Queue[_McpCommand]) -> None:
        """Own the SDK context while allowing calls to run concurrently."""
        try:
            while True:
                try:
                    cmd = await queue.get()
                except asyncio.CancelledError:
                    break

                try:
                    if cmd.action == "close":
                        await self._close_stack_internal()
                        if not cmd.future.done():
                            cmd.future.set_result(None)
                        break

                    if cmd.action == "init":
                        success = await self._init_internal()
                    elif cmd.action == "recover":
                        success = await self._recover_session_internal(
                            int(cmd.payload["generation"])
                        )
                    else:
                        raise RuntimeError(f"Unknown MCP owner command: {cmd.action}")

                    if not cmd.future.done():
                        cmd.future.set_result(success)
                except Exception as e:
                    if not cmd.future.done():
                        cmd.future.set_exception(e)
                finally:
                    queue.task_done()
        finally:
            await self._close_stack_internal()

    async def _submit_owner_command(self, action: str, **payload: Any) -> Any:
        if self._closing and action != "close":
            raise RuntimeError(f"{self._label} client is closing")
        queue = self._queue
        worker = self._worker_task
        if queue is None or worker is None or worker.done():
            raise RuntimeError(f"{self._label} client owner is not running")

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        await queue.put(_McpCommand(action=action, future=future, payload=payload))
        return await future

    async def _init_internal(self) -> bool:
        await self._close_stack_internal()
        stack = AsyncExitStack()
        try:
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s initializing MCP connection to %s",
                    self._label,
                    self.base_url,
                )
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
            page = 1
            while True:
                res = await entered_client.list_tools(cursor=cursor)
                raw_tools = getattr(res, "tools", [])
                page_tools: list[McpTool] = []
                for t in raw_tools:
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
                    tool_item = McpTool(name=t.name, description=desc, input_schema=schema_dict)
                    tools.append(tool_item)
                    page_tools.append(tool_item)

                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "%s list_tools page %d (cursor=%s) returned %d tools: %s",
                        self._label,
                        page,
                        cursor,
                        len(page_tools),
                        safe_serialize(
                            [
                                {
                                    "name": t.name,
                                    "description": t.description,
                                    "input_schema": t.input_schema,
                                }
                                for t in page_tools
                            ]
                        ),
                    )

                next_cursor = getattr(res, "next_cursor", None)
                if not next_cursor:
                    break
                cursor = next_cursor
                page += 1

            self._cached_tools = tools
            self._initialized = True
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s completed list_tools: %d total tools loaded across %d page(s)",
                    self._label,
                    len(self._cached_tools),
                    page,
                )
            logger.info(
                "%s HTTP client initialized with %d tools at %s (protocol: %s)",
                self._label,
                len(self._cached_tools),
                self.base_url,
                self._protocol_version,
            )
            return True
        except Exception as e:
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s initialization error at %s: %s",
                    self._label,
                    self.base_url,
                    e,
                    exc_info=True,
                )
            logger.error(
                "%s initialization failed — bot will run without tools from %s: %s",
                self._label,
                self.base_url,
                e,
                exc_info=True,
            )
            await self._close_stack_internal()
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

    async def _recover_session_internal(self, failed_generation: int) -> bool:
        """Re-establish connection after a broken session or server restart."""
        if self._session_generation != failed_generation:
            # Another caller already triggered re-initialisation while queued
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s session recovery already performed by another caller (gen %d -> %d)",
                    self._label,
                    failed_generation,
                    self._session_generation,
                )
            return self._initialized

        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "%s recovering session (generation %d)",
                self._label,
                failed_generation,
            )
        logger.warning(
            "%s connection at %s lost/expired — re-initializing", self._label, self.base_url
        )
        previous_tools = {tool.name for tool in self._cached_tools}

        recovered = await self._init_internal()
        self._session_generation += 1

        if not recovered:
            logger.error("%s could not re-establish connection at %s", self._label, self.base_url)
            return False

        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "%s successfully recovered session (new generation %d)",
                self._label,
                self._session_generation,
            )
        current_tools = {tool.name for tool in self._cached_tools}
        if current_tools != previous_tools:
            logger.warning(
                "%s tool set changed after reconnect: %s -> %s",
                self._label,
                sorted(previous_tools),
                sorted(current_tools),
            )
        return True

    async def _invoke_tool(
        self, client: Any, tool_name: str, arguments: dict[str, Any] | None
    ) -> str:
        if client is None:
            raise RuntimeError(f"{self._label} client is not connected")
        args_payload = arguments if arguments is not None else {}
        if logger.isEnabledFor(TRACE):
            logger.log(
                TRACE,
                "%s calling tool '%s' with args: %s",
                self._label,
                tool_name,
                safe_serialize(args_payload),
            )
        start_time = time.monotonic()
        try:
            result = await client.call_tool(
                name=tool_name,
                arguments=args_payload,
            )
            duration = time.monotonic() - start_time
            rendered = render_tool_result(result)
            is_error = getattr(result, "is_error", False)
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s tool '%s' returned in %.3fs (is_error=%s): %s",
                    self._label,
                    tool_name,
                    duration,
                    is_error,
                    rendered,
                )
            if is_error:
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
        except Exception as e:
            duration = time.monotonic() - start_time
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s tool '%s' raised %s in %.3fs: %s",
                    self._label,
                    tool_name,
                    type(e).__name__,
                    duration,
                    e,
                )
            raise

    async def _close_stack_internal(self) -> None:
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

    async def init(self) -> bool:
        """Connect to MCP server, negotiate protocol, and load tool definitions."""
        async with self._close_lock:
            await self._no_active_calls.wait()
            async with self._call_state_lock:
                self._closing = False
            self._ensure_worker()
            return bool(await self._submit_owner_command("init"))

    async def _start_tool_call(self) -> tuple[bool, int, Any]:
        async with self._call_state_lock:
            if (
                self._closing
                or not self._initialized
                or self._client is None
                or self._worker_task is None
                or self._worker_task.done()
            ):
                return False, 0, None

            self._active_calls += 1
            self._no_active_calls.clear()
            return True, self._session_generation, self._client

    async def _finish_tool_call(self) -> None:
        async with self._call_state_lock:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._no_active_calls.set()

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute a tool with given arguments and return JSON string result."""
        started, generation, active_client = await self._start_tool_call()
        if not started:
            if logger.isEnabledFor(TRACE):
                logger.log(
                    TRACE,
                    "%s call_tool '%s' blocked: client not initialized or closing",
                    self._label,
                    tool_name,
                )
            return json.dumps({"error": f"{self._label} client not initialized"})

        try:
            try:
                return await self._invoke_tool(active_client, tool_name, arguments)
            except Exception as e:
                if not _is_recoverable_error(e):
                    logger.error(
                        "%s failed to call tool: %s: %s", self._label, tool_name, e, exc_info=True
                    )
                    await self._notify_admins(f"{self._label} tool call failed: {tool_name}", e)
                    return json.dumps({"error": str(e) if str(e) else "unknown error"})

                if logger.isEnabledFor(TRACE):
                    logger.log(
                        TRACE,
                        "%s tool '%s' failed with recoverable error %s: %s — initiating recovery",
                        self._label,
                        tool_name,
                        type(e).__name__,
                        e,
                    )

                async with self._recovery_lock:
                    if self._session_generation == generation:
                        try:
                            recovered = bool(
                                await self._submit_owner_command("recover", generation=generation)
                            )
                        except Exception:
                            recovered = False
                    else:
                        recovered = self._initialized

                if not recovered:
                    return json.dumps(
                        {"error": f"{self._label} session lost and not recovered: {tool_name}"}
                    )

                retry_client = self._client
                if retry_client is None:
                    return json.dumps({"error": f"{self._label} client not initialized"})
                try:
                    if logger.isEnabledFor(TRACE):
                        logger.log(
                            TRACE,
                            "%s re-invoking tool '%s' after session recovery",
                            self._label,
                            tool_name,
                        )
                    return await self._invoke_tool(retry_client, tool_name, arguments)
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
        finally:
            await self._finish_tool_call()

    async def close(self) -> None:
        """Close the owned MCP client context and release resources idempotently."""
        async with self._close_lock:
            async with self._call_state_lock:
                self._closing = True
            await self._no_active_calls.wait()
            worker = self._worker_task
            queue = self._queue
            try:
                if worker is not None and not worker.done() and queue is not None:
                    loop = asyncio.get_running_loop()
                    future: asyncio.Future[None] = loop.create_future()
                    await queue.put(_McpCommand(action="close", future=future))
                    try:
                        await future
                    except Exception:
                        pass

                if worker is not None:
                    try:
                        await worker
                    except Exception:
                        pass
            finally:
                self._worker_task = None
                self._queue = None
                self._exit_stack = None
                self._initialized = False
                self._client = None
                self._cached_tools = []
                self._protocol_version = None
                self._closing = False
