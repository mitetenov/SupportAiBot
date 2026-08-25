"""Unit tests for HttpMcpClient and render_tool_result with MCP SDK v2."""

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

from app.llm.mcp_client import (
    HttpMcpClient,
    McpTool,
    render_tool_result,
)


class MockSdkClient:
    """Mock context-managed SDK v2 Client."""

    def __init__(
        self,
        protocol_version: str = "2026-07-28",
        tools: list[Tool] | None = None,
        tool_results: dict[str, Any] | None = None,
        raise_on_enter: Exception | None = None,
        raise_on_list_tools: Exception | None = None,
        raise_on_call: Exception | None = None,
        pages: list[ListToolsResult] | None = None,
    ) -> None:
        self._protocol_version = protocol_version
        self._tools = tools or []
        self._tool_results = tool_results or {}
        self._raise_on_enter = raise_on_enter
        self._raise_on_list_tools = raise_on_list_tools
        self._raise_on_call = raise_on_call
        self._pages = pages
        self._page_idx = 0
        self.closed = False
        self.entered = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> MockSdkClient:
        if self._raise_on_enter:
            raise self._raise_on_enter
        self.entered = True
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.closed = True

    def protocol_version(self) -> str:
        return self._protocol_version

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        if self._raise_on_list_tools:
            raise self._raise_on_list_tools
        if self._pages is not None:
            res = self._pages[self._page_idx]
            self._page_idx += 1
            return res
        return ListToolsResult(tools=self._tools, next_cursor=None)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any
    ) -> CallToolResult:
        self.calls.append((name, arguments or {}))
        if self._raise_on_call:
            err = self._raise_on_call
            self._raise_on_call = None
            raise err
        if name in self._tool_results:
            return self._tool_results[name]
        return CallToolResult(content=[TextContent(type="text", text=json.dumps({"ok": True}))])


class TestMcpTool:
    """Test McpTool descriptor dataclass."""

    def test_mcp_tool_dataclass_defaults(self) -> None:
        tool = McpTool(name="test_tool")
        assert tool.name == "test_tool"
        assert tool.description == ""
        assert tool.input_schema == {}

    def test_mcp_tool_dataclass_custom(self) -> None:
        schema = {"type": "object", "properties": {"id": {"type": "number"}}}
        tool = McpTool(name="get_user", description="Get a user", input_schema=schema)
        assert tool.name == "get_user"
        assert tool.description == "Get a user"
        assert tool.input_schema == schema

    def test_mcp_tool_dataclass_none_schema(self) -> None:
        tool = McpTool(name="get_user", input_schema=None)  # type: ignore[arg-type]
        assert tool.input_schema == {}


class TestRenderToolResult:
    """Test tool result serialization and normalization."""

    def test_is_error_with_text_content(self) -> None:
        res = CallToolResult(
            content=[TextContent(type="text", text="Invalid user ID")],
            is_error=True,
        )
        rendered = render_tool_result(res)
        data = json.loads(rendered)
        assert data == {"error": "Invalid user ID"}

    def test_is_error_without_content(self) -> None:
        res = CallToolResult(content=[], is_error=True)
        rendered = render_tool_result(res)
        data = json.loads(rendered)
        assert data == {"error": "Tool execution failed"}

    def test_structured_content_takes_precedence(self) -> None:
        res = CallToolResult(
            content=[TextContent(type="text", text="ignored text")],
            structured_content={"status": "active", "days_left": 10},
        )
        rendered = render_tool_result(res)
        data = json.loads(rendered)
        assert data == {"status": "active", "days_left": 10}

    def test_single_text_content_returns_verbatim_string(self) -> None:
        json_text = '{"users": [{"id": 1, "name": "Alice"}]}'
        res = CallToolResult(content=[TextContent(type="text", text=json_text)])
        rendered = render_tool_result(res)
        assert rendered == json_text

    def test_multiple_content_blocks_serialized_as_json_array(self) -> None:
        res = CallToolResult(
            content=[
                TextContent(type="text", text="Part 1"),
                TextContent(type="text", text="Part 2"),
            ]
        )
        rendered = render_tool_result(res)
        data = json.loads(rendered)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["type"] == "text"
        assert data[0]["text"] == "Part 1"
        assert data[1]["type"] == "text"
        assert data[1]["text"] == "Part 2"

    def test_non_text_content_serialized_as_json_array(self) -> None:
        res = CallToolResult(
            content=[
                ImageContent(type="image", data="base64data", mime_type="image/png"),
            ]
        )
        rendered = render_tool_result(res)
        data = json.loads(rendered)
        assert isinstance(data, list)
        assert data[0]["type"] == "image"
        assert data[0]["data"] == "base64data"


