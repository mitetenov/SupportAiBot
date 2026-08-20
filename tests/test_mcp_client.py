"""Unit tests for HttpMcpClient."""

import json
from unittest.mock import MagicMock

import httpx
import pytest

from app.llm.mcp_client import (
    HttpMcpClient,
    McpTool,
    extract_json_from_sse,
)


class TestExtractJsonFromSse:
    """Test SSE parsing logic."""

    def test_should_extract_plain_json(self) -> None:
        input_data = '{"jsonrpc":"2.0","result":{"key":"value"},"id":1}'
        assert extract_json_from_sse(input_data) == input_data

    @pytest.mark.parametrize(
        ("name", "input_data", "expected"),
        [
            (
                "bare data line",
                'data: {"jsonrpc":"2.0","result":{"key":"value"},"id":1}',
                '{"jsonrpc":"2.0","result":{"key":"value"},"id":1}',
            ),
            (
                "preceded by an event line",
                'event: message\ndata: {"jsonrpc":"2.0","result":"hello","id":1}',
                '{"jsonrpc":"2.0","result":"hello","id":1}',
            ),
            (
                "only the first of several data lines",
                'event: message\ndata: {"jsonrpc":"2.0","result":"first","id":1}\n\ndata: {"jsonrpc":"2.0","result":"second","id":2}',
                '{"jsonrpc":"2.0","result":"first","id":1}',
            ),
        ],
    )
    def test_should_extract_the_first_data_line_from_an_sse_body(
        self, name: str, input_data: str, expected: str
    ) -> None:
        assert extract_json_from_sse(input_data) == expected

    def test_should_return_empty_body_as_is(self) -> None:
        assert extract_json_from_sse("") == ""

    def test_should_handle_sse_data_with_spaces(self) -> None:
        input_data = 'data:   {"jsonrpc":"2.0","result":"spaces","id":1}'
        assert extract_json_from_sse(input_data) == '  {"jsonrpc":"2.0","result":"spaces","id":1}'

    def test_should_handle_crlf_sse_data(self) -> None:
        input_data = 'data: {"jsonrpc":"2.0","result":"crlf","id":1}\r\n'
        assert extract_json_from_sse(input_data) == '{"jsonrpc":"2.0","result":"crlf","id":1}\r'


class TestHttpMcpClient:
    """Test HttpMcpClient initialization, session tracking, and tool execution."""

    @pytest.fixture
    def admin_notifier(self) -> MagicMock:
        notifier = MagicMock()
        notifier.notify_error = MagicMock()
        return notifier

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

    def test_should_return_empty_list_before_init(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:3100")
        assert client.list_tools() == []
        assert client.initialized is False
        assert client.session_id is None

    @pytest.mark.asyncio
    async def test_should_return_error_when_calling_tool_before_init(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:3100")
        result = await client.call_tool("test_tool", {})
        data = json.loads(result)
        assert "error" in data
        assert "not initialized" in data["error"]

    def test_should_set_initialized_flag_to_false_on_shutdown(self) -> None:
        client = HttpMcpClient(base_url="http://localhost:3100")
        client.initialized = True
        client.session_id = "test-session-123"
        client.shutdown()
        assert client.initialized is False
        assert client.session_id is None

    @pytest.mark.asyncio
    async def test_successful_initialize_and_load_tools(self, admin_notifier: MagicMock) -> None:
        calls: list[httpx.Request] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")

            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-12345"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "mcp-remnawave", "version": "1.0.0"},
                        },
                    },
                )
            if method == "notifications/initialized":
                assert request.headers.get("mcp-session-id") == "sess-12345"
                return httpx.Response(200, json={})
            if method == "tools/list":
                assert request.headers.get("mcp-session-id") == "sess-12345"
                return httpx.Response(
                    200,
                    text='data: {"jsonrpc":"2.0","id":'
                    + str(body["id"])
                    + ',"result":{"tools":[{"name":"users_get_by_telegram_id","description":"Get user by tg id","inputSchema":{"type":"object","properties":{"telegramId":{"type":"number"}}}}]}}\n\n',
                )
            return httpx.Response(404)

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test-mcp:3100"
        ) as http_client:
            client = HttpMcpClient(
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            success = await client.init()
            assert success is True
            assert client.initialized is True
            assert client.session_id == "sess-12345"

            tools = client.list_tools()
            assert len(tools) == 1
            assert tools[0].name == "users_get_by_telegram_id"
            assert tools[0].description == "Get user by tg id"
            assert tools[0].input_schema == {
                "type": "object",
                "properties": {"telegramId": {"type": "number"}},
            }

            # Check shutdown notification
            client.shutdown()
            assert client.initialized is False
            assert client.session_id is None
            admin_notifier.notify_error.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_already_initialized_with_session_id(
        self, admin_notifier: MagicMock
    ) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")

            if method == "initialize":
                return httpx.Response(
                    400,
                    headers={"Mcp-Session-Id": "existing-session-abc"},
                    json={
                        "jsonrpc": "2.0",
                        "error": {"code": -32000, "message": "Server already initialized"},
                        "id": None,
                    },
                )
            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"tools": []},
                    },
                )
            return httpx.Response(200, json={})

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test-mcp:3100"
        ) as http_client:
            client = HttpMcpClient(
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            success = await client.init()
            assert success is True
            assert client.session_id == "existing-session-abc"
            assert client.initialized is True

    @pytest.mark.asyncio
    async def test_handle_already_initialized_without_session_id(
        self, admin_notifier: MagicMock
    ) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Server already initialized"},
                    "id": None,
                },
            )

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test-mcp:3100"
        ) as http_client:
            client = HttpMcpClient(
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            success = await client.init()
            assert success is False
            assert client.session_id is None
            assert client.initialized is False

    @pytest.mark.asyncio
    async def test_handle_initialization_error(self, admin_notifier: MagicMock) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="Internal Server Error")

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test-mcp:3100"
        ) as http_client:
            client = HttpMcpClient(
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            success = await client.init()
            assert success is False
            assert client.initialized is False
            admin_notifier.notify_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_call_tool_success_and_error(self, admin_notifier: MagicMock) -> None:
        def handle_request(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")

            if method == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-call-test"},
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                )
            if method == "notifications/initialized":
                return httpx.Response(200, json={})
            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
                )
            if method == "tools/call":
                params = body.get("params", {})
                if params.get("name") == "nodes_list":
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "result": {"nodes": [{"id": 1, "name": "Node 1"}]},
                        },
                    )
                if params.get("name") == "failing_tool":
                    return httpx.Response(
                        200,
                        json={
                            "jsonrpc": "2.0",
                            "id": body["id"],
                            "error": {"code": -32603, "message": "Internal error in tool"},
                        },
                    )
            return httpx.Response(404)

        transport = httpx.MockTransport(handle_request)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test-mcp:3100"
        ) as http_client:
            client = HttpMcpClient(
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            await client.init()

            # Success call
            res = await client.call_tool("nodes_list", {})
            data = json.loads(res)
            assert data == {"nodes": [{"id": 1, "name": "Node 1"}]}

            # Error response from server
            res_err = await client.call_tool("failing_tool", {})
            err_data = json.loads(res_err)
            assert "error" in err_data
            admin_notifier.notify_error.assert_called()
