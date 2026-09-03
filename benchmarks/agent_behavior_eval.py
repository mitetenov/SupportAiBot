"""Run live behavioral contracts through production LLM clients and MCP routing.

The evaluator uses the provider configured in ``.env`` but never connects to
production MCP servers. All tool results and identities are synthetic.

    python -m benchmarks.agent_behavior_eval --runs 3 --threshold 0.8 --confirm-external-api
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.llm import (
    EscalationPolicy,
    McpClientInterface,
    McpRouter,
    McpTool,
    create_llm_client,
    is_rejection,
)
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqContext, FaqResult

BENCHMARK_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BENCHMARK_DIR.parent
CASES_PATH = BENCHMARK_DIR / "agent_behavior_cases.json"
FAQ_PATH = PROJECT_ROOT / "faq" / "faq.json"
SYNTHETIC_TELEGRAM_ID = 424_242

BILLING_META_NOTE = (
    "deposit = a balance credit (money added to the balance), not a purchase. "
    "Only a completed subscription_payment confirms a purchase. bot_record_status "
    "is the bot-side purchase record, not the VPN panel status; verify the actual "
    "panel state via the separate Remnawave MCP."
)
BOT_RECORD_NOTE = (
    "This is the bot-side Bedolaga purchase record, not the VPN panel status. "
    "Verify the actual panel state via the separate Remnawave MCP."
)
SUBSCRIPTION_META_NOTE = "Bedolaga subscription records are not the VPN panel status."


@dataclass(frozen=True)
class BehaviorCase:
    name: str
    user_message: str
    expected_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    must_contain_any: list[str] = field(default_factory=list)
    must_not_contain: list[str] = field(default_factory=list)
    expect_escalation: bool = False
    history: list[dict[str, Any]] = field(default_factory=list)
    tool_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    faq_candidates: list[str] = field(default_factory=list)
    expected_tool_order: list[str] = field(default_factory=list)
    require_clarification: bool = False
    max_questions: int | None = None
    expect_no_tools: bool = False
    require_two_step_payment: bool = False
    reject_premature_advice: bool = False
    require_scoped_user_not_found: bool = False
    reject_absent_payment_on_error: bool = False


@dataclass
class CaseResult:
    name: str
    passed: bool
    tools: list[str]
    violations: list[str] = field(default_factory=list)
    response: str = ""


class SyntheticMcpClient(McpClientInterface):
    """In-memory MCP owner with production schemas, envelopes, and shared chronological tracing."""

    def __init__(
        self,
        server_name: str,
        tools: list[McpTool],
        results: dict[str, dict[str, Any]],
        trace: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        self._server_name = server_name
        self._tools = tools
        self._results = dict(results)
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.trace: list[tuple[str, dict[str, Any]]] = trace if trace is not None else []

    @property
    def server_name(self) -> str:
        return self._server_name

    def list_tools(self) -> list[McpTool]:
        return self._tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        safe_arguments = dict(arguments or {})
        self.calls.append((tool_name, safe_arguments))
        self.trace.append((tool_name, safe_arguments))
        result = self._results.get(
            tool_name,
            {
                "ok": True,
                "source": self._server_name,
                "tool": tool_name,
                "data": {},
            },
        )
        return json.dumps(result, ensure_ascii=False)


class ScenarioHistory:
    """Scenario-based conversation memory supporting standard role-content and Gemini formats."""

    def __init__(self, initial_history: list[dict[str, Any]] | None = None) -> None:
        self._history: list[dict[str, str]] = []
        self._rejected_faq_questions: set[str] = set()
        if initial_history:
            for item in initial_history:
                role, text = self._parse_message(item)
                if role and text:
                    self._history.append({"role": role, "content": text})

    @staticmethod
    def _parse_message(item: dict[str, Any]) -> tuple[str, str]:
        raw_role = str(item.get("role", "")).strip().lower()
        role = "assistant" if raw_role in ("model", "assistant") else "user"
        content = item.get("content")
        if isinstance(content, str):
            return role, content.strip()
        parts = item.get("parts")
        if isinstance(parts, list):
            texts: list[str] = []
            for part in parts:
                if isinstance(part, dict) and "text" in part:
                    texts.append(str(part["text"]))
                elif isinstance(part, str):
                    texts.append(part)
            return role, " ".join(texts).strip()
        return role, ""

    def clear_rejected_faqs_if_new_topic(self, telegram_id: int, message: str) -> None:
        if not is_rejection(message):
            self._rejected_faq_questions.clear()

    def get_last_user_message(self, telegram_id: int) -> str | None:
        for msg in reversed(self._history):
            if msg.get("role") == "user":
                return msg.get("content")
        return None

    def get_rejected_faq_questions(self, telegram_id: int) -> set[str]:
        return set(self._rejected_faq_questions)

    async def get_history(self, telegram_id: int) -> list[dict[str, Any]]:
        return [{"role": msg["role"], "content": msg["content"]} for msg in self._history]

    async def to_gemini_contents(self, telegram_id: int) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for msg in self._history:
            gemini_role = "model" if msg["role"] == "assistant" else "user"
            contents.append(
                {
                    "role": gemini_role,
                    "parts": [{"text": msg["content"]}],
                }
            )
        return contents

    async def add_user_message(self, telegram_id: int, message: str) -> None:
        if message and message.strip():
            self._history.append({"role": "user", "content": message.strip()})

    async def add_assistant_message(self, telegram_id: int, message: str) -> None:
        if message and message.strip():
            self._history.append({"role": "assistant", "content": message.strip()})

    def add_rejected_faq_questions(self, telegram_id: int, questions: set[str]) -> None:
        self._rejected_faq_questions.update(questions)

    def clear(self, telegram_id: int) -> None:
        self._history.clear()
        self._rejected_faq_questions.clear()


MemoryHistory = ScenarioHistory


class EvaluationFaqService(FaqEmbeddingService):
    """Evaluation FAQ service that substitutes only retrieval and delegates context formatting to FaqEmbeddingService."""

    def __init__(self, faq_path: Path | None = None) -> None:
        super().__init__(db_manager=None, embedding_provider=None)  # type: ignore[arg-type]
        self._path = faq_path or FAQ_PATH
        self._entries: list[dict[str, Any]] = self._load_entries()
        self._current_candidates: list[str] = []
        self.ready = True

    def _load_entries(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def set_candidates(self, candidates: list[str]) -> None:
        self._current_candidates = list(candidates)

    def find_entry(self, pattern: str) -> dict[str, Any] | None:
        lowered = pattern.strip().lower()
        for entry in self._entries:
            q = entry.get("question", "")
            if q == pattern:
                return entry
        for entry in self._entries:
            q = entry.get("question", "")
            if q.lower().startswith(lowered):
                return entry
        for entry in self._entries:
            q = entry.get("question", "")
            if lowered in q.lower():
                return entry
        return None

    async def search_with_fallback(
        self,
        query: str,
        exclude_questions: set[str] | None = None,
    ) -> list[FaqResult]:
        candidate_patterns = self._current_candidates
        if not candidate_patterns and query:
            matched = self.find_entry(query)
            if matched:
                candidate_patterns = [matched["question"]]

        results: list[FaqResult] = []
        for pattern in candidate_patterns:
            entry = self.find_entry(pattern)
            if not entry:
                continue
            question = str(entry.get("question", ""))
            answer = str(entry.get("answer", ""))
            image = str(entry["image"]) if entry.get("image") else None
            results.append(
                FaqResult(
                    question=question,
                    answer=answer,
                    similarity=1.0,
                    rrf_score=1.0,
                    image=image,
                )
            )

        if exclude_questions:
            results = [r for r in results if r.question not in exclude_questions]
        return results

    async def build_faq_context(
        self,
        user_query: str = "",
        exclude_questions: set[str] | None = None,
        *,
        query: str | None = None,
        exclude: set[str] | None = None,
        candidates: list[str] | None = None,
    ) -> FaqContext:
        target_query = query if query is not None else user_query
        target_exclude = exclude_questions if exclude_questions is not None else exclude
        if candidates is not None:
            old_candidates = self._current_candidates
            self._current_candidates = list(candidates)
            try:
                return await super().build_faq_context(target_query, target_exclude)
            finally:
                self._current_candidates = old_candidates
        return await super().build_faq_context(target_query, target_exclude)


EmptyFaqService = EvaluationFaqService


def make_success_envelope(tool: str, data: dict[str, Any], meta: Any = None) -> dict[str, Any]:
    return {
        "ok": True,
        "source": "bedolaga-mcp",
        "tool": tool,
        "data": data,
        "meta": meta,
    }


def make_error_envelope(
    tool: str,
    code: str,
    message: str,
    retryable: bool = False,
) -> dict[str, Any]:
    return {
        "ok": False,
        "source": "bedolaga-mcp",
        "tool": tool,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }


def make_billing_deposit_without_purchase(
    balance_rubles: float = 500.0,
    deposit_rubles: float = 500.0,
) -> dict[str, Any]:
    kopeks = int(round(deposit_rubles * 100))
    bal_kopeks = int(round(balance_rubles * 100))
    tx = {
        "id": 101,
        "category": "deposit",
        "direction": "credit",
        "raw_type": "deposit",
        "amount_kopeks": kopeks,
        "amount_rubles": deposit_rubles,
        "payment_method": "card",
        "is_completed": True,
        "description": "Пополнение баланса",
        "created_at": "2026-09-01T10:00:00Z",
        "completed_at": "2026-09-01T10:01:00Z",
    }
    dep_summary = {
        "amount_kopeks": kopeks,
        "amount_rubles": deposit_rubles,
        "category": "deposit",
        "created_at": "2026-09-01T10:00:00Z",
        "completed_at": "2026-09-01T10:01:00Z",
    }
    data = {
        "balance_kopeks": bal_kopeks,
        "balance_rubles": balance_rubles,
        "transactions": [tx],
        "latest_completed_deposit": dep_summary,
        "latest_completed_subscription_purchase": None,
        "purchased_after_latest_deposit": False,
        "bot_subscriptions": [],
        "meta": BILLING_META_NOTE,
    }
    return make_success_envelope("bedolaga_billing_get", data, meta=BILLING_META_NOTE)


def make_billing_completed_purchase() -> dict[str, Any]:
    deposit_tx = {
        "id": 101,
        "category": "deposit",
        "direction": "credit",
        "raw_type": "deposit",
        "amount_kopeks": 50000,
        "amount_rubles": 500.0,
        "payment_method": "card",
        "is_completed": True,
        "description": "Пополнение баланса",
        "created_at": "2026-09-01T10:00:00Z",
        "completed_at": "2026-09-01T10:01:00Z",
    }
    purchase_tx = {
        "id": 102,
        "category": "subscription_purchase",
        "direction": "debit",
        "raw_type": "subscription_payment",
        "amount_kopeks": 50000,
        "amount_rubles": 500.0,
        "payment_method": "balance",
        "is_completed": True,
        "description": "Покупка подписки 1 месяц",
        "created_at": "2026-09-01T10:05:00Z",
        "completed_at": "2026-09-01T10:05:00Z",
    }
    data = {
        "balance_kopeks": 0,
        "balance_rubles": 0.0,
        "transactions": [purchase_tx, deposit_tx],
        "latest_completed_deposit": {
            "amount_kopeks": 50000,
            "amount_rubles": 500.0,
            "category": "deposit",
            "created_at": "2026-09-01T10:00:00Z",
            "completed_at": "2026-09-01T10:01:00Z",
        },
        "latest_completed_subscription_purchase": {
            "amount_kopeks": 50000,
            "amount_rubles": 500.0,
            "category": "subscription_purchase",
            "created_at": "2026-09-01T10:05:00Z",
            "completed_at": "2026-09-01T10:05:00Z",
        },
        "purchased_after_latest_deposit": True,
        "bot_subscriptions": [
            {
                "id": 1,
                "bot_record_status": "active",
                "bot_record_effective_status": "active",
                "is_trial": False,
                "tariff_id": 1,
                "tariff_name": "1 месяц",
                "created_at": "2026-09-01T10:05:00Z",
                "start_date": "2026-09-01T10:05:00Z",
                "end_date": "2026-10-01T10:05:00Z",
                "autopay_enabled": False,
                "autopay_days_before": None,
                "note": BOT_RECORD_NOTE,
            }
        ],
        "meta": BILLING_META_NOTE,
    }
    return make_success_envelope("bedolaga_billing_get", data, meta=BILLING_META_NOTE)


def make_billing_empty() -> dict[str, Any]:
    data = {
        "balance_kopeks": 0,
        "balance_rubles": 0.0,
        "transactions": [],
        "latest_completed_deposit": None,
        "latest_completed_subscription_purchase": None,
        "purchased_after_latest_deposit": None,
        "bot_subscriptions": [],
        "meta": BILLING_META_NOTE,
    }
    return make_success_envelope("bedolaga_billing_get", data, meta=BILLING_META_NOTE)


def make_billing_user_not_found() -> dict[str, Any]:
    return make_error_envelope(
        "bedolaga_billing_get",
        "user_not_found",
        f"User with telegram_id {SYNTHETIC_TELEGRAM_ID} was not found in Bedolaga.",
        retryable=False,
    )


def make_billing_upstream_unavailable() -> dict[str, Any]:
    return make_error_envelope(
        "bedolaga_billing_get",
        "upstream_unavailable",
        "Bedolaga billing service returned 503 Service Unavailable.",
        retryable=True,
    )


def _identity_schema(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "telegram_id": {"type": "integer", "description": "System-pinned Telegram identity"},
        "user_id": {"type": "integer", "description": "System-pinned cabinet identity"},
    }
    properties.update(extra or {})
    return {"type": "object", "properties": properties}


def build_synthetic_router(
    custom_results: dict[str, dict[str, Any]] | None = None,
    trace: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[McpRouter, list[SyntheticMcpClient]]:
    bedolaga_tools = [
        McpTool(
            "bedolaga_user_get",
            "Read the current user's Bedolaga account and balance.",
            _identity_schema(),
        ),
        McpTool(
            "bedolaga_billing_get",
            "Read deposits and subscription purchases for personal payment and balance verification.",
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
            "users_get_subscription_url_by_telegram_id",
            "Return the current Telegram user's only unambiguous VPN subscription URL.",
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
    bedolaga_results: dict[str, dict[str, Any]] = {
        "bedolaga_user_get": make_success_envelope(
            "bedolaga_user_get",
            {
                "found": True,
                "telegram_id": SYNTHETIC_TELEGRAM_ID,
                "display_name": "Synthetic User",
                "status": "active",
                "balance_kopeks": 50000,
                "balance_rubles": 500.0,
                "has_made_first_topup": True,
                "has_had_paid_subscription": False,
                "referral_code": "SYNTH123",
                "was_referred": False,
                "promo_group": None,
                "created_at": "2026-08-01T00:00:00Z",
                "last_activity": "2026-09-03T12:00:00Z",
            },
        ),
        "bedolaga_billing_get": make_billing_deposit_without_purchase(),
        "bedolaga_subscription_get": make_success_envelope(
            "bedolaga_subscription_get",
            {
                "has_subscription_records": True,
                "active_record_count": 1,
                "subscriptions": [
                    {
                        "id": 1,
                        "bot_record_status": "active",
                        "bot_record_effective_status": "active",
                        "is_trial": False,
                        "tariff_id": 1,
                        "tariff_name": "1 месяц",
                        "created_at": "2026-08-01T00:00:00Z",
                        "start_date": "2026-08-01T00:00:00Z",
                        "end_date": "2026-12-31T00:00:00Z",
                        "autopay_enabled": False,
                        "autopay_days_before": None,
                        "note": BOT_RECORD_NOTE,
                    }
                ],
                "meta": SUBSCRIPTION_META_NOTE,
            },
            meta=SUBSCRIPTION_META_NOTE,
        ),
    }
    remnawave_results: dict[str, dict[str, Any]] = {
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
        "users_get_subscription_url_by_telegram_id": {
            "status": "found",
            "subscriptionUrl": "https://sub.example.test/synthetic",
        },
        "subscriptions_get_by_user_id": {
            "ok": True,
            "response": {"status": "EXPIRED", "expireAt": "2026-08-01T00:00:00Z"},
        },
    }

    if custom_results:
        for tool_name, res in custom_results.items():
            if tool_name in bedolaga_results or tool_name.startswith("bedolaga_"):
                bedolaga_results[tool_name] = res
            else:
                remnawave_results[tool_name] = res

    shared_trace = trace if trace is not None else []
    bedolaga = SyntheticMcpClient("bedolaga", bedolaga_tools, bedolaga_results, trace=shared_trace)
    remnawave = SyntheticMcpClient(
        "remnawave", remnawave_tools, remnawave_results, trace=shared_trace
    )
    clients = [bedolaga, remnawave]
    router_clients: list[McpClientInterface] = [bedolaga, remnawave]
    return McpRouter(clients=router_clients, readonly=True), clients


def load_cases(path: Path | None = None) -> list[BehaviorCase]:
    cases_file = path or CASES_PATH
    payload = json.loads(cases_file.read_text(encoding="utf-8"))
    cases: list[BehaviorCase] = []
    for item in payload["cases"]:
        data = dict(item)
        if "scenario_history" in data and "history" not in data:
            data["history"] = data.pop("scenario_history")
        if "custom_tool_results" in data and "tool_results" not in data:
            data["tool_results"] = data.pop("custom_tool_results")
        cases.append(BehaviorCase(**data))
    return cases


def score_case(
    case: BehaviorCase,
    response: str,
    clients: list[SyntheticMcpClient],
    trace: list[tuple[str, dict[str, Any]]] | None = None,
) -> CaseResult:
    if trace is not None:
        calls = trace
    elif clients and hasattr(clients[0], "trace") and clients[0].trace:
        calls = clients[0].trace
    else:
        calls = [call for client in clients for call in client.calls]

    tools = [name for name, _ in calls]
    lowered = response.lower()
    violations: list[str] = []

    if case.expect_no_tools and tools:
        violations.append(f"expected no tools to be called, but received: {', '.join(tools)}")

    missing_tools = sorted(set(case.expected_tools) - set(tools))
    if missing_tools:
        violations.append(f"missing tools: {', '.join(missing_tools)}")
    forbidden_tools = sorted(set(case.forbidden_tools) & set(tools))
    if forbidden_tools:
        violations.append(f"forbidden tools: {', '.join(forbidden_tools)}")
    if case.must_contain_any and not any(text.lower() in lowered for text in case.must_contain_any):
        violations.append(f"response lacks any of: {', '.join(case.must_contain_any)}")
    present_forbidden = [text for text in case.must_not_contain if text.lower() in lowered]
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

    if case.expected_tool_order:
        called_indices: list[tuple[str, int]] = []
        for expected_tool in case.expected_tool_order:
            if expected_tool in tools:
                called_indices.append((expected_tool, tools.index(expected_tool)))
        for i in range(len(called_indices) - 1):
            tool_a, idx_a = called_indices[i]
            tool_b, idx_b = called_indices[i + 1]
            if idx_a >= idx_b:
                violations.append(f"incorrect tool ordering: {tool_b} was called before {tool_a}")

    if case.require_clarification:
        if "?" not in response:
            violations.append("response lacks clarification for vague input")
        if case.max_questions is not None:
            question_count = response.count("?")
            if question_count > case.max_questions:
                violations.append(
                    f"response asked {question_count} questions, maximum allowed is {case.max_questions}"
                )
        clarification_topics = [
            "где",
            "каком шаге",
            "каком этапе",
            "какая ошибка",
            "текст ошибки",
            "код ошибки",
            "сообщение об ошибке",
            "скриншот",
            "в боте или",
            "личном кабинете",
        ]
        if not any(topic in lowered for topic in clarification_topics):
            violations.append(
                "clarification lacks relevant inquiry about payment interface, step, error, or screenshot"
            )

    if case.reject_premature_advice:
        premature_indicators = [
            "роутер",
            "перезагрузите",
            "happ",
            "пинг",
            "обновить подписку",
            "отключитесь от vpn",
            "смените сервер",
            "протокол",
            "другой способ",
            "криптовалют",
            "сбп",
            "пополните баланс",
            "купите подписку",
            "приобретите период",
        ]
        found_premature = [p for p in premature_indicators if p in lowered]
        if found_premature:
            violations.append(
                f"premature advice provided before clarifying vague complaint: {', '.join(found_premature)}"
            )

    if case.require_two_step_payment:
        has_deposit_step = any(w in lowered for w in ["пополн", "баланс", "депозит"])
        has_purchase_step = any(
            w in lowered for w in ["приобре", "куп", "выбер", "период", "активир"]
        )
        if not (has_deposit_step and has_purchase_step):
            violations.append(
                "response must cover both mandatory payment steps: balance top-up and subscription period purchase/activation"
            )
        auto_renew_claims = [
            "автоматически продлится",
            "продлится автоматически",
            "автоматически активируется",
            "активируется автоматически",
            "автоматическое продление",
            "автопродление",
        ]
        found_auto_renew = [c for c in auto_renew_claims if c in lowered]
        if found_auto_renew:
            violations.append(
                f"response incorrectly claims balance top-up will automatically renew or activate subscription: {', '.join(found_auto_renew)}"
            )

    if case.require_scoped_user_not_found:
        causal_ui_indicators = [
            "поэтому кнопка",
            "из-за этого кнопка",
            "кнопка не работает из-за",
            "кнопка не работает, потому что",
            "кнопка сломалась",
            "сбой кнопки",
            "ошибка интерфейса",
            "сбой интерфейса",
        ]
        found_causal = [c for c in causal_ui_indicators if c in lowered]
        if found_causal:
            violations.append(
                f"causal UI diagnosis in user_not_found response: {', '.join(found_causal)}"
            )

        broad_claims = [
            "у вас нет аккаунта",
            "нет аккаунта",
            "вы не зарегистрированы",
            "аккаунт отсутствует",
        ]
        found_broad = [b for b in broad_claims if b in lowered]
        if found_broad:
            violations.append(
                f"broad unscoped claim in user_not_found response: {', '.join(found_broad)}"
            )

        has_not_found = any(nf in lowered for nf in ["не найден", "не удалось найти"])
        has_scope = any(
            s in lowered
            for s in ["telegram", "bedolaga", "биллинг", "по вашему id", "по данному id", "по id"]
        )
        if not (has_not_found and has_scope):
            violations.append(
                "user_not_found response lacks scoped explanation (must state user was not found by Telegram ID in Bedolaga/billing)"
            )

    if case.reject_absent_payment_on_error:
        absent_payment_claims = [
            "платежа нет",
            "платеж отсутствует",
            "платёж отсутствует",
            "деньги не дошли",
            "оплата не поступила",
            "платеж не найден",
            "платёж не найден",
            "вы не оплатили",
            "нет платежа",
        ]
        found_absent = [a for a in absent_payment_claims if a in lowered]
        if found_absent:
            violations.append(
                f"response asserts payment is absent when verification failed due to upstream error: {', '.join(found_absent)}"
            )
        temporary_failure_terms = [
            "временно",
            "не удалось проверить",
            "сервис недоступен",
            "попробуйте позже",
            "повторите попытку",
            "повторите позже",
            "ошибка сервиса",
            "технические сложности",
        ]
        if not any(t in lowered for t in temporary_failure_terms):
            violations.append(
                "upstream_unavailable response lacks clear statement explaining temporary impossibility of verifying payment records"
            )

    return CaseResult(
        name=case.name,
        passed=not violations,
        tools=tools,
        violations=violations,
        response=response,
    )


async def run_once(
    cases: list[BehaviorCase] | None = None,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> list[CaseResult]:
    active_settings = settings or get_settings()
    results: list[CaseResult] = []
    case_list = cases if cases is not None else load_cases()
    for case in case_list:
        history = ScenarioHistory(case.history)
        faq_service = EvaluationFaqService()
        if case.faq_candidates:
            faq_service.set_candidates(case.faq_candidates)

        trace: list[tuple[str, dict[str, Any]]] = []
        router, clients = build_synthetic_router(
            custom_results=case.tool_results if case.tool_results else None,
            trace=trace,
        )
        client = create_llm_client(
            active_settings,
            router,
            history,  # type: ignore[arg-type]
            faq_service,
            http_client=http_client,
        )
        try:
            reply = await client.chat(case.user_message, SYNTHETIC_TELEGRAM_ID)
            results.append(score_case(case, reply.text, clients, trace=trace))
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
