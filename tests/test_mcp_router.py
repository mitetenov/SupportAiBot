"""Unit tests for McpRouter."""

import json
from typing import Any

import pytest

from app.config import Settings
from app.llm.mcp_client import McpClientInterface, McpTool
from app.llm.mcp_router import McpRouter

CALLER = 555_000
NEGATIVE_CABINET_KEY = -42


class StubMcpClient(McpClientInterface):
    """Stub implementation of McpClientInterface for testing McpRouter."""

    @property
    def server_name(self) -> str:
        return self._server_name

    @server_name.setter
    def server_name(self, value: str) -> None:
        self._server_name = value

    def __init__(
        self,
        tools: list[McpTool] | None = None,
        tool_results: dict[str, str] | None = None,
        server_name: str = "remnawave",
    ) -> None:
        self.server_name = server_name
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
    async def test_should_expose_subscription_url_lookup_and_pin_telegram_id(self) -> None:
        tool_name = "users_get_subscription_url_by_telegram_id"
        client = StubMcpClient(
            tools=[
                McpTool(
                    name=tool_name,
                    description="Return the current user's subscription URL",
                    input_schema={
                        "type": "object",
                        "properties": {"telegramId": {"type": "number"}},
                    },
                )
            ],
            tool_results={tool_name: '{"status":"not_found","subscriptionUrl":null}'},
        )
        router = create_router([client], readonly=True)

        assert [tool.name for tool in router.list_tools()] == [tool_name]

        await router.call_tool(
            tool_name,
            {"telegramId": 999_999},
            telegram_user_id=CALLER,
        )

        assert client.last_arguments() == {"telegramId": CALLER}

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


class TestOwnerBasedRouting:
    """The backend serving a call is decided by the tool's OWNER, never by name matching."""

    @pytest.mark.asyncio
    async def test_should_route_each_tool_to_its_owning_server(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="users_get_by_telegram_id", description="remnawave")],
            tool_results={"users_get_by_telegram_id": "from remnawave"},
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_user_get", description="bedolaga")],
            tool_results={"bedolaga_user_get": "from bedolaga"},
        )

        router = create_router([remnawave, bedolaga])

        assert await router.call_tool("bedolaga_user_get", {}, telegram_user_id=CALLER) == (
            "from bedolaga"
        )
        assert (
            await router.call_tool("users_get_by_telegram_id", {}, telegram_user_id=CALLER)
            == "from remnawave"
        )
        assert bedolaga.last_arguments().get("telegram_id") == CALLER

    def test_should_expose_only_the_owners_own_tools(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="users_get_by_telegram_id", description="a")],
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[
                McpTool(name="bedolaga_user_get", description="b"),
                McpTool(name="nodes_list", description="declared but not allowed for bedolaga"),
            ],
        )

        names = {tool.name for tool in create_router([remnawave, bedolaga]).list_tools()}

        assert names == {"users_get_by_telegram_id", "bedolaga_user_get"}

    @pytest.mark.asyncio
    async def test_should_block_a_tool_declared_but_not_allowed_by_its_owner(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="nodes_list", description="not in the bedolaga profile")],
            tool_results={"nodes_list": "must not be served"},
        )

        router = create_router([bedolaga])

        assert router.list_tools() == []
        result = await router.call_tool("nodes_list", {}, telegram_user_id=CALLER)
        assert "Tool not allowed" in result
        assert "must not be served" not in result
        assert bedolaga.calls == []

    def test_should_not_apply_remnawave_readonly_to_bedolaga_tools(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_billing_get", description="read-only by contract")],
        )

        names = {tool.name for tool in create_router([bedolaga], readonly=True).list_tools()}

        assert names == {"bedolaga_billing_get"}

    def test_should_hide_and_report_a_tool_name_declared_by_two_servers(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="nodes_list", description="remnawave nodes")],
            tool_results={"nodes_list": "remnawave"},
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="nodes_list", description="bedolaga copy")],
            tool_results={"nodes_list": "bedolaga"},
        )

        router = create_router([remnawave, bedolaga])

        assert router.collisions == {"nodes_list": ("bedolaga", "remnawave")}
        assert router.list_tools() == []

    @pytest.mark.asyncio
    async def test_should_block_a_hidden_colliding_tool(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="nodes_list", description="remnawave nodes")],
            tool_results={"nodes_list": "remnawave"},
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="nodes_list", description="bedolaga copy")],
            tool_results={"nodes_list": "bedolaga"},
        )

        router = create_router([remnawave, bedolaga])

        result = await router.call_tool("nodes_list", {}, telegram_user_id=CALLER)
        data = json.loads(result)
        assert "collision" in data["error"]
        assert remnawave.calls == []
        assert bedolaga.calls == []


