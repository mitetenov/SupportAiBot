"""Security gatekeeper between LLM and MCP clients."""

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.config import Settings
from app.llm.mcp_client import McpClientInterface, McpTool

logger = logging.getLogger(__name__)

READ_TOOLS: set[str] = {
    "users_get_by_telegram_id",
    "nodes_list",
    "nodes_get",
    "hwid_devices_list",
}

WRITE_TOOLS: set[str] = {
    "hwid_device_delete",
}


@dataclass(frozen=True)
class TelegramIdParam:
    """Descriptor for tool parameter carrying Telegram user ID."""

    name: str
    json_type: str | None = None


class McpRouter:
    """Gatekeeper between the LLM and the Remnawave MCP servers.

    Enforces:
    1. Only allow-listed tools are exposed and callable, with mutating tools gated behind
       readonly=False.
    2. The Telegram ID argument is always overwritten or injected with the authenticated sender ID.
    """

    def __init__(
        self,
        clients: list[McpClientInterface] | None = None,
        readonly: bool = False,
        settings: Settings | None = None,
    ) -> None:
        if settings is not None:
            readonly = settings.remnawave_mcp_readonly
        self.readonly = readonly
        self.clients: list[McpClientInterface] = list(clients) if clients is not None else []
        self.allowed_tools: set[str] = set(
            READ_TOOLS if self.readonly else (READ_TOOLS | WRITE_TOOLS)
        )
        self._tool_to_client: dict[str, McpClientInterface] = self._build_tool_to_client_map()
        self._telegram_id_param_by_tool: dict[str, TelegramIdParam] = (
            self._build_telegram_id_param_map()
        )

        if self.readonly:
            logger.info(
                "McpRouter in read-only mode: %d write tool(s) withheld from the model",
                len(WRITE_TOOLS),
            )
        logger.info(
            "McpRouter initialized with %d client(s), %d tool(s) exposed",
            len(self.clients),
            len(self.list_tools()),
        )

    def _build_tool_to_client_map(self) -> dict[str, McpClientInterface]:
        mapping: dict[str, McpClientInterface] = {}
        for client in self.clients:
            for tool in client.list_tools():
                if tool.name in self.allowed_tools and tool.name not in mapping:
                    mapping[tool.name] = client
        return mapping

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
        """Return list of allowed tools from all configured clients."""
        tools: list[McpTool] = []
        for client in self.clients:
            for tool in client.list_tools():
                if tool.name in self.allowed_tools:
                    tools.append(tool)
        return tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        telegram_user_id: int = 0,
    ) -> str:
        """Execute a tool on behalf of telegram_user_id, overriding Telegram ID arguments."""
        if tool_name not in self.allowed_tools:
            logger.warning("Blocked call to non-allowed tool: %s", tool_name)
            return json.dumps({"error": f"Tool not allowed: {tool_name}"})

        client = self._tool_to_client.get(tool_name)
        if client is None:
            logger.warning("Unknown tool requested: %s", tool_name)
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        safe_args = self._pin_telegram_id(tool_name, arguments, telegram_user_id)
        return await client.call_tool(tool_name, safe_args)

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
