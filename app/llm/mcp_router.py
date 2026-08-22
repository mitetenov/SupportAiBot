"""Security gatekeeper between LLM and MCP clients."""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm.mcp_client import McpClientInterface, McpTool

logger = logging.getLogger(__name__)

#: Remnawave read profile. Only these panel tools reach the model.
REMNAWAVE_READ_TOOLS: set[str] = {
    "users_get_by_telegram_id",
    "users_get",
    "subscriptions_get_by_user_id",
    "users_accessible_nodes",
    "bandwidth_user_usage",
    "hwid_devices_list",
    "nodes_list",
    "nodes_get",
}

#: Remnawave write profile; gated behind readonly.
REMNAWAVE_WRITE_TOOLS: set[str] = {
    "hwid_device_delete",
}

#: Bedolaga read profile. No Bedolaga write tools exist; the set is fixed.
BEDOLAGA_READ_TOOLS: set[str] = {
    "bedolaga_user_get",
    "bedolaga_billing_get",
    "bedolaga_referrals_get",
}

#: Backwards-compatible aliases (module-level consumers still read these names).
READ_TOOLS: set[str] = REMNAWAVE_READ_TOOLS
WRITE_TOOLS: set[str] = REMNAWAVE_WRITE_TOOLS

#: Tool arguments that must never be trusted from the model on Bedolaga tools.
BEDOLAGA_IDENTITY_KEYS: frozenset[str] = frozenset(
    {"telegram_id", "telegramId", "user_id", "userId"}
)


@dataclass(frozen=True)
class TelegramIdParam:
    """Descriptor for tool parameter carrying Telegram user ID."""

    name: str
    json_type: str | None = None


