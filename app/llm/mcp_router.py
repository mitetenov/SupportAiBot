"""Security gatekeeper between LLM and MCP clients.

The router is OWNER-based: every tool is bound to the server that declared it,
and the model may only call a tool when (a) its owner declared it and (b) the
owner's allowlist profile allows it. A name alone can no longer smuggle a call
to the wrong backend, and a tool one server declared but another did not is
never silently served by the first match.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm.mcp_client import McpClientInterface, McpTool

logger = logging.getLogger(__name__)

#: Stable server names the bot may hold clients for. Allowlists are selected by
#: these names; an unrecognised owner gets no tools at all (fail-closed).
SERVER_REMNAWAVE = "remnawave"
SERVER_BEDOLAGA = "bedolaga"

#: Remnawave tools that read panel state. Exact, fixed set — a Remnawave tool
#: not listed here is never exposed, no matter what the server declares.
REMWNAWAVE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "users_get_by_telegram_id",
        "users_get",
        "subscriptions_get_by_user_id",
        "users_accessible_nodes",
        "bandwidth_user_usage",
        "hwid_devices_list",
        "nodes_list",
        "nodes_get",
    }
)

#: Remnawave tools that mutate panel state. Only exposed when the bot is run
#: with remnawave_mcp_readonly=False.
REMWNAWAVE_WRITE_TOOLS: frozenset[str] = frozenset({"hwid_device_delete"})

#: Bedolaga tools the model may call. Read-only by construction; the Remnawave
#: readonly setting never applies to them, and there are no Bedolaga writes.
BEDOLAGA_READ_TOOLS: frozenset[str] = frozenset(
    {"bedolaga_user_get", "bedolaga_billing_get", "bedolaga_referrals_get"}
)


@dataclass(frozen=True)
class TelegramIdParam:
    """Descriptor for tool parameter carrying the Telegram user ID."""

    name: str
    json_type: str | None = None


@dataclass(frozen=True)
class _Route:
    """Where one (owner, tool) call goes, and how its identity is pinned."""

    client: McpClientInterface
    owner: str
    telegram_id_param: TelegramIdParam | None


class McpRouter:
    """Gatekeeper between the LLM and the per-owner MCP clients.

    Enforces:
    1. Only tools declared by their owner AND listed in the owner's allowlist
       profile are exposed and callable; mutating Remnawave tools are gated
       behind readonly=False. A tool name declared by more than one server is
       hidden outright and reported to the administrator — never "first wins".
    2. The Telegram ID argument is always overwritten or injected with the
       authenticated sender ID, never trusted from the model.
    3. A non-positive caller key (email-only Bedolaga ticket) is never sent to
       any user-by-Telegram-ID tool: the call returns ``identity_unavailable``.
    """

    def __init__(
        self,
        clients: list[McpClientInterface] | None = None,
        readonly: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        # An explicit readonly= wins; settings only supply the default. The other
        # way round, passing both silently discarded the caller's argument.
        if readonly is None:
            readonly = settings.remnawave_mcp_readonly if settings is not None else False
        self.readonly = readonly
        self.clients: list[McpClientInterface] = list(clients) if clients is not None else []

        remnawave_allowed = set(REMWNAWAVE_READ_TOOLS)
        if not self.readonly:
            remnawave_allowed |= set(REMWNAWAVE_WRITE_TOOLS)
        self._profiles: dict[str, frozenset[str]] = {
            SERVER_REMNAWAVE: frozenset(remnawave_allowed),
            SERVER_BEDOLAGA: BEDOLAGA_READ_TOOLS,
        }

        #: tool name -> owners that declared it, when more than one did. The
        #: caller (main.py) reads this at startup to alert the administrators;
        #: the name is hidden from the model either way.
        self.collisions: dict[str, tuple[str, ...]] = {}

        # (server_name, tool_name) -> route; also the reverse by plain name,
        # which is unambiguous because colliding names are hidden.
        self._routes: dict[tuple[str, str], _Route] = {}
        self._route_by_tool_name: dict[str, _Route] = {}
        #: owner -> tool names the model may call, post-profile, post-collision.
        self.allowed_tools_by_server: dict[str, set[str]] = {name: set() for name in self._profiles}
        self._build_routes()

        # Flat union of everything the model sees, kept for the composition
        # root's per-server checks (which use allowed_tools_by_server).
        self.allowed_tools: set[str] = (
            set().union(*self.allowed_tools_by_server.values())
            if self.allowed_tools_by_server
            else set()
        )

        if self.readonly:
            logger.info(
                "McpRouter in read-only mode: %d Remnawave write tool(s) withheld from the model",
                len(REMWNAWAVE_WRITE_TOOLS),
            )
        logger.info(
            "McpRouter initialized with %d client(s), %d tool(s) exposed",
            len(self.clients),
            len(self.allowed_tools),
        )

    def _build_routes(self) -> None:
        """Bind every allowed, non-colliding (owner, tool) to exactly one client.

        Fail-closed in both directions: a tool whose owner has no profile is
        ignored, and a tool its owner did not allow is never routable even
        though the server declared it.
        """
        declared: dict[str, dict[str, tuple[McpTool, McpClientInterface]]] = {}
        for client in self.clients:
            server_name = getattr(client, "server_name", None)
            if not isinstance(server_name, str) or not server_name:
                logger.error("MCP client without a stable server_name is ignored by the router")
                continue
            server_tools = declared.setdefault(server_name, {})
            for tool in client.list_tools():
                server_tools[tool.name] = (tool, client)

        # A name declared by more than one server is hidden outright — never the
        # first match. Same-owner duplicate declarations merely overwrite (the
        # backend is still unambiguous).
        declared_by_name: dict[str, set[str]] = {}
        for server_name, server_tools in declared.items():
            for name in server_tools:
                declared_by_name.setdefault(name, set()).add(server_name)
        for name, owners in declared_by_name.items():
            if len(owners) > 1:
                self.collisions[name] = tuple(sorted(owners))
                logger.error(
                    "MCP tool name collision: '%s' is declared by %s — hiding it from the model",
                    name,
                    ", ".join(sorted(owners)),
                )

        hidden = set(self.collisions)
        for server_name, server_tools in declared.items():
            profile = self._profiles.get(server_name)
            if profile is None:
                logger.error(
                    "MCP client server_name=%r has no allowlist profile; none of its tools "
                    "are exposed",
                    server_name,
                )
                continue
            for tool_name, (tool, client) in server_tools.items():
                if tool_name in hidden:
                    continue
                if tool_name not in profile:
                    logger.info(
                        "Tool '%s' declared by %s is not in its owner's profile — withheld",
                        tool_name,
                        server_name,
                    )
                    continue
                telegram_param = self._telegram_id_property(tool.input_schema)
                if server_name == SERVER_BEDOLAGA and telegram_param is None:
                    # Bedolaga tools always require the sender's Telegram ID in
                    # integer form; the schema may omit it, the contract never does.
                    telegram_param = TelegramIdParam(name="telegram_id", json_type="integer")
                route = _Route(
                    client=client,
                    owner=server_name,
                    telegram_id_param=telegram_param,
                )
                self._routes[(server_name, tool_name)] = route
                self._route_by_tool_name[tool_name] = route
                self.allowed_tools_by_server[server_name].add(tool_name)

    @classmethod
    def _telegram_id_property(cls, input_schema: dict[str, Any] | None) -> TelegramIdParam | None:
        if not input_schema or not isinstance(input_schema, dict):
            return None
        properties = input_schema.get("properties")
        if not properties or not isinstance(properties, dict):
            return None
        for key, prop_schema in properties.items():
            if cls._is_telegram_id_arg(key):
                json_type = (
                    prop_schema.get("type")
                    if isinstance(prop_schema, dict) and isinstance(prop_schema.get("type"), str)
                    else None
                )
                return TelegramIdParam(name=key, json_type=json_type)
        return None

    @staticmethod
    def _is_telegram_id_arg(key: str | None) -> bool:
        return bool(key and key.replace("_", "").lower() == "telegramid")

    def list_tools(self) -> list[McpTool]:
        """Return allowed tools from all configured clients.

        Order follows the clients and their tool order, so the model's tool
        menu is stable. Hidden collision names and tools outside their owner's
        profile are excluded.
        """
        tools: list[McpTool] = []
        for client in self.clients:
            server_name = getattr(client, "server_name", None)
            if not isinstance(server_name, str):
                continue
            allowed = self.allowed_tools_by_server.get(server_name, set())
            for tool in client.list_tools():
                if tool.name in allowed:
                    tools.append(tool)
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        telegram_user_id: int = 0,
    ) -> str:
        """Execute a tool on behalf of telegram_user_id, overriding Telegram ID arguments.

        A non-positive caller key is an email-only Bedolaga ticket whose owner
        has no Telegram identity: it must never be sent to any user-by-Telegram-ID
        tool, so every call for such a key returns ``identity_unavailable``.
        """
        if telegram_user_id <= 0:
            logger.warning(
                "Blocked call to %s: no real Telegram identity for key %d",
                tool_name,
                telegram_user_id,
            )
            return self._identity_unavailable(tool_name)

        route = self._route_for(tool_name)
        if route is None:
            if tool_name in self.collisions:
                logger.warning("Blocked call to hidden colliding tool: %s", tool_name)
                return json.dumps({"error": f"Tool hidden due to name collision: {tool_name}"})
            if tool_name in self.allowed_tools:
                logger.error(
                    "Tool %s is allowed but has no route — routing table is inconsistent",
                    tool_name,
                )
                return json.dumps({"error": f"Unknown tool: {tool_name}"})
            logger.warning("Blocked call to non-allowed tool: %s", tool_name)
            return json.dumps({"error": f"Tool not allowed: {tool_name}"})

        safe_args = self._pin_telegram_id(route, tool_name, arguments, telegram_user_id)
        return await route.client.call_tool(tool_name, safe_args)

    def _route_for(self, tool_name: str) -> _Route | None:
        return self._route_by_tool_name.get(tool_name)

    def _pin_telegram_id(
        self,
        route: _Route,
        tool_name: str,
        arguments: dict[str, Any] | None,
        telegram_user_id: int,
    ) -> dict[str, Any]:
        """Return arguments with the sender's Telegram ID pinned, never the model's.

        Bedolaga tools: every telegram_id/telegramId variant the model supplied
        is removed and the canonical parameter carries the sender as an integer.
        Remnawave tools: the telegram-typed parameter (``users_get_by_telegram_id``)
        is overwritten in the schema's declared type; a stray variant on a tool
        without one keeps the shape the model chose, and a ``userId`` parameter
        is never touched.
        """
        safe: dict[str, Any] = dict(arguments) if arguments is not None else {}
        telegram_param = route.telegram_id_param

        if route.owner == SERVER_BEDOLAGA:
            for key in list(safe.keys()):
                if self._is_telegram_id_arg(key):
                    logger.warning(
                        "Tool %s called with %s=%s — stripping; injecting actual sender %s",
                        tool_name,
                        key,
                        safe[key],
                        telegram_user_id,
                    )
                    del safe[key]
            name = telegram_param.name if telegram_param is not None else "telegram_id"
            safe[name] = self._coerce(telegram_user_id, telegram_param, None)
            return safe

        for key in list(safe.keys()):
            if not self._is_telegram_id_arg(key):
                continue
            supplied = safe[key]
            if not self._matches_user(supplied, telegram_user_id):
                logger.warning(
                    "Tool %s called with %s=%s — overriding with actual sender %s",
                    tool_name,
                    key,
                    supplied,
                    telegram_user_id,
                )
            if telegram_param is not None and key == telegram_param.name:
                safe[key] = self._coerce(telegram_user_id, telegram_param, supplied)
            else:
                safe[key] = self._coerce(telegram_user_id, None, supplied)

        if telegram_param is not None and telegram_param.name not in safe:
            safe[telegram_param.name] = self._coerce(telegram_user_id, telegram_param, None)

        return safe

    @staticmethod
    def _coerce(telegram_user_id: int, param: TelegramIdParam | None, supplied: Any) -> Any:
        json_type = param.json_type if param is not None else None
        if json_type is not None:
            if json_type == "string":
                return str(telegram_user_id)
            if json_type in ("number", "integer"):
                return telegram_user_id
            return str(telegram_user_id)

        if isinstance(supplied, (int, float)) and not isinstance(supplied, bool):
            return telegram_user_id
        return str(telegram_user_id)

    @staticmethod
    def _matches_user(supplied: Any, telegram_user_id: int) -> bool:
        if isinstance(supplied, (int, float)) and not isinstance(supplied, bool):
            return int(supplied) == telegram_user_id
        if supplied is not None:
            return str(telegram_user_id) == str(supplied).strip()
        return False

    @staticmethod
    def _identity_unavailable(tool_name: str) -> str:
        """Envelope for a conversation with no real Telegram identity.

        A JSON string fed back to the model, in the same ``ok=false`` shape the
        MCP servers themselves use, so the model can tell the user it cannot
        look them up rather than inventing a user or id.
        """
        return json.dumps(
            {
                "ok": False,
                "source": "mcp",
                "tool": tool_name,
                "error": {
                    "code": "identity_unavailable",
                    "message": "Идентификация пользователя недоступна: у этого "
                    "разговора нет Telegram-аккаунта.",
                    "retryable": False,
                },
            },
            ensure_ascii=False,
        )