class TestHttpMcpClient:
    """Test HttpMcpClient lifecycle, tools listing, and execution."""

    @pytest.fixture
    def admin_notifier(self) -> MagicMock:
        notifier = MagicMock()
        notifier.notify_error = AsyncMock()
        return notifier

    def test_initial_state_before_init(self) -> None:
        client = HttpMcpClient(server_name="remnawave", base_url="http://localhost:3100")
        assert client.list_tools() == []
        assert client.initialized is False
        assert client.protocol_version is None
        assert client.server_name == "remnawave"

    @pytest.mark.asyncio
    async def test_call_tool_before_init_returns_error_json(self) -> None:
        client = HttpMcpClient(server_name="remnawave", base_url="http://localhost:3100")
        result = await client.call_tool("test_tool", {})
        data = json.loads(result)
        assert "error" in data
        assert "not initialized" in data["error"]

    @pytest.mark.asyncio
    async def test_successful_init_and_tool_loading(self, admin_notifier: MagicMock) -> None:
        mock_tool = Tool(
            name="users_get_by_telegram_id",
            description="Get user by tg id",
            input_schema={"type": "object", "properties": {"telegramId": {"type": "number"}}},
        )
        sdk_client = MockSdkClient(protocol_version="2026-07-28", tools=[mock_tool])

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            admin_notifier=admin_notifier,
            client_factory=lambda _url: sdk_client,
        )

        success = await client.init()
        assert success is True
        assert client.initialized is True
        assert client.protocol_version == "2026-07-28"

        tools = client.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "users_get_by_telegram_id"
        assert tools[0].description == "Get user by tg id"
        assert tools[0].input_schema == {
            "type": "object",
            "properties": {"telegramId": {"type": "number"}},
        }
        admin_notifier.notify_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_pagination_in_list_tools(self) -> None:
        page1 = ListToolsResult(
            tools=[Tool(name="tool_1", description="Tool 1", input_schema={})],
            next_cursor="cursor-page-2",
        )
        page2 = ListToolsResult(
            tools=[Tool(name="tool_2", description="Tool 2", input_schema={})],
            next_cursor=None,
        )
        sdk_client = MockSdkClient(pages=[page1, page2])

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=lambda _url: sdk_client,
        )

        assert await client.init() is True
        tools = client.list_tools()
        assert len(tools) == 2
        assert [t.name for t in tools] == ["tool_1", "tool_2"]

    @pytest.mark.asyncio
    async def test_init_failure_cleans_up_and_notifies_admins(
        self, admin_notifier: MagicMock
    ) -> None:
        sdk_client = MockSdkClient(raise_on_enter=ConnectionRefusedError("Connection refused"))

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            admin_notifier=admin_notifier,
            client_factory=lambda _url: sdk_client,
        )

        success = await client.init()
        assert success is False
        assert client.initialized is False
        assert client.list_tools() == []
        admin_notifier.notify_error.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_init_failure_during_list_tools_cleans_up(
        self, admin_notifier: MagicMock
    ) -> None:
        sdk_client = MockSdkClient(raise_on_list_tools=RuntimeError("Protocol error in list_tools"))

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            admin_notifier=admin_notifier,
            client_factory=lambda _url: sdk_client,
        )

        success = await client.init()
        assert success is False
        assert client.initialized is False
        assert client.list_tools() == []
        admin_notifier.notify_error.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_tool_success(self) -> None:
        sdk_client = MockSdkClient(
            tools=[Tool(name="nodes_list", description="", input_schema={})],
            tool_results={
                "nodes_list": CallToolResult(
                    content=[TextContent(type="text", text='{"nodes": [{"id": 1}]}')]
                )
            },
        )
        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=lambda _url: sdk_client,
        )
        assert await client.init() is True

        res = await client.call_tool("nodes_list", {})
        data = json.loads(res)
        assert data == {"nodes": [{"id": 1}]}

    @pytest.mark.asyncio
    async def test_call_tool_is_error_notifies_admin_without_reconnect(
        self, admin_notifier: MagicMock
    ) -> None:
        sdk_client = MockSdkClient(
            tools=[Tool(name="failing_tool", description="", input_schema={})],
            tool_results={
                "failing_tool": CallToolResult(
                    content=[TextContent(type="text", text="Upstream panel 500")],
                    is_error=True,
                )
            },
        )
        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            admin_notifier=admin_notifier,
            client_factory=lambda _url: sdk_client,
        )
        assert await client.init() is True

        res = await client.call_tool("failing_tool", {})
        data = json.loads(res)
        assert data == {"error": "Upstream panel 500"}
        admin_notifier.notify_error.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_tool_bedolaga_ok_false_is_not_treated_as_transport_error(
        self, admin_notifier: MagicMock
    ) -> None:
        sdk_client = MockSdkClient(
            tools=[Tool(name="bedolaga_subscription_get", description="", input_schema={})],
            tool_results={
                "bedolaga_subscription_get": CallToolResult(
                    content=[TextContent(type="text", text='{"ok": false, "reason": "not_found"}')]
                )
            },
        )
        client = HttpMcpClient(
            server_name="bedolaga",
            base_url="http://bedolaga-mcp:3100",
            admin_notifier=admin_notifier,
            client_factory=lambda _url: sdk_client,
        )
        assert await client.init() is True

        res = await client.call_tool("bedolaga_subscription_get", {"telegram_id": 123})
        data = json.loads(res)
        assert data == {"ok": False, "reason": "not_found"}
        admin_notifier.notify_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self) -> None:
        sdk_client = MockSdkClient(tools=[Tool(name="nodes_list", description="", input_schema={})])
        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=lambda _url: sdk_client,
        )
        assert await client.init() is True
        assert sdk_client.entered is True
        assert sdk_client.closed is False

        await client.close()
        assert sdk_client.closed is True
        assert client.initialized is False
        assert client.protocol_version is None
        assert client.list_tools() == []

        # Second close is a safe no-op
        await client.close()