class TestBedolagaIdentityPinning:
    """Bedolaga tools always carry the system-pinned identity, as an integer."""

    @pytest.mark.asyncio
    async def test_should_pin_the_sender_when_the_model_omits_telegram_id(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_user_get", description="desc")],
            tool_results={"bedolaga_user_get": "ok"},
        )

        await create_router([bedolaga]).call_tool("bedolaga_user_get", {}, telegram_user_id=CALLER)

        assert bedolaga.last_arguments().get("telegram_id") == CALLER

    @pytest.mark.asyncio
    async def test_should_override_a_model_supplied_telegram_id_with_the_sender(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_user_get", description="desc")],
            tool_results={"bedolaga_user_get": "ok"},
        )

        await create_router([bedolaga]).call_tool(
            "bedolaga_user_get",
            {"telegram_id": 999_999},
            telegram_user_id=CALLER,
        )

        assert bedolaga.last_arguments().get("telegram_id") == CALLER

    @pytest.mark.asyncio
    async def test_should_strip_camel_case_variant_and_inject_the_canonical_integer(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_billing_get", description="desc")],
            tool_results={"bedolaga_billing_get": "ok"},
        )

        await create_router([bedolaga]).call_tool(
            "bedolaga_billing_get",
            {"telegramId": "777", "limit": 10},
            telegram_user_id=CALLER,
        )

        assert bedolaga.last_arguments() == {"telegram_id": CALLER, "limit": 10}

    @pytest.mark.asyncio
    async def test_should_use_the_schema_declared_integer_type(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[
                McpTool(
                    name="bedolaga_referrals_get",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"telegram_id": {"type": "integer"}},
                    },
                )
            ],
            tool_results={"bedolaga_referrals_get": "ok"},
        )

        await create_router([bedolaga]).call_tool(
            "bedolaga_referrals_get",
            {"telegram_id": "999999"},
            telegram_user_id=CALLER,
        )

        assert bedolaga.last_arguments().get("telegram_id") == CALLER


class TestUserIdIsNeverOverwritten:
    """A Remnawave userId parameter must never receive the sender's Telegram ID."""

    @pytest.mark.asyncio
    async def test_should_leave_a_user_id_param_untouched(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[
                McpTool(
                    name="users_get",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"userId": {"type": "string"}},
                    },
                )
            ],
            tool_results={"users_get": "ok"},
        )

        await create_router([remnawave]).call_tool(
            "users_get",
            {"userId": "abc-123"},
            telegram_user_id=CALLER,
        )

        assert remnawave.last_arguments().get("userId") == "abc-123"

    @pytest.mark.asyncio
    async def test_should_not_put_the_sender_into_a_user_id_param_when_model_sends_both(
        self,
    ) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[
                McpTool(
                    name="subscriptions_get_by_user_id",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {"userId": {"type": "string"}},
                    },
                )
            ],
            tool_results={"subscriptions_get_by_user_id": "ok"},
        )

        await create_router([remnawave]).call_tool(
            "subscriptions_get_by_user_id",
            {"userId": "abc-123", "telegram_id": "999999"},
            telegram_user_id=CALLER,
        )

        assert remnawave.last_arguments().get("userId") == "abc-123"


