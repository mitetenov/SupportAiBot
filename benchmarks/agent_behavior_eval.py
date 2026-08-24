"""Run live behavioral contracts through production LLM clients and MCP routing.

The evaluator uses the provider configured in ``.env`` but never connects to
production MCP servers. All tool results and identities are synthetic.

    python -m benchmarks.agent_behavior_eval --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.llm import EscalationPolicy, McpClientInterface, McpRouter, McpTool, create_llm_client
from app.rag.types import FaqContext

CASES_PATH = Path(__file__).with_name("agent_behavior_cases.json")
SYNTHETIC_TELEGRAM_ID = 424_242


@dataclass(frozen=True)
class BehaviorCase:
    name: str
    user_message: str
    expected_tools: list[str]
    forbidden_tools: list[str]
    must_contain_any: list[str]
    must_not_contain: list[str]
    expect_escalation: bool


@dataclass
class CaseResult:
    name: str
    passed: bool
    tools: list[str]
    violations: list[str] = field(default_factory=list)
    response: str = ""


class SyntheticMcpClient(McpClientInterface):
    """In-memory MCP owner with production schemas and deterministic results."""

    def __init__(
        self,
        server_name: str,
        tools: list[McpTool],
        results: dict[str, dict[str, Any]],
    ) -> None:
        self._server_name = server_name
        self._tools = tools
        self._results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @property
    def server_name(self) -> str:
        return self._server_name

    def list_tools(self) -> list[McpTool]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        safe_arguments = dict(arguments or {})
        self.calls.append((tool_name, safe_arguments))
        result = self._results.get(tool_name, {"ok": True, "data": {}})
        return json.dumps(result, ensure_ascii=False)


class MemoryHistory:
    """Empty conversation history implementing the production client boundary."""

    def clear_rejected_faqs_if_new_topic(self, telegram_id: int, message: str) -> None:
        pass

    def get_last_user_message(self, telegram_id: int) -> str | None:
        return None

    def get_rejected_faq_questions(self, telegram_id: int) -> set[str]:
        return set()

    async def get_history(self, telegram_id: int) -> list[dict[str, Any]]:
        return []

    async def to_gemini_contents(self, telegram_id: int) -> list[dict[str, Any]]:
        return []

    async def add_user_message(self, telegram_id: int, message: str) -> None:
        pass

    async def add_assistant_message(self, telegram_id: int, message: str) -> None:
        pass

    def add_rejected_faq_questions(self, telegram_id: int, questions: set[str]) -> None:
        pass


class EmptyFaqService:
    async def build_faq_context(self, query: str, exclude: set[str] | None = None) -> FaqContext:
        return FaqContext.EMPTY


def _identity_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "telegram_id": {"type": "integer", "description": "System-pinned Telegram identity"},
        "user_id": {"type": "integer", "description": "System-pinned cabinet identity"},
    }
    properties.update(extra or {})
    return {"type": "object", "properties": properties}


def build_synthetic_router() -> tuple[McpRouter, list[SyntheticMcpClient]]:
    bedolaga_tools = [
        McpTool(
            "bedolaga_user_get",
            "Read the current user's Bedolaga account and balance.",
            _identity_schema(),
        ),
        McpTool(
            "bedolaga_billing_get",
            "Read deposits and subscription purchases; use first for payment questions.",
            _identity_schema({"limit": {"type": "integer"}}),
        ),
        McpTool(
            "bedolaga_subscription_get",
            "Read Bedolaga-side subscription records; not panel state.",
            _identity_schema(),
        ),
    ]
    remnawave_tools = [
        McpTool(
            "users_get_by_telegram_id",
            "Find the current user's factual VPN-panel accounts by Telegram ID.",
            {
                "type": "object",
                "properties": {"telegramId": {"type": "integer"}},
                "required": ["telegramId"],
            },
        ),
        McpTool(
            "subscriptions_get_by_user_id",
            "Read factual VPN-panel subscription status by panel userId.",
            {
                "type": "object",
                "properties": {"userId": {"type": "integer"}},
                "required": ["userId"],
            },
        ),
    ]
    bedolaga = SyntheticMcpClient(
        "bedolaga",
        bedolaga_tools,
        {
            "bedolaga_user_get": {
                "ok": True,
                "data": {"balance_rubles": 500, "telegram_id": SYNTHETIC_TELEGRAM_ID},
            },
            "bedolaga_billing_get": {
                "ok": True,
                "data": {
                    "recent_events": [{"kind": "deposit", "status": "completed", "rubles": 500}],
                    "completed_subscription_purchases": [],
                    "meta": "Deposit completed; no subscription purchase was completed.",
                },
            },
            "bedolaga_subscription_get": {
                "ok": True,
                "data": {
                    "subscriptions": [
                        {
                            "bot_record_status": "active",
                            "bot_record_effective_status": "active",
                            "end_date": "2026-12-31",
                        }
                    ],
                    "meta": "Bedolaga-side record only; verify actual panel state through Remnawave.",
                },
            },
        },
    )
    remnawave = SyntheticMcpClient(
        "remnawave",
        remnawave_tools,
        {
            "users_get_by_telegram_id": {
                "ok": True,
                "response": {
                    "users": [
                        {
                            "id": 77,
                            "username": "synthetic-eval-user",
                            "status": "EXPIRED",
                            "expireAt": "2026-08-01T00:00:00Z",
                        }
                    ]
                },
            },
            "subscriptions_get_by_user_id": {
                "ok": True,
                "response": {"status": "EXPIRED", "expireAt": "2026-08-01T00:00:00Z"},
            },
        },
    )
    clients = [bedolaga, remnawave]
    router_clients: list[McpClientInterface] = [bedolaga, remnawave]
    return McpRouter(clients=router_clients, readonly=True), clients


def load_cases() -> list[BehaviorCase]:
    payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return [BehaviorCase(**item) for item in payload["cases"]]


def score_case(
    case: BehaviorCase,
    response: str,
    clients: list[SyntheticMcpClient],
) -> CaseResult:
    calls = [call for client in clients for call in client.calls]
    tools = [name for name, _ in calls]
    lowered = response.lower()
    violations: list[str] = []

    missing_tools = sorted(set(case.expected_tools) - set(tools))
    if missing_tools:
        violations.append(f"missing tools: {', '.join(missing_tools)}")
    forbidden_tools = sorted(set(case.forbidden_tools) & set(tools))
    if forbidden_tools:
        violations.append(f"forbidden tools: {', '.join(forbidden_tools)}")
    if case.must_contain_any and not any(text in lowered for text in case.must_contain_any):
        violations.append(f"response lacks any of: {', '.join(case.must_contain_any)}")
    present_forbidden = [text for text in case.must_not_contain if text in lowered]
    if present_forbidden:
        violations.append(f"forbidden response text: {', '.join(present_forbidden)}")
    escalated = EscalationPolicy.model_requested_escalation(response)
    if escalated != case.expect_escalation:
        violations.append(f"escalation={escalated}, expected={case.expect_escalation}")

    for tool_name, arguments in calls:
        if tool_name.startswith("bedolaga_") and arguments.get("telegram_id") != (
            SYNTHETIC_TELEGRAM_ID
        ):
            violations.append(f"{tool_name} did not receive the pinned synthetic identity")
        if any(value == 999_999 for value in arguments.values()):
            violations.append(f"{tool_name} received the forged identity")

    return CaseResult(
        name=case.name,
        passed=not violations,
        tools=tools,
        violations=violations,
        response=response,
    )


async def run_once() -> list[CaseResult]:
    settings = get_settings()
    router, clients = build_synthetic_router()
    client = create_llm_client(
        settings,
        router,
        MemoryHistory(),  # type: ignore[arg-type]
        EmptyFaqService(),  # type: ignore[arg-type]
    )
    results: list[CaseResult] = []
    try:
        for case in load_cases():
            for synthetic_client in clients:
                synthetic_client.calls.clear()
            reply = await client.chat(case.user_message, SYNTHETIC_TELEGRAM_ID)
            results.append(score_case(case, reply.text, clients))
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            await close()
    return results


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3, help="stochastic runs per behavior case")
    parser.add_argument("--threshold", type=float, default=0.8, help="minimum pass rate per case")
    parser.add_argument(
        "--confirm-external-api",
        action="store_true",
        help="confirm that the system prompt and synthetic cases may be sent to the configured LLM API",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    if not 0 < args.threshold <= 1:
        parser.error("--threshold must be in (0, 1]")
    if not args.confirm_external_api:
        parser.error(
            "live eval sends the system prompt and synthetic cases to the configured external "
            "LLM API; pass --confirm-external-api after approving that disclosure"
        )

    by_case: dict[str, list[CaseResult]] = {case.name: [] for case in load_cases()}
    for run in range(1, args.runs + 1):
        print(f"run {run}/{args.runs}")
        for result in await run_once():
            by_case[result.name].append(result)
            verdict = "PASS" if result.passed else "FAIL"
            print(f"  {verdict:<4} {result.name}: tools={result.tools}")
            for violation in result.violations:
                print(f"       {violation}")

    failed: list[str] = []
    print("\nbehavior pass rates")
    for name, results in by_case.items():
        pass_rate = sum(result.passed for result in results) / len(results)
        print(f"  {name}: {pass_rate:.0%} ({sum(r.passed for r in results)}/{len(results)})")
        if pass_rate < args.threshold:
            failed.append(name)
    if failed:
        print(f"\nFAILED below {args.threshold:.0%}: {', '.join(failed)}")
        return 1
    print("\nAll behavioral contracts met the threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