class McpRouter:
    """Gatekeeper between the LLM and the Remnawave/Bedolaga MCP servers.

    Enforces:
    1. Fail-closed per-owner allowlists: a tool is callable only when its
       owner's profile permits it. A name declared by two different servers is
       hidden entirely and recorded in ``collisions``.
    2. Identity pinning: any Telegram/user id the model supplies is discarded
       and replaced with the authenticated sender. Telegram senders pin
       ``telegram_id``; email-only cabinet tickets (negative conversation key)
       pin ``user_id = abs(key)`` on Bedolaga tools and are refused on
       Remnawave user-by-Telegram-ID lookups.
    """

    def __init__(
        self,
        clients: Sequence[McpClientInterface] | None = None,
        readonly: bool | None = None,
        settings: Settings | None = None,
    ) -> None:
        # An explicit readonly= wins; settings only supply the default. The other
        # way round, passing both silently discarded the caller's argument.
        if readonly is None:
            readonly = settings.remnawave_mcp_readonly if settings is not None else False
        self.readonly = readonly
        self.clients: list[McpClientInterface] = list(clients) if clients is not None else []

        self.allowed_by_server: dict[str, set[str]] = {
            "remnawave": REMNAWAVE_READ_TOOLS
            | (REMNAWAVE_WRITE_TOOLS if not self.readonly else set()),
            "bedolaga": BEDOLAGA_READ_TOOLS,
        }

        #: tool name -> servers that declared it; a name with more than one
        #: owner is a collision and stays hidden. Inspected by main() to notify.
        self.collisions: dict[str, set[str]] = {}
        self._tool_to_client: dict[str, McpClientInterface] = {}
        self._owner_by_tool: dict[str, str] = {}
        self._build_tool_to_client_map()
        self._telegram_id_param_by_tool: dict[str, TelegramIdParam] = (
            self._build_telegram_id_param_map()
        )

        if self.readonly:
            logger.info(
                "McpRouter in read-only mode: %d Remnawave write tool(s) withheld from the model",
                len(REMNAWAVE_WRITE_TOOLS),
            )
        logger.info(
            "McpRouter initialized with %d client(s), %d tool(s) exposed",
            len(self.clients),
            len(self.list_tools()),
        )

    def _build_tool_to_client_map(self) -> None:
        declared_by: dict[str, set[str]] = {}
        for client in self.clients:
            for tool in client.list_tools():
                declared_by.setdefault(tool.name, set()).add(client.server_name)

        self.collisions = {
            name: servers for name, servers in declared_by.items() if len(servers) > 1
        }
        for name, servers in self.collisions.items():
            logger.error(
                "MCP tool name collision across servers %s: '%s' hidden fail-closed",
                sorted(servers),
                name,
            )

        for client in self.clients:
            allowed = self.allowed_by_server.get(client.server_name, set())
            for tool in client.list_tools():
                if tool.name not in allowed:
                    continue
                if tool.name in self.collisions:
                    continue
                if tool.name in self._tool_to_client:
                    # Same name from a second client of the same server: keep the first.
                    continue
                self._tool_to_client[tool.name] = client
                self._owner_by_tool[tool.name] = client.server_name

    def _build_telegram_id_param_map(self) -> dict[str, TelegramIdParam]:
        mapping: dict[str, TelegramIdParam] = {}
        for tool in self.list_tools():
            param = self._telegram_id_property(tool.input_schema)
            if param is not None and tool.name not in mapping:
                mapping[tool.name] = param
        return mapping

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
        """Return the allowed, de-duplicated tool list across all clients."""
        tools: list[McpTool] = []
        for client in self.clients:
            for tool in client.list_tools():
                if self._owner_by_tool.get(tool.name) == client.server_name:
                    tools.append(tool)
        return tools

    def allowed_tools_for(self, server_name: str) -> list[McpTool]:
        """Tools exposed in the router that belong to the given server."""
        return [
            tool
            for client in self.clients
            if client.server_name == server_name
            for tool in client.list_tools()
            if self._owner_by_tool.get(tool.name) == server_name
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        telegram_user_id: int = 0,
    ) -> str:
        """Execute a tool on behalf of the authenticated sender.

        ``telegram_user_id`` is the positive Telegram id of a chat user, or the
        negative synthetic key of an email-only cabinet ticket. The identity is
        always pinned here and never trusted from the model's arguments.
        """
        owner = self._owner_by_tool.get(tool_name)
        if owner is None:
            logger.warning("Blocked call to non-allowed tool: %s", tool_name)
            return json.dumps({"error": f"Tool not allowed: {tool_name}"})

        client = self._tool_to_client[tool_name]

        if owner == "bedolaga":
            safe_args = self._pin_bedolaga_identity(arguments, telegram_user_id)
            if safe_args is None:
                return json.dumps(
                    {
                        "error": "identity unavailable",
                        "code": "identity_unavailable",
                        "retryable": False,
                    }
                )
            return await client.call_tool(tool_name, safe_args)

        safe_args = self._pin_remnawave_identity(tool_name, arguments, telegram_user_id)
        if safe_args is None:
            return json.dumps(
                {
                    "error": (
                        "identity unavailable: для аккаунта без Telegram нельзя "
                        "обращаться к инструментам по Telegram ID"
                    ),
                    "code": "identity_unavailable",
                    "retryable": False,
                }
            )
        return await client.call_tool(tool_name, safe_args)

    def _pin_bedolaga_identity(
        self,
        arguments: dict[str, Any] | None,
        telegram_user_id: int,
    ) -> dict[str, Any] | None:
        """Strip any identity the model supplied and pin the actual sender.

        Bedolaga tools are read-only regardless of ``readonly``. A positive
        sender id pins ``telegram_id``; a negative cabinet key pins
        ``user_id = abs(key)``; zero (no identity) yields None.
        """
        safe: dict[str, Any] = dict(arguments) if arguments is not None else {}
        for key in list(safe.keys()):
            if key in BEDOLAGA_IDENTITY_KEYS:
                safe.pop(key)

        if telegram_user_id > 0:
            safe["telegram_id"] = int(telegram_user_id)
        elif telegram_user_id < 0:
            safe["user_id"] = abs(int(telegram_user_id))
        else:
            logger.warning("Bedolaga tool called with no available identity")
            return None
        return safe

    def _pin_remnawave_identity(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        telegram_user_id: int,
    ) -> dict[str, Any] | None:
        """Remnawave pinning; refuses Telegram-ID tools for email-only tickets."""
        schema_param = self._telegram_id_param_by_tool.get(tool_name)
        if schema_param is not None and telegram_user_id <= 0:
            logger.warning(
                "Remnawave tool %s needs a Telegram identity but none is available", tool_name
            )
            return None
        return self._pin_telegram_id(tool_name, arguments, telegram_user_id)

    def _pin_telegram_id(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        telegram_user_id: int,
    ) -> dict[str, Any]:
        safe: dict[str, Any] = dict(arguments) if arguments is not None else {}
        schema_param = self._telegram_id_param_by_tool.get(tool_name)

        for key in list(safe.keys()):
            if self._is_telegram_id_arg(key):
                supplied = safe[key]
                if not self._matches_user(supplied, telegram_user_id):
                    logger.warning(
                        "Tool %s called with %s=%s — overriding with actual sender %s",
                        tool_name,
                        key,
                        supplied,
                        telegram_user_id,
                    )
                safe[key] = self._coerce(telegram_user_id, schema_param, supplied)

        if schema_param is not None and schema_param.name not in safe:
            safe[schema_param.name] = self._coerce(telegram_user_id, schema_param, None)

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