class TestIdentityUnavailable:
    """Identity is chosen by the caller key's sign; a cabinet caller is served on Bedolaga only."""

    @pytest.mark.asyncio
    async def test_should_serve_bedolaga_for_a_negative_caller_key_by_pinning_user_id(
        self,
    ) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_user_get", description="desc")],
            tool_results={"bedolaga_user_get": "ok"},
        )

        result = await create_router([bedolaga]).call_tool(
            "bedolaga_user_get",
            {"telegram_id": 999_999, "userId": 777},
            telegram_user_id=NEGATIVE_CABINET_KEY,
        )

        assert result == "ok"
        assert bedolaga.last_arguments() == {"user_id": -NEGATIVE_CABINET_KEY}

    @pytest.mark.asyncio
    async def test_should_inject_user_id_under_the_schema_declared_name(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[
                McpTool(
                    name="bedolaga_billing_get",
                    description="desc",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "telegramId": {"type": "integer"},
                            "userId": {"type": "integer"},
                        },
                    },
                )
            ],
            tool_results={"bedolaga_billing_get": "ok"},
        )

        await create_router([bedolaga]).call_tool(
            "bedolaga_billing_get", {"limit": 10}, telegram_user_id=NEGATIVE_CABINET_KEY
        )

        assert bedolaga.last_arguments() == {"userId": -NEGATIVE_CABINET_KEY, "limit": 10}

    @pytest.mark.asyncio
    async def test_should_return_identity_unavailable_for_a_zero_caller_key(self) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name="bedolaga_user_get", description="desc")],
            tool_results={"bedolaga_user_get": "must not be called"},
        )

        result = await create_router([bedolaga]).call_tool(
            "bedolaga_user_get", {}, telegram_user_id=0
        )

        data = json.loads(result)
        assert data["ok"] is False
        assert data["error"]["code"] == "identity_unavailable"
        assert bedolaga.calls == []

    @pytest.mark.asyncio
    async def test_should_not_call_remnawave_user_tools_for_a_negative_caller_key(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="users_get_by_telegram_id", description="desc")],
            tool_results={"users_get_by_telegram_id": "must not be called"},
        )

        result = await create_router([remnawave]).call_tool(
            "users_get_by_telegram_id", {}, telegram_user_id=NEGATIVE_CABINET_KEY
        )

        data = json.loads(result)
        assert data["ok"] is False
        assert data["error"]["code"] == "identity_unavailable"
        assert remnawave.calls == []

    @pytest.mark.asyncio
    async def test_should_also_block_non_user_tools_for_a_negative_caller_key(self) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name="nodes_list", description="desc")],
            tool_results={"nodes_list": "must not be called"},
        )

        result = await create_router([remnawave]).call_tool(
            "nodes_list", {}, telegram_user_id=NEGATIVE_CABINET_KEY
        )

        data = json.loads(result)
        assert data["ok"] is False
        assert data["error"]["code"] == "identity_unavailable"
        assert remnawave.calls == []


ALL_EXPECTED_BEDOLAGA_TOOLS = [
    "bedolaga_user_get",
    "bedolaga_billing_get",
    "bedolaga_referrals_get",
    "bedolaga_subscription_get",
    "bedolaga_tickets_get",
    "bedolaga_payment_status_get",
    "bedolaga_promocode_check",
    "bedolaga_gifts_get",
]


