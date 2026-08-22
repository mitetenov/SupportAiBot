"""Unit tests for HttpMcpClient."""

import asyncio
import json
from unittest.mock import MagicMock

import httpx
import pytest

from app.llm.mcp_client import (
    HttpMcpClient,
    McpTool,
    extract_json_from_sse,
    looks_like_expired_session,
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
        client = HttpMcpClient(server_name="remnawave", base_url="http://localhost:3100")
        assert client.list_tools() == []
        assert client.initialized is False
        assert client.session_id is None

    @pytest.mark.asyncio
    async def test_should_return_error_when_calling_tool_before_init(self) -> None:
        client = HttpMcpClient(server_name="remnawave", base_url="http://localhost:3100")
        result = await client.call_tool("test_tool", {})
        data = json.loads(result)
        assert "error" in data
        assert "not initialized" in data["error"]

    def test_should_set_initialized_flag_to_false_on_shutdown(self) -> None:
        client = HttpMcpClient(server_name="remnawave", base_url="http://localhost:3100")
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
                            "protocolVersion": "2025-11-25",
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
                server_name="remnawave",
                base_url="http://test-mcp:3100",
                http_client=http_client,
                admin_notifier=admin_notifier,
            )
            success = await client.init()
            assert success is True
            assert client.initialized is True
            assert client.session_id == "sess-12345"
            assert client.protocol_version == "2025-11-25"

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
    async def test_follow_up_requests_carry_the_negotiated_protocol_version(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-version"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if body.get("method") == "tools/list":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
                )
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            assert await client.init() is True

        assert client.protocol_version == "2025-11-25"
        assert all(
            request.headers.get("mcp-protocol-version") == "2025-11-25" for request in seen[1:]
        )

    @pytest.mark.asyncio
    async def test_close_terminates_the_session_with_delete(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "DELETE":
                return httpx.Response(200)
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-close"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if body.get("method") == "tools/list":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
                )
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()
            await client.close()

        deletes = [request for request in seen if request.method == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0].headers["mcp-session-id"] == "sess-close"
        assert deletes[0].headers["mcp-protocol-version"] == "2025-11-25"
        posted_methods = [
            json.loads(request.content).get("method")
            for request in seen
            if request.method == "POST"
        ]
        assert "notifications/cancelled" not in posted_methods
        assert client.session_id is None
        assert client.protocol_version is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("delete_status", [404, 405])
    async def test_close_tolerates_an_absent_or_non_terminable_session(
        self, delete_status: int
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE":
                return httpx.Response(delete_status)
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-old-server"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if body.get("method") == "tools/list":
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}},
                )
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()
            await client.close()

        assert client.initialized is False

    @pytest.mark.asyncio
    async def test_close_terminates_session_when_init_failed_during_tool_loading(self) -> None:
        seen: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "DELETE":
                return httpx.Response(200)
            body = json.loads(request.content)
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-partial-init"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if body.get("method") == "tools/list":
                return httpx.Response(500, text="Internal Server Error")
            return httpx.Response(202)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            assert await client.init() is False
            assert client.initialized is False
            assert client.session_id == "sess-partial-init"

            await client.close()

        deletes = [request for request in seen if request.method == "DELETE"]
        assert len(deletes) == 1
        assert deletes[0].headers["mcp-session-id"] == "sess-partial-init"
        assert deletes[0].headers["mcp-protocol-version"] == "2025-11-25"
        assert client.session_id is None

    @pytest.mark.asyncio
    async def test_already_initialized_without_session_id_is_not_retried_blindly(self) -> None:
        initialize_calls = 0

        def handle(_request: httpx.Request) -> httpx.Response:
            nonlocal initialize_calls
            initialize_calls += 1
            return httpx.Response(
                400,
                json={
                    "jsonrpc": "2.0",
                    "error": {"code": -32000, "message": "Server already initialized"},
                    "id": None,
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            assert await client.init() is False

        assert initialize_calls == 1

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
                server_name="remnawave",
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
                server_name="remnawave",
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
                server_name="remnawave",
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
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
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
                server_name="remnawave",
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


class TestSessionRecovery:
    """The MCP server can restart under a running bot; the bot must not stay blind.

    mcp-remnawave keeps one session and forgets it when it restarts. Every
    tools/call after that came back as an error and the model answered without
    the user's subscription data — for as long as the bot stayed up.
    """

    @staticmethod
    def _server() -> tuple[httpx.MockTransport, dict]:
        """An MCP server whose session can be killed mid-test.

        ``state["dead"]`` is the set of session ids the server has forgotten —
        exactly what a restarted mcp-remnawave looks like to a running bot.
        """
        state: dict = {"sessions": 0, "calls": 0, "dead": set()}

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            method = body.get("method")

            if method == "initialize":
                state["sessions"] += 1
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": f"sess-{state['sessions']}"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if method == "notifications/initialized":
                return httpx.Response(200, json={})

            if request.headers.get("Mcp-Session-Id") in state["dead"]:
                return httpx.Response(404, text="Session not found")

            if method == "tools/list":
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"tools": [{"name": "nodes_list"}]},
                    },
                )
            if method == "tools/call":
                state["calls"] += 1
                return httpx.Response(
                    200,
                    json={"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
                )
            return httpx.Response(500, text="unexpected")

        return httpx.MockTransport(handle), state

    @pytest.mark.asyncio
    async def test_a_forgotten_session_is_renegotiated_and_the_call_replayed(self) -> None:
        transport, state = self._server()

        async with httpx.AsyncClient(transport=transport) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            assert await client.init() is True

            state["dead"].add("sess-1")  # the MCP server restarts
            result = json.loads(await client.call_tool("nodes_list", {}))

        assert result == {"ok": True}, "the call was not replayed after reconnecting"
        assert state["sessions"] == 2, "a new session was not negotiated"
        assert client.session_id == "sess-2"

    @pytest.mark.asyncio
    async def test_a_healthy_session_is_left_alone(self) -> None:
        transport, state = self._server()

        async with httpx.AsyncClient(transport=transport) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()

            await client.call_tool("nodes_list", {})

        assert state["sessions"] == 1, "a working session was thrown away"

    @pytest.mark.asyncio
    async def test_gives_up_with_an_error_when_the_new_session_is_dead_too(self) -> None:
        transport, state = self._server()

        async with httpx.AsyncClient(transport=transport) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()

            state["dead"].update({"sess-1", "sess-2"})
            result = json.loads(await client.call_tool("nodes_list", {}))

        assert "error" in result

    @pytest.mark.asyncio
    async def test_concurrent_calls_negotiate_only_one_new_session(self) -> None:
        transport, state = self._server()

        async with httpx.AsyncClient(transport=transport) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()

            state["dead"].add("sess-1")
            results = await asyncio.gather(*(client.call_tool("nodes_list", {}) for _ in range(3)))

        assert state["sessions"] == 2, "each in-flight call re-initialised on its own"
        assert all(json.loads(r) == {"ok": True} for r in results)

    @pytest.mark.asyncio
    async def test_a_tool_error_is_not_mistaken_for_an_expired_session(self) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            if body.get("method") == "initialize":
                return httpx.Response(
                    200,
                    headers={"Mcp-Session-Id": "sess-1"},
                    json={
                        "jsonrpc": "2.0",
                        "id": body["id"],
                        "result": {"protocolVersion": "2025-11-25"},
                    },
                )
            if body.get("method") == "tools/list":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": []}}
                )
            if body.get("method") == "tools/call":
                return httpx.Response(400, text="Invalid arguments for nodes_get")
            return httpx.Response(200, json={})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = HttpMcpClient(
                server_name="remnawave", base_url="http://mcp.test", http_client=http_client
            )
            await client.init()

            result = json.loads(await client.call_tool("nodes_get", {}))

        assert "error" in result
        assert client.session_id == "sess-1", "a bad request must not drop the session"