class TestSessionRecovery:
    """Test automatic recovery on broken session or transport loss."""

    @pytest.mark.asyncio
    async def test_session_loss_triggers_reconnect_and_call_replay(self) -> None:
        clients_created: list[MockSdkClient] = []

        def client_factory(_url: str) -> MockSdkClient:
            idx = len(clients_created)
            if idx == 0:
                # First client raises MCPError "Session not found" on call
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    raise_on_call=MCPError(-32000, "Session not found"),
                )
            else:
                # Replacement client succeeds
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    tool_results={
                        "nodes_list": CallToolResult(
                            content=[TextContent(type="text", text='{"ok": true}')]
                        )
                    },
                )
            clients_created.append(c)
            return c

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=client_factory,
        )
        assert await client.init() is True
        assert len(clients_created) == 1

        result = await client.call_tool("nodes_list", {})
        data = json.loads(result)
        assert data == {"ok": True}
        assert len(clients_created) == 2
        assert clients_created[0].closed is True

    @pytest.mark.asyncio
    async def test_concurrent_failures_trigger_only_one_reconnect(self) -> None:
        clients_created: list[MockSdkClient] = []

        def client_factory(_url: str) -> MockSdkClient:
            idx = len(clients_created)
            if idx == 0:
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    raise_on_call=MCPError(-32000, "Session not found"),
                )
            else:
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    tool_results={
                        "nodes_list": CallToolResult(
                            content=[TextContent(type="text", text='{"ok": true}')]
                        )
                    },
                )
            clients_created.append(c)
            return c

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=client_factory,
        )
        assert await client.init() is True

        results = await asyncio.gather(
            client.call_tool("nodes_list", {}),
            client.call_tool("nodes_list", {}),
            client.call_tool("nodes_list", {}),
        )

        assert len(clients_created) == 2, "Only 1 reconnection should have occurred"
        assert all(json.loads(r) == {"ok": True} for r in results)

    @pytest.mark.asyncio
    async def test_failed_reconnect_returns_error_json(self) -> None:
        clients_created: list[MockSdkClient] = []

        def client_factory(_url: str) -> MockSdkClient:
            idx = len(clients_created)
            if idx == 0:
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    raise_on_call=MCPError(-32000, "Session not found"),
                )
            else:
                c = MockSdkClient(raise_on_enter=ConnectionRefusedError("Server down"))
            clients_created.append(c)
            return c

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=client_factory,
        )
        assert await client.init() is True

        result = await client.call_tool("nodes_list", {})
        data = json.loads(result)
        assert "error" in data
        assert "not recovered" in data["error"]

    @pytest.mark.asyncio
    async def test_non_recoverable_error_does_not_trigger_reconnect(self) -> None:
        clients_created: list[MockSdkClient] = []

        def client_factory(_url: str) -> MockSdkClient:
            c = MockSdkClient(
                tools=[Tool(name="nodes_get", description="", input_schema={})],
                raise_on_call=MCPError(-32602, "Invalid arguments"),
            )
            clients_created.append(c)
            return c

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=client_factory,
        )
        assert await client.init() is True

        result = await client.call_tool("nodes_get", {"invalid": True})
        data = json.loads(result)
        assert "error" in data
        assert len(clients_created) == 1, "Non-recoverable error must not reconnect"

    @pytest.mark.asyncio
    async def test_cross_task_reconnect_does_not_fail_with_task_affinity_error(self) -> None:
        """AnyIO context manager must not be exited from a different task than entered."""
        clients_created: list[MockSdkClient] = []

        def client_factory(_url: str) -> MockSdkClient:
            idx = len(clients_created)
            if idx == 0:
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    raise_on_call=ConnectionResetError("Connection reset by peer"),
                )
            else:
                c = MockSdkClient(
                    tools=[Tool(name="nodes_list", description="", input_schema={})],
                    tool_results={
                        "nodes_list": CallToolResult(
                            content=[TextContent(type="text", text='{"ok": true}')]
                        )
                    },
                )
            clients_created.append(c)
            return c

        client = HttpMcpClient(
            server_name="remnawave",
            base_url="http://test-mcp:3100",
            client_factory=client_factory,
        )

        # Task 1: Startup task initializes client
        init_task = asyncio.create_task(client.init())
        assert await init_task is True

        # Task 2: Worker / Telegram message task calls tool and triggers reconnect
        async def worker_call() -> str:
            return await client.call_tool("nodes_list", {})

        worker_task = asyncio.create_task(worker_call())
        result_str = await worker_task
        data = json.loads(result_str)
        assert data == {"ok": True}
        assert len(clients_created) == 2

        await client.close()


