"""Unit tests for McpRouter."""

import json
from typing import Any

import pytest

from app.config import Settings
from app.llm.mcp_client import McpClientInterface, McpTool
from app.llm.mcp_router import McpRouter

CALLER = 555_000


class StubMcpClient(McpClientInterface):
    """Stub implementation of McpClientInterface for testing McpRouter."""

    def __init__(
        self,
        tools: list[McpTool] | None = None,
        tool_results: dict[str, str] | None = None,
    ) -> None:
        self.tools = tools if tools is not None else []
        self.tool_results = tool_results if tool_results is not None else {}
        self.calls: list[dict[str, Any]] = []

    def last_arguments(self) -> dict[str, Any]:
        return self.calls[-1] if self.calls else {}

    def list_tools(self) -> list[McpTool]:
        return self.tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        self.calls.append(arguments if arguments is not None else {})
        return self.tool_results.get(tool_name, json.dumps({"error": "Unknown"}))


def create_router(
    clients: list[McpClientInterface] | None = None,
    readonly: bool = False,
) -> McpRouter:
    return McpRouter(clients=clients, readonly=readonly)


class TestMcpRouter:
    """Test McpRouter tool filtering, aggregation, and Telegram ID security overrides."""

    def test_should_return_empty_tools_with_no_clients(self) -> None:
        assert create_router([]).list_tools() == []

    def test_should_return_empty_tools_with_none_client_list(self) -> None:
        assert create_router(None).list_tools() == []

    def test_should_aggregate_tools_from_all_clients(self) -> None:
        client1 = StubMcpClient(
            tools=[McpTool(name="users_get_by_telegram_id", description="desc1")],
            tool_results={"users_get_by_telegram_id": "result1"},
        )
        client2 = StubMcpClient(
            tools=[McpTool(name="hwid_devices_list", description="desc2")],
            tool_results={"hwid_devices_list": "result2"},
        )

        tools = create_router([client1, client2]).list_tools()

        assert len(tools) == 2
        assert tools[0].name == "users_get_by_telegram_id"
        assert tools[1].name == "hwid_devices_list"

    @pytest.mark.asyncio
    async def test_should_call_correct_tool(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="nodes_list", description="A test tool")],
            tool_results={"nodes_list": '{"status": "ok"}'},
        )

        result = await create_router([client]).call_tool(
            "nodes_list", {"param": "value"}, telegram_user_id=CALLER
        )

        assert "ok" in result

    @pytest.mark.asyncio
    async def test_should_return_error_for_unknown_tool(self) -> None:
        result = await create_router([]).call_tool("nonexistent", {}, telegram_user_id=CALLER)
        data = json.loads(result)
        assert "error" in data
        assert "nonexistent" in data["error"]

    @pytest.mark.asyncio
    async def test_should_call_tool_from_correct_client_when_multiple(self) -> None:
        client1 = StubMcpClient(
            tools=[McpTool(name="users_get_by_telegram_id", description="desc1")],
            tool_results={"users_get_by_telegram_id": "result1"},
        )
        client2 = StubMcpClient(
            tools=[McpTool(name="hwid_devices_list", description="desc2")],
            tool_results={"hwid_devices_list": "result2"},
        )

        router = create_router([client1, client2])

        res1 = await router.call_tool("users_get_by_telegram_id", {}, telegram_user_id=CALLER)
        res2 = await router.call_tool("hwid_devices_list", {}, telegram_user_id=CALLER)

        assert res1 == "result1"
        assert res2 == "result2"

    @pytest.mark.asyncio
    async def test_should_return_error_for_tool_not_in_any_client(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="nodes_get", description="desc")],
            tool_results={"nodes_get": "result_a"},
        )

        result = await create_router([client]).call_tool(
            "hwid_devices_list", {}, telegram_user_id=CALLER
        )
        assert "hwid_devices_list" in result

    @pytest.mark.asyncio
    async def test_should_handle_empty_clients_with_null_safety(self) -> None:
        router = create_router(None)
        assert router.list_tools() == []
        res = await router.call_tool("any", {}, telegram_user_id=CALLER)
        assert "error" in json.loads(res)

    def test_should_filter_out_non_allowed_tools(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(name="users_get_by_telegram_id", description="Allowed tool"),
                McpTool(name="some_unsafe_tool", description="Should be filtered out"),
                McpTool(name="nodes_list", description="Also allowed"),
            ],
            tool_results={
                "users_get_by_telegram_id": "allowed_result",
                "some_unsafe_tool": "should_not_be_callable",
            },
        )

        tools = create_router([client]).list_tools()

        assert len(tools) == 2
        assert tools[0].name == "users_get_by_telegram_id"
        assert tools[1].name == "nodes_list"

    @pytest.mark.asyncio
    async def test_should_return_error_for_filtered_tool(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="some_unsafe_tool", description="Filtered tool")],
            tool_results={"some_unsafe_tool": "should_not_be_callable"},
        )

        result = await create_router([client]).call_tool(
            "some_unsafe_tool", {}, telegram_user_id=CALLER
        )
        assert "error" in result
        assert "some_unsafe_tool" in result

    @pytest.mark.asyncio
    async def test_should_never_expose_revoke_subscription(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="users_revoke_subscription", description="destructive")],
            tool_results={"users_revoke_subscription": "revoked!"},
        )

        router = create_router([client])

        assert router.list_tools() == []
        result = await router.call_tool("users_revoke_subscription", {}, telegram_user_id=CALLER)
        assert "error" in result
        assert "revoked!" not in result

    @pytest.mark.asyncio
    async def test_should_withhold_write_tools_in_readonly_mode(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(name="hwid_devices_list", description="read"),
                McpTool(name="hwid_device_delete", description="write"),
            ],
            tool_results={"hwid_device_delete": "deleted!"},
        )

        readonly_router = create_router([client], readonly=True)

        assert len(readonly_router.list_tools()) == 1
        assert readonly_router.list_tools()[0].name == "hwid_devices_list"

        result = await readonly_router.call_tool(
            "hwid_device_delete", {"hwid": "x"}, telegram_user_id=CALLER
        )
        assert "Tool not allowed" in result
        assert "deleted!" not in result

    @pytest.mark.asyncio
    async def test_should_expose_write_tools_when_not_readonly(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="hwid_device_delete", description="write")],
            tool_results={"hwid_device_delete": "deleted!"},
        )

        router = create_router([client], readonly=False)

        assert len(router.list_tools()) == 1
        res = await router.call_tool("hwid_device_delete", {"hwid": "x"}, telegram_user_id=CALLER)
        assert "deleted!" in res

    def test_should_configure_readonly_mode_from_settings(
        self, valid_settings_dict: dict[str, Any]
    ) -> None:
        settings_ro = Settings(**{**valid_settings_dict, "remnawave_mcp_readonly": True})
        client = StubMcpClient(
            tools=[
                McpTool(name="hwid_devices_list", description="read"),
                McpTool(name="hwid_device_delete", description="write"),
            ]
        )
        router_ro = McpRouter(clients=[client], settings=settings_ro)
        assert len(router_ro.list_tools()) == 1
        assert router_ro.list_tools()[0].name == "hwid_devices_list"

        settings_rw = Settings(**{**valid_settings_dict, "remnawave_mcp_readonly": False})
        router_rw = McpRouter(clients=[client], settings=settings_rw)
        assert len(router_rw.list_tools()) == 2

    @pytest.mark.asyncio
    async def test_should_override_telegram_id_supplied_by_the_model(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="users_get_by_telegram_id", description="desc")],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id",
            {"telegramId": 999_999},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments().get("telegramId") == CALLER

    @pytest.mark.asyncio
    async def test_should_override_snake_case_telegram_id_variant(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="users_get_by_telegram_id", description="desc")],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id",
            {"telegram_id": "999999"},
            telegram_user_id=CALLER,
        )

        # Model sent a string, so override stays a string when schema omits type
        assert client.last_arguments().get("telegram_id") == str(CALLER)

    @pytest.mark.asyncio
    async def test_should_supply_telegram_id_from_schema_when_model_omits_it(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "number"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id", {}, telegram_user_id=CALLER
        )

        assert client.last_arguments().get("telegramId") == CALLER

    @pytest.mark.asyncio
    async def test_should_send_the_id_as_a_string_when_the_schema_declares_one(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "string"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id", {}, telegram_user_id=CALLER
        )

        assert client.last_arguments().get("telegramId") == str(CALLER)

    @pytest.mark.asyncio
    async def test_should_send_the_id_as_a_string_even_when_the_model_supplied_a_number(
        self,
    ) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "string"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id",
            {"telegramId": 999_999},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments().get("telegramId") == str(CALLER)

    @pytest.mark.asyncio
    async def test_should_send_the_id_as_a_number_when_the_schema_declares_one(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "number"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id",
            {"telegramId": "999999"},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments().get("telegramId") == CALLER

    @pytest.mark.asyncio
    async def test_should_send_the_id_as_an_integer_when_the_schema_declares_one(self) -> None:
        client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "integer"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id", {}, telegram_user_id=CALLER
        )

        assert client.last_arguments().get("telegramId") == CALLER

    @pytest.mark.asyncio
    async def test_should_keep_the_shape_the_model_chose_when_the_schema_omits_a_type(self) -> None:
        numeric_client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"description": "no type here"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )
        await create_router([numeric_client]).call_tool(
            "users_get_by_telegram_id",
            {"telegramId": 12345},
            telegram_user_id=CALLER,
        )
        assert numeric_client.last_arguments().get("telegramId") == CALLER

        textual_client = StubMcpClient(
            tools=[
                McpTool(
                    name="users_get_by_telegram_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"description": "no type here"}},
                    },
                )
            ],
            tool_results={"users_get_by_telegram_id": "ok"},
        )
        await create_router([textual_client]).call_tool(
            "users_get_by_telegram_id",
            {"telegramId": "12345"},
            telegram_user_id=CALLER,
        )
        assert textual_client.last_arguments().get("telegramId") == str(CALLER)

    @pytest.mark.asyncio
    async def test_should_default_to_a_string_when_nothing_says_otherwise(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="users_get_by_telegram_id", description="desc")],
            tool_results={"users_get_by_telegram_id": "ok"},
        )

        await create_router([client]).call_tool(
            "users_get_by_telegram_id",
            {"telegram_id": "1"},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments().get("telegram_id") == str(CALLER)

    @pytest.mark.asyncio
    async def test_should_leave_unrelated_arguments_untouched(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="nodes_get", description="desc")],
            tool_results={"nodes_get": "ok"},
        )

        await create_router([client]).call_tool(
            "nodes_get",
            {"uuid": "abc-123"},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments().get("uuid") == "abc-123"
        assert "telegramId" not in client.last_arguments()

    @pytest.mark.asyncio
    async def test_should_tolerate_none_arguments(self) -> None:
        client = StubMcpClient(
            tools=[McpTool(name="nodes_list", description="desc")],
            tool_results={"nodes_list": "ok"},
        )

        result = await create_router([client]).call_tool(
            "nodes_list", None, telegram_user_id=CALLER
        )

        assert result == "ok"
        assert client.last_arguments() == {}