class TestExpiredSessionDetection:
    def test_404_means_the_session_is_gone(self) -> None:
        assert looks_like_expired_session(httpx.Response(404, text="Session not found")) is True

    def test_400_about_a_session_counts(self) -> None:
        assert looks_like_expired_session(httpx.Response(400, text="No valid session ID")) is True

    def test_the_wording_mcp_remnawave_actually_uses_counts(self) -> None:
        """The message a restarted mcp-remnawave really sends, captured live.

        It never mentions the session, so a predicate looking only for that word
        left the client holding a dead session forever — which is the whole
        failure this detection exists to catch.
        """
        body = '{"jsonrpc":"2.0","error":{"code":-32000,"message":"Bad Request: Server not initialized"},"id":null}'
        assert looks_like_expired_session(httpx.Response(400, text=body)) is True

    def test_already_initialized_is_not_an_expired_session(self) -> None:
        """The handshake's own 400 must not be read as a dead session."""
        assert (
            looks_like_expired_session(httpx.Response(400, text="Server already initialized"))
            is False
        )

    def test_an_ordinary_bad_request_does_not(self) -> None:
        assert looks_like_expired_session(httpx.Response(400, text="Invalid arguments")) is False

    def test_a_server_error_does_not(self) -> None:
        assert looks_like_expired_session(httpx.Response(500, text="boom")) is False