class TestBedolagaSupportToolsRouter:
    """Parameterized tests for the eight Bedolaga support tools in McpRouter."""

    @pytest.mark.parametrize("tool_name", ALL_EXPECTED_BEDOLAGA_TOOLS)
    def test_tool_visible_only_from_bedolaga_server(self, tool_name: str) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name=tool_name, description="rogue declaration")],
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name=tool_name, description="legit declaration")],
        )

        router_remna = create_router([remnawave])
        assert router_remna.list_tools() == []

        router_bedo = create_router([bedolaga])
        tools = router_bedo.list_tools()
        assert len(tools) == 1
        assert tools[0].name == tool_name

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ALL_EXPECTED_BEDOLAGA_TOOLS)
    async def test_positive_caller_key_pins_telegram_id_and_strips_forged_identity(
        self, tool_name: str
    ) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name=tool_name, description="desc")],
            tool_results={tool_name: '{"ok": true}'},
        )
        router = create_router([bedolaga])
        res = await router.call_tool(
            tool_name,
            {
                "telegram_id": 999_999,
                "user_id": 888,
                "userId": 777,
                "code": "SUMMER",
                "limit": 10,
            },
            telegram_user_id=CALLER,
        )
        assert res == '{"ok": true}'
        args = bedolaga.last_arguments()
        assert args.get("telegram_id") == CALLER
        assert "user_id" not in args
        assert "userId" not in args
        if tool_name == "bedolaga_promocode_check":
            assert args.get("code") == "SUMMER"
        if tool_name in (
            "bedolaga_billing_get",
            "bedolaga_tickets_get",
            "bedolaga_payment_status_get",
            "bedolaga_gifts_get",
        ):
            assert args.get("limit") == 10

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ALL_EXPECTED_BEDOLAGA_TOOLS)
    async def test_negative_caller_key_pins_internal_user_id(self, tool_name: str) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name=tool_name, description="desc")],
            tool_results={tool_name: '{"ok": true}'},
        )
        router = create_router([bedolaga])
        res = await router.call_tool(
            tool_name,
            {"telegram_id": 999_999, "userId": 888, "code": "SUMMER"},
            telegram_user_id=NEGATIVE_CABINET_KEY,
        )
        assert res == '{"ok": true}'
        args = bedolaga.last_arguments()
        assert args.get("user_id") == -NEGATIVE_CABINET_KEY
        assert "telegram_id" not in args
        assert "userId" not in args

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ALL_EXPECTED_BEDOLAGA_TOOLS)
    async def test_zero_caller_key_returns_identity_unavailable(self, tool_name: str) -> None:
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name=tool_name, description="desc")],
            tool_results={tool_name: '{"ok": true}'},
        )
        router = create_router([bedolaga])
        res = await router.call_tool(tool_name, {}, telegram_user_id=0)
        data = json.loads(res)
        assert data["ok"] is False
        assert data["error"]["code"] == "identity_unavailable"
        assert bedolaga.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool_name", ALL_EXPECTED_BEDOLAGA_TOOLS)
    async def test_collision_hides_tool_fail_closed(self, tool_name: str) -> None:
        remnawave = StubMcpClient(
            server_name="remnawave",
            tools=[McpTool(name=tool_name, description="desc")],
            tool_results={tool_name: "remnawave"},
        )
        bedolaga = StubMcpClient(
            server_name="bedolaga",
            tools=[McpTool(name=tool_name, description="desc")],
            tool_results={tool_name: "bedolaga"},
        )
        router = create_router([remnawave, bedolaga])
        assert router.list_tools() == []
        res = await router.call_tool(tool_name, {}, telegram_user_id=CALLER)
        data = json.loads(res)
        assert "collision" in data["error"]
        assert remnawave.calls == []
        assert bedolaga.calls == []


class TestMcpRouterTraceLogging:
    """Verify TRACE level logging in McpRouter."""

    @pytest.mark.asyncio
    async def test_trace_logging_identity_pinning_and_dispatch(self) -> None:
        import logging

        from app.logging_config import TRACE

        router_logger = logging.getLogger("app.llm.mcp_router")
        router_logger.setLevel(TRACE)
        records: list[logging.LogRecord] = []

        class TestHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = TestHandler()
        router_logger.addHandler(handler)

        try:
            bedolaga = StubMcpClient(
                server_name="bedolaga",
                tools=[McpTool(name="bedolaga_user_get", description="desc")],
                tool_results={"bedolaga_user_get": '{"ok": true}'},
            )
            router = create_router([bedolaga])

            await router.call_tool(
                "bedolaga_user_get",
                {"telegram_id": 999_999, "extra": "data"},
                telegram_user_id=CALLER,
            )

            trace_msgs = [r.getMessage() for r in records if r.levelno == TRACE]
            assert any("stripping model identity arg" in m for m in trace_msgs)
            assert any("pinned Bedolaga identity for bedolaga_user_get" in m for m in trace_msgs)
            assert any(
                "dispatching tool 'bedolaga_user_get' to client 'bedolaga'" in m for m in trace_msgs
            )
            assert any("result from client 'bedolaga'" in m for m in trace_msgs)
        finally:
            router_logger.removeHandler(handler)

    @pytest.mark.asyncio
    async def test_trace_logging_blocked_call_zero_caller_key(self) -> None:
        import logging

        from app.logging_config import TRACE

        router_logger = logging.getLogger("app.llm.mcp_router")
        router_logger.setLevel(TRACE)
        records: list[logging.LogRecord] = []

        class TestHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = TestHandler()
        router_logger.addHandler(handler)

        try:
            bedolaga = StubMcpClient(
                server_name="bedolaga",
                tools=[McpTool(name="bedolaga_user_get", description="desc")],
                tool_results={"bedolaga_user_get": '{"ok": true}'},
            )
            router = create_router([bedolaga])

            await router.call_tool("bedolaga_user_get", {}, telegram_user_id=0)

            trace_msgs = [r.getMessage() for r in records if r.levelno == TRACE]
            assert any("caller key is 0" in m for m in trace_msgs)
        finally:
            router_logger.removeHandler(handler)