class TestErrorRecoveryFilter:
    """Test precision of _is_recoverable_error."""

    def test_programmatic_and_logic_errors_are_not_recoverable(self) -> None:
        from app.llm.mcp_client import _is_recoverable_error

        assert _is_recoverable_error(TypeError("unhashable type")) is False
        assert _is_recoverable_error(ValueError("invalid literal")) is False
        assert _is_recoverable_error(KeyError("missing_key")) is False
        assert _is_recoverable_error(AttributeError("no attribute")) is False
        assert _is_recoverable_error(IndexError("list index out of range")) is False
        assert _is_recoverable_error(json.JSONDecodeError("msg", "doc", 0)) is False
        assert _is_recoverable_error(MCPError(-32601, "Method not found")) is False
        assert _is_recoverable_error(MCPError(-32602, "Invalid params")) is False

    def test_transport_and_session_errors_are_recoverable(self) -> None:
        import httpx

        from app.llm.mcp_client import _is_recoverable_error

        assert _is_recoverable_error(ConnectionResetError("reset")) is True
        assert _is_recoverable_error(ConnectionRefusedError("refused")) is True
        assert _is_recoverable_error(BrokenPipeError("broken")) is True
        assert _is_recoverable_error(TimeoutError("timed out")) is True
        assert _is_recoverable_error(EOFError("eof")) is True
        assert _is_recoverable_error(httpx.ConnectError("cannot connect")) is True
        assert _is_recoverable_error(httpx.ReadTimeout("read timeout")) is True
        assert _is_recoverable_error(MCPError(-32000, "Session not found")) is True
        assert (
            _is_recoverable_error(MCPError(-32000, "Bad Request: Server not initialized")) is True
        )
        assert _is_recoverable_error(RuntimeError("Connection closed unexpectedly")) is True


class TestMcpLoggingLevel:
    def test_mcp_logger_is_warning_or_above(self) -> None:
        import logging

        assert logging.getLogger("mcp").level >= logging.WARNING
