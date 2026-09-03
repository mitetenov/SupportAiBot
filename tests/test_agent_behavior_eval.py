"""Deterministic checks for the live behavioral evaluator's local boundaries."""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from benchmarks.agent_behavior_eval import (
    BILLING_META_NOTE,
    SYNTHETIC_TELEGRAM_ID,
    BehaviorCase,
    EvaluationFaqService,
    ScenarioHistory,
    build_synthetic_router,
    load_cases,
    main,
    make_billing_completed_purchase,
    make_billing_deposit_without_purchase,
    make_billing_empty,
    make_billing_upstream_unavailable,
    make_billing_user_not_found,
    run_once,
    score_case,
)


def test_behavior_cases_cover_all_scenarios() -> None:
    cases = load_cases()
    names = {case.name for case in cases}
    expected_names = {
        # Preserved existing 6 cases
        "installation_uses_ready_instruction",
        "subscription_url_is_returned_with_manual_location",
        "deposit_without_purchase",
        "effective_status_requires_panel_check",
        "foreign_identity_is_rejected",
        "refund_escalates",
        # New 7 payment triage cases
        "vague_payment_complaint_requires_clarification",
        "general_payment_howto_uses_faq_without_tools",
        "followup_with_known_context_avoids_repeated_questions",
        "lookup_user_not_found_explains_search_without_ui_diagnosis",
        "upstream_unavailable_reports_temporary_failure",
        "external_charge_without_record_escalates",
        "completed_purchase_panel_discrepancy_checks_panel_and_escalates",
    }
    assert names == expected_names
    assert len(cases) == 13


@pytest.mark.asyncio
async def test_synthetic_router_pins_identity_before_recording_a_tool_call() -> None:
    router, clients = build_synthetic_router()

    await router.call_tool(
        "bedolaga_billing_get",
        {"telegram_id": 999_999, "user_id": 999_999},
        SYNTHETIC_TELEGRAM_ID,
    )

    bedolaga = next(client for client in clients if client.server_name == "bedolaga")
    _, arguments = bedolaga.calls[-1]
    assert arguments["telegram_id"] == SYNTHETIC_TELEGRAM_ID
    assert "user_id" not in arguments
    assert 999_999 not in arguments.values()


def test_behavior_scorer_detects_contract_violations() -> None:
    case = BehaviorCase(
        name="contract",
        user_message="test",
        expected_tools=["bedolaga_billing_get"],
        forbidden_tools=["bedolaga_user_get"],
        must_contain_any=["период"],
        must_not_contain=["готово"],
        expect_escalation=False,
    )
    _, clients = build_synthetic_router()

    result = score_case(case, "Возврат уже готово [ESCALATE]", clients)

    assert result.passed is False
    assert any("missing tools" in violation for violation in result.violations)
    assert any("response lacks" in violation for violation in result.violations)
    assert any("forbidden response text" in violation for violation in result.violations)
    assert any("escalation=True" in violation for violation in result.violations)


def test_scorer_accepts_valid_responses_and_traces() -> None:
    case = next(c for c in load_cases() if c.name == "installation_uses_ready_instruction")
    _, clients = build_synthetic_router()

    valid_reply = (
        "Инструкция по установке доступна в боте @PeipivoSalesBot или на сайте lk.peipivo.top."
    )
    result = score_case(case, valid_reply, clients)
    assert result.passed is True
    assert result.violations == []

    # Test completed purchase panel discrepancy case
    discrepancy_case = next(
        c
        for c in load_cases()
        if c.name == "completed_purchase_panel_discrepancy_checks_panel_and_escalates"
    )
    valid_trace: list[tuple[str, dict[str, Any]]] = [
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
        ("users_get_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID}),
    ]
    discrepancy_reply = (
        "Я проверил биллинг и панель Remnawave: покупка подписки завершена, однако статус в "
        "панели не обновился. Передаю обращение оператору для ручной синхронизации. "
        "/operator [ESCALATE]"
    )
    disc_result = score_case(discrepancy_case, discrepancy_reply, clients, trace=valid_trace)
    assert disc_result.passed is True
    assert disc_result.violations == []


def test_scorer_rejects_irrelevant_faq_sheet() -> None:
    case = next(
        c for c in load_cases() if c.name == "vague_payment_complaint_requires_clarification"
    )
    _, clients = build_synthetic_router()

    # Dumps connection FAQ with Happ buttons and ping
    dumped_faq_reply = (
        "Отключитесь от VPN в Happ (это обязательно!). Затем нажмите две кнопки: "
        "обновить подписку и пинг строго по порядку."
    )
    result = score_case(case, dumped_faq_reply, clients)
    assert result.passed is False
    assert any("forbidden response text" in v for v in result.violations)


def test_scorer_rejects_lack_of_clarification_on_vague_input() -> None:
    case = next(
        c for c in load_cases() if c.name == "vague_payment_complaint_requires_clarification"
    )
    _, clients = build_synthetic_router()

    # Statement without any question or clarification
    no_clarification_reply = (
        "Вы можете попробовать оплатить подписку другим способом в боте @PeipivoSalesBot."
    )
    result = score_case(case, no_clarification_reply, clients)
    assert result.passed is False
    assert any("response lacks clarification" in v for v in result.violations)


def test_scorer_rejects_too_many_questions_on_vague_input() -> None:
    case = next(
        c for c in load_cases() if c.name == "vague_payment_complaint_requires_clarification"
    )
    _, clients = build_synthetic_router()

    # 3 questions when max_questions=2
    three_questions_reply = (
        "Уточните, где вы производите оплату? На каком шаге возникает ошибка? "
        "Какой способ оплаты вы используете?"
    )
    result = score_case(case, three_questions_reply, clients)
    assert result.passed is False
    assert any("maximum allowed is 2" in v for v in result.violations)


def test_scorer_rejects_repeating_questions_from_history() -> None:
    case = next(
        c for c in load_cases() if c.name == "followup_with_known_context_avoids_repeated_questions"
    )
    _, clients = build_synthetic_router()

    # Negative Probe 1: Review finding probe repeating known method and asking about bank error
    probe_review = "Вы оплачиваете через СБП? Какую ошибку показывает банк?"
    res_review = score_case(case, probe_review, clients)
    assert res_review.passed is False
    assert any(
        "forbidden response text" in v or "redundant question" in v or "response lacks any of" in v
        for v in res_review.violations
    )

    # Negative Probe 2: Asking which payment method is used
    probe_method = "Уточните, какой способ оплаты вы используете?"
    res_method = score_case(case, probe_method, clients)
    assert res_method.passed is False

    # Negative Probe 3: Asking what error the bank shows
    probe_bank_err = "Какую ошибку показывает банк?"
    res_bank_err = score_case(case, probe_bank_err, clients)
    assert res_bank_err.passed is False

    # Negative Probe 4: Asking if payment is via SBP
    probe_sbp_q = "Вы оплачиваете через СБП?"
    res_sbp_q = score_case(case, probe_sbp_q, clients)
    assert res_sbp_q.passed is False

    # Negative Probe 5: Asking interface where user already specified bot
    probe_interface = "Уточните, где вы оплачиваете: в боте или в личном кабинете?"
    res_interface = score_case(case, probe_interface, clients)
    assert res_interface.passed is False

    # Negative Probe 6: Merely echoing SBP without providing applicable assistance
    probe_echo = "Оплата через СБП."
    res_echo = score_case(case, probe_echo, clients)
    assert res_echo.passed is False
    assert any("response lacks any of" in v for v in res_echo.violations)

    # Positive Probe 1: Suggesting alternative payment options and trying later
    valid_alt = (
        "Если при оплате через СБП возникает ошибка банка, попробуйте другой способ оплаты "
        "(например, банковской картой или криптовалютой) либо повторите попытку позже."
    )
    res_valid_alt = score_case(case, valid_alt, clients)
    assert res_valid_alt.passed is True
    assert res_valid_alt.violations == []

    # Positive Probe 2: Suggesting card payment or operator escalation
    valid_op = "Попробуйте оплатить банковской картой или обратитесь к оператору: /operator."
    res_valid_op = score_case(case, valid_op, clients)
    assert res_valid_op.passed is True
    assert res_valid_op.violations == []

    # Positive Probe 3: Recommending other method in bot or trying later
    valid_bot_later = "Рекомендуем выбрать другой способ оплаты в боте или повторить попытку позже."
    res_valid_bot = score_case(case, valid_bot_later, clients)
    assert res_valid_bot.passed is True
    assert res_valid_bot.violations == []

    # Positive Probe 4 (Coordinator review finding): Affirmative statement acknowledging SBP with applicable next step
    valid_sbp_affirmative = "Вижу, что вы оплачиваете через СБП. Попробуйте другой способ оплаты."
    res_affirmative = score_case(case, valid_sbp_affirmative, clients)
    assert res_affirmative.passed is True
    assert res_affirmative.violations == []

    # Positive Probe 5: Affirmative statement acknowledging both SBP and bank error with operator next step
    valid_sbp_bank_err = (
        "Вижу, что вы оплачиваете через СБП и возникает ошибка банка. "
        "Рекомендуем выбрать другой способ оплаты или обратиться к оператору: /operator."
    )
    res_sbp_bank = score_case(case, valid_sbp_bank_err, clients)
    assert res_sbp_bank.passed is True
    assert res_sbp_bank.violations == []

    # Negative Probe 7: Repeating SBP question paired with advice
    probe_sbp_q_adv = "Вы оплачиваете через СБП? Попробуйте другой способ оплаты."
    res_sbp_adv = score_case(case, probe_sbp_q_adv, clients)
    assert res_sbp_adv.passed is False
    assert any(
        "redundant question" in v or "forbidden response text" in v for v in res_sbp_adv.violations
    )

    # Negative Probe 8: Repeating bank error question paired with advice
    probe_err_adv = "Какую ошибку показывает банк? Попробуйте другой способ оплаты."
    res_err_adv = score_case(case, probe_err_adv, clients)
    assert res_err_adv.passed is False
    assert any(
        "redundant question" in v or "forbidden response text" in v for v in res_err_adv.violations
    )

    # Negative Probe 9: Prompting to clarify SBP
    probe_clarify_sbp = "Уточните, вы оплачиваете через СБП? Попробуйте другой способ оплаты."
    res_clarify = score_case(case, probe_clarify_sbp, clients)
    assert res_clarify.passed is False
    assert any("redundant question" in v for v in res_clarify.violations)


def test_scorer_rejects_unnecessary_personal_tools_for_howto() -> None:
    case = next(c for c in load_cases() if c.name == "general_payment_howto_uses_faq_without_tools")
    _, clients = build_synthetic_router()

    trace: list[tuple[str, dict[str, Any]]] = [
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})
    ]
    reply = "Для оплаты перейдите в @peipivosalesbot, раздел баланс и выберите период подписки."
    result = score_case(case, reply, clients, trace=trace)
    assert result.passed is False
    assert any("forbidden tools: bedolaga_billing_get" in v for v in result.violations)


def test_scorer_rejects_incorrect_tool_ordering() -> None:
    cases = {c.name: c for c in load_cases()}
    _, clients = build_synthetic_router()

    # 1. completed_purchase_panel_discrepancy: panel called before billing
    disc_case = cases["completed_purchase_panel_discrepancy_checks_panel_and_escalates"]
    inverted_trace: list[tuple[str, dict[str, Any]]] = [
        ("users_get_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID}),
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
    ]
    reply_disc = "Вижу расхождение. Передаю оператору /operator [ESCALATE]"
    result_disc = score_case(disc_case, reply_disc, clients, trace=inverted_trace)
    assert result_disc.passed is False
    assert any(
        "bedolaga_billing_get must be the first profile tool called" in v
        or "incorrect tool ordering" in v
        for v in result_disc.violations
    )

    # 2. external_charge_without_record_escalates: panel lookup called before billing
    ext_case = cases["external_charge_without_record_escalates"]
    reply_ext = "Передаю оператору для ручной проверки платежа. /operator [ESCALATE]"
    result_ext = score_case(ext_case, reply_ext, clients, trace=inverted_trace)
    assert result_ext.passed is False
    assert any(
        "bedolaga_billing_get must be the first profile tool called" in v
        for v in result_ext.violations
    )

    # 2b. external_charge_without_record_escalates: bedolaga_user_get called before billing
    user_first_trace: list[tuple[str, dict[str, Any]]] = [
        ("bedolaga_user_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
    ]
    result_user_first = score_case(ext_case, reply_ext, clients, trace=user_first_trace)
    assert result_user_first.passed is False
    assert any(
        "bedolaga_billing_get must be the first profile tool called" in v
        for v in result_user_first.violations
    )

    # 3. lookup_user_not_found_explains_search_without_ui_diagnosis: reversed trace
    not_found_case = cases["lookup_user_not_found_explains_search_without_ui_diagnosis"]
    reply_nf = "Пользователь с вашим Telegram ID не найден в сервисе Bedolaga."
    result_nf = score_case(not_found_case, reply_nf, clients, trace=inverted_trace)
    assert result_nf.passed is False
    assert any(
        "bedolaga_billing_get must be the first profile tool called" in v
        for v in result_nf.violations
    )

    # 4. upstream_unavailable_reports_temporary_failure: reversed trace
    upstream_case = cases["upstream_unavailable_reports_temporary_failure"]
    reply_up = "Сервис временно недоступен, не удалось проверить платежи. Попробуйте позже."
    result_up = score_case(upstream_case, reply_up, clients, trace=inverted_trace)
    assert result_up.passed is False
    assert any(
        "bedolaga_billing_get must be the first profile tool called" in v
        for v in result_up.violations
    )


def test_scorer_rejects_false_claims_of_missing_account() -> None:
    case = next(
        c for c in load_cases() if c.name == "upstream_unavailable_reports_temporary_failure"
    )
    _, clients = build_synthetic_router()

    trace: list[tuple[str, dict[str, Any]]] = [
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})
    ]
    reply = "У вас нет аккаунта в системе, поэтому проверить платежи не удалось."
    result = score_case(case, reply, clients, trace=trace)
    assert result.passed is False
    assert any("forbidden response text: у вас нет аккаунта" in v for v in result.violations)


def test_scorer_rejects_incorrect_escalation() -> None:
    case = next(c for c in load_cases() if c.name == "deposit_without_purchase")
    _, clients = build_synthetic_router()

    trace: list[tuple[str, dict[str, Any]]] = [
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})
    ]
    # Unexpected escalation
    reply_with_unwanted_escalate = (
        "Баланс пополнен, приобретите период подписки в боте. /operator [ESCALATE]"
    )
    result = score_case(case, reply_with_unwanted_escalate, clients, trace=trace)
    assert result.passed is False
    assert any("escalation=True, expected=False" in v for v in result.violations)


def test_cli_refuses_without_confirm_external_api() -> None:
    test_args = ["agent_behavior_eval.py", "--runs", "1"]
    with patch.object(sys, "argv", test_args):
        with pytest.raises(SystemExit) as exc_info:
            import asyncio

            asyncio.run(main())
        assert exc_info.value.code != 0


def test_cli_rejects_invalid_runs_and_threshold() -> None:
    with patch.object(
        sys, "argv", ["agent_behavior_eval.py", "--runs", "0", "--confirm-external-api"]
    ):
        with pytest.raises(SystemExit) as exc_info:
            import asyncio

            asyncio.run(main())
        assert exc_info.value.code != 0

    with patch.object(
        sys, "argv", ["agent_behavior_eval.py", "--threshold", "1.5", "--confirm-external-api"]
    ):
        with pytest.raises(SystemExit) as exc_info:
            import asyncio

            asyncio.run(main())
        assert exc_info.value.code != 0


@pytest.mark.asyncio
async def test_evaluation_faq_service_loads_real_entries_and_formats_production_context() -> None:
    faq_service = EvaluationFaqService()
    # Test loading real entries and formatting with candidates
    context = await faq_service.build_faq_context(
        query="Не подключается функция оплата",
        candidates=[
            "Как пополнить баланс / приобрести период / продлить подписку / оплатить подписку?",
            "Не могу подключиться к VPN / не работает / не заходит / подписка активна, но не работает",
        ],
    )
    assert not context.is_empty()
    assert len(context.results) == 2
    assert context.text.startswith("Кандидаты FAQ (проверь соответствие вопросу")
    assert "Вопрос: Как пополнить баланс" in context.text
    assert "Вопрос: Не могу подключиться к VPN" in context.text

    # Test candidate exclusion
    excluded_question = context.results[0].question
    context_excluded = await faq_service.build_faq_context(
        query="Не подключается функция оплата",
        exclude={excluded_question},
        candidates=[
            "Как пополнить баланс / приобрести период / продлить подписку / оплатить подписку?",
            "Не могу подключиться к VPN / не работает / не заходит / подписка активна, но не работает",
        ],
    )
    assert len(context_excluded.results) == 1
    assert context_excluded.results[0].question != excluded_question

    # Excluding all yields EMPTY
    context_all_excluded = await faq_service.build_faq_context(
        query="query",
        exclude={r.question for r in context.results},
        candidates=[r.question for r in context.results],
    )
    assert context_all_excluded.is_empty()


@pytest.mark.asyncio
async def test_scenario_history_preserves_isolation_and_formats_across_turns() -> None:
    initial_h1 = [
        {"role": "user", "content": "Вопрос 1"},
        {"role": "assistant", "content": "Ответ 1"},
    ]
    initial_h2 = [
        {"role": "user", "parts": [{"text": "Gemini вопрос"}]},
        {"role": "model", "parts": [{"text": "Gemini ответ"}]},
    ]

    h1 = ScenarioHistory(initial_h1)
    h2 = ScenarioHistory(initial_h2)

    hist1 = await h1.get_history(SYNTHETIC_TELEGRAM_ID)
    assert hist1 == [
        {"role": "user", "content": "Вопрос 1"},
        {"role": "assistant", "content": "Ответ 1"},
    ]

    gemini_contents2 = await h2.to_gemini_contents(SYNTHETIC_TELEGRAM_ID)
    assert gemini_contents2 == [
        {"role": "user", "parts": [{"text": "Gemini вопрос"}]},
        {"role": "model", "parts": [{"text": "Gemini ответ"}]},
    ]

    # Mutate h1 and verify h2 is untouched
    await h1.add_user_message(SYNTHETIC_TELEGRAM_ID, "Вопрос 2")
    await h1.add_assistant_message(SYNTHETIC_TELEGRAM_ID, "Ответ 2")

    assert len(await h1.get_history(SYNTHETIC_TELEGRAM_ID)) == 4
    assert len(await h2.get_history(SYNTHETIC_TELEGRAM_ID)) == 2
    assert h1.get_last_user_message(SYNTHETIC_TELEGRAM_ID) == "Вопрос 2"


def test_synthetic_envelopes_and_distinct_billing_datasets() -> None:
    # 1. Deposit without purchase
    dep = make_billing_deposit_without_purchase(500.0, 500.0)
    assert dep["ok"] is True
    assert dep["source"] == "bedolaga-mcp"
    assert dep["tool"] == "bedolaga_billing_get"
    data = dep["data"]
    assert data["balance_rubles"] == 500.0
    assert data["balance_kopeks"] == 50000
    assert data["purchased_after_latest_deposit"] is False
    assert data["latest_completed_deposit"] is not None
    assert data["latest_completed_subscription_purchase"] is None
    assert "recent_events" not in data
    assert dep["meta"] == BILLING_META_NOTE

    # 2. Completed purchase
    purch = make_billing_completed_purchase()
    assert purch["ok"] is True
    assert purch["data"]["purchased_after_latest_deposit"] is True
    assert purch["data"]["latest_completed_deposit"] is not None
    assert purch["data"]["latest_completed_subscription_purchase"] is not None
    assert len(purch["data"]["bot_subscriptions"]) == 1

    # 3. Empty (absence of payment record)
    empty = make_billing_empty()
    assert empty["ok"] is True
    assert empty["data"]["transactions"] == []
    assert empty["data"]["latest_completed_deposit"] is None

    # 4. user_not_found error envelope
    not_found = make_billing_user_not_found()
    assert not_found["ok"] is False
    assert not_found["source"] == "bedolaga-mcp"
    assert not_found["error"]["code"] == "user_not_found"
    assert not_found["error"]["retryable"] is False

    # 5. upstream_unavailable error envelope
    unavailable = make_billing_upstream_unavailable()
    assert unavailable["ok"] is False
    assert unavailable["source"] == "bedolaga-mcp"
    assert unavailable["error"]["code"] == "upstream_unavailable"
    assert unavailable["error"]["retryable"] is True


@pytest.mark.asyncio
async def test_chronological_trace_records_interleaved_mcp_calls() -> None:
    router, clients = build_synthetic_router()

    await router.call_tool("bedolaga_billing_get", {}, SYNTHETIC_TELEGRAM_ID)
    await router.call_tool(
        "users_get_by_telegram_id",
        {"telegramId": SYNTHETIC_TELEGRAM_ID},
        SYNTHETIC_TELEGRAM_ID,
    )
    await router.call_tool("bedolaga_subscription_get", {}, SYNTHETIC_TELEGRAM_ID)

    bedolaga = next(c for c in clients if c.server_name == "bedolaga")
    remnawave = next(c for c in clients if c.server_name == "remnawave")

    # Client-specific calls
    assert [name for name, _ in bedolaga.calls] == [
        "bedolaga_billing_get",
        "bedolaga_subscription_get",
    ]
    assert [name for name, _ in remnawave.calls] == ["users_get_by_telegram_id"]

    # Shared unified chronological trace
    assert [name for name, _ in bedolaga.trace] == [
        "bedolaga_billing_get",
        "users_get_by_telegram_id",
        "bedolaga_subscription_get",
    ]


@pytest.mark.asyncio
async def test_production_faq_formatter_output_reaches_evaluation_client() -> None:
    """Modifications to FaqEmbeddingService.build_faq_context directly reach the evaluation context."""
    from app.rag.service import FaqEmbeddingService

    faq_service = EvaluationFaqService()
    faq_service.set_candidates(
        ["Как пополнить баланс / приобрести период / продлить подписку / оплатить подписку?"]
    )

    # Unmodified production formatter returns production header
    ctx_normal = await faq_service.build_faq_context("оплата")
    assert "Кандидаты FAQ (проверь соответствие вопросу" in ctx_normal.text
    assert "Вопрос: Как пополнить баланс" in ctx_normal.text
    assert len(ctx_normal.results) == 1
    assert ctx_normal.best_question == ctx_normal.results[0].question

    # Monkeypatching production FaqEmbeddingService.build_faq_context directly changes EvaluationFaqService output
    orig_build = FaqEmbeddingService.build_faq_context

    async def patched_build_faq_context(
        self: Any, user_query: str, exclude_questions: set[str] | None = None
    ) -> Any:
        ctx = await orig_build(self, user_query, exclude_questions)
        return ctx.__class__(
            text=f"CUSTOM_PRODUCTION_HEADER_V2\n{ctx.text}",
            results=ctx.results,
            max_similarity=ctx.max_similarity,
            best_question=ctx.best_question,
        )

    with patch.object(FaqEmbeddingService, "build_faq_context", patched_build_faq_context):
        ctx_patched = await faq_service.build_faq_context("оплата")
        assert ctx_patched.text.startswith("CUSTOM_PRODUCTION_HEADER_V2")
        assert "Кандидаты FAQ (проверь соответствие вопросу" in ctx_patched.text


def test_vague_payment_complaint_scorer_discriminates_relevance_and_premature_advice() -> None:
    case = next(
        c for c in load_cases() if c.name == "vague_payment_complaint_requires_clarification"
    )
    _, clients = build_synthetic_router()

    # Probe 1: Irrelevant question (asking name)
    irrelevant_q = "Как вас зовут?"
    res_irr = score_case(case, irrelevant_q, clients)
    assert res_irr.passed is False
    assert any("clarification lacks relevant inquiry" in v for v in res_irr.violations)

    # Probe 2: Premature VPN troubleshooting dump
    premature_vpn = "Где вы оплачиваете? Отключитесь от VPN в Happ и перезагрузите роутер."
    res_vpn = score_case(case, premature_vpn, clients)
    assert res_vpn.passed is False
    assert any("premature advice" in v for v in res_vpn.violations)

    # Probe 3: Premature alternative payment advice
    premature_alt = "Где вы оплачиваете? Попробуйте другой способ, например сбп или криптовалюту."
    res_alt = score_case(case, premature_alt, clients)
    assert res_alt.passed is False
    assert any("premature advice" in v for v in res_alt.violations)

    # Probe 4: Premature step-by-step payment instructions dump
    premature_steps = "Где вы оплачиваете? Пополните баланс в боте и затем купите подписку."
    res_steps = score_case(case, premature_steps, clients)
    assert res_steps.passed is False
    assert any("premature advice" in v for v in res_steps.violations)

    # Probe 5: Unsupported bank diagnosis accompanying valid question (Sol review finding)
    probe_bank_1 = "Где именно не получается оплатить? Это сбой банка."
    res_bank_1 = score_case(case, probe_bank_1, clients)
    assert res_bank_1.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_bank_1.violations
    )

    # Probe 6: Unsupported bank diagnosis (claim that bank rejected payment)
    probe_bank_2 = "Уточните, где вы оплачиваете? Скорее всего, банк отклонил платёж."
    res_bank_2 = score_case(case, probe_bank_2, clients)
    assert res_bank_2.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_bank_2.violations
    )

    # Probe 7: Unsupported bank diagnosis (claim issue is on bank side)
    probe_bank_3 = "Какая ошибка появляется? Это проблема на стороне банка."
    res_bank_3 = score_case(case, probe_bank_3, clients)
    assert res_bank_3.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_bank_3.violations
    )

    # Probe 8: Unsupported internet diagnosis accompanying valid question
    probe_inet_1 = "Где вы оплачиваете? Возможно, у вас проблема с интернетом."
    res_inet_1 = score_case(case, probe_inet_1, clients)
    assert res_inet_1.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_inet_1.violations
    )

    # Probe 9: Unsupported internet diagnosis (unstable connection)
    probe_inet_2 = "На каком этапе ошибка? Наверное, нестабильное соединение."
    res_inet_2 = score_case(case, probe_inet_2, clients)
    assert res_inet_2.passed is False
    assert any("unsupported causal diagnosis" in v for v in res_inet_2.violations)

    # Probe 10: Unsupported VPN diagnosis accompanying valid question
    probe_vpn_diag_1 = "Какая ошибка появляется? Это сбой VPN."
    res_vpn_diag_1 = score_case(case, probe_vpn_diag_1, clients)
    assert res_vpn_diag_1.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_vpn_diag_1.violations
    )

    # Probe 11: Unsupported VPN diagnosis (attributing failure to VPN)
    probe_vpn_diag_2 = "Где именно не получается оплатить? Оплата не проходит из-за VPN."
    res_vpn_diag_2 = score_case(case, probe_vpn_diag_2, clients)
    assert res_vpn_diag_2.passed is False
    assert any(
        "unsupported causal diagnosis" in v or "forbidden response text" in v
        for v in res_vpn_diag_2.violations
    )

    # Probe 12 (Coordinator review finding): Unconfirmed absence claim asserting failure to find account
    probe_absence_1 = (
        "Не удалось найти вашу учётную запись в системе оплаты. "
        "Где вы оплачиваете: в боте или в личном кабинете?"
    )
    res_absence_1 = score_case(case, probe_absence_1, clients)
    assert res_absence_1.passed is False
    assert any(
        "unverified" in v or "forbidden response text" in v for v in res_absence_1.violations
    )

    # Probe 13: Unconfirmed absence claim with 'учетная запись не найдена'
    probe_absence_2 = "Учётная запись не найдена. На каком шаге возникает сбой?"
    res_absence_2 = score_case(case, probe_absence_2, clients)
    assert res_absence_2.passed is False
    assert any(
        "unverified" in v or "forbidden response text" in v for v in res_absence_2.violations
    )

    # Probe 14: Unconfirmed absence claim with 'пользователь не найден'
    probe_absence_3 = (
        "Пользователь не найден. Уточните, где вы производите оплату: в боте или на сайте?"
    )
    res_absence_3 = score_case(case, probe_absence_3, clients)
    assert res_absence_3.passed is False
    assert any(
        "unverified" in v or "forbidden response text" in v for v in res_absence_3.violations
    )

    # Probe 15 (Sol review round 4 finding): Premature advice recommending payment via SBP
    premature_sbp_rec = (
        "Где именно вы оплачиваете? Рекомендую оплатить через СБП, это работает стабильнее."
    )
    res_sbp_rec = score_case(case, premature_sbp_rec, clients)
    assert res_sbp_rec.passed is False
    assert any("premature advice" in v for v in res_sbp_rec.violations)

    # Probe 16: Premature advice attempting payment via SBP
    premature_sbp_att = "Какая ошибка возникает? Попробуйте оплатить через СБП."
    res_sbp_att = score_case(case, premature_sbp_att, clients)
    assert res_sbp_att.passed is False
    assert any("premature advice" in v for v in res_sbp_att.violations)

    # Positive probe 1: Targeted relevant clarification about interface and error
    valid_clarification = (
        "Уточните, пожалуйста, где именно вы производите оплату (в боте или в личном кабинете) "
        "и какая ошибка появляется?"
    )
    res_valid = score_case(case, valid_clarification, clients)
    assert res_valid.passed is True
    assert res_valid.violations == []

    # Positive probe 2 (Sol review round 4 finding): Clarifying inquiry distinguishing error step during SBP selection
    valid_sbp_clarification = (
        "Где вы оплачиваете — в боте или личном кабинете? "
        "На каком шаге возникает ошибка: при выборе СБП или после?"
    )
    res_sbp_clar = score_case(case, valid_sbp_clarification, clients)
    assert res_sbp_clar.passed is True
    assert res_sbp_clar.violations == []

    # Positive probe 3: Clarifying inquiry asking if SBP or card was used
    valid_sbp_method = (
        "Уточните, где вы производите оплату (в боте или на сайте) "
        "и какой способ был выбран: банковская карта или СБП?"
    )
    res_sbp_method = score_case(case, valid_sbp_method, clients)
    assert res_sbp_method.passed is True
    assert res_sbp_method.violations == []


def test_general_payment_howto_requires_both_steps_and_rejects_auto_renewal() -> None:
    case = next(c for c in load_cases() if c.name == "general_payment_howto_uses_faq_without_tools")
    _, clients = build_synthetic_router()

    # Probe 1: Single step with incorrect auto-renew claim
    auto_renew_single_step = "Пополните баланс — подписка продлится автоматически."
    res_auto = score_case(case, auto_renew_single_step, clients)
    assert res_auto.passed is False
    assert any("both mandatory payment steps" in v for v in res_auto.violations)
    assert any("automatically renew" in v for v in res_auto.violations)

    # Probe 2: Balance deposit only, missing period purchase
    only_deposit = "Для оплаты пополните баланс в личном кабинете lk.peipivo.top через СБП."
    res_dep = score_case(case, only_deposit, clients)
    assert res_dep.passed is False
    assert any("both mandatory payment steps" in v for v in res_dep.violations)

    # Probe 3: Period purchase only, missing balance top-up
    only_purchase = "Перейдите в бот @PeipivoSalesBot и выберите период подписки на 1 месяц."
    res_purch = score_case(case, only_purchase, clients)
    assert res_purch.passed is False
    assert any("both mandatory payment steps" in v for v in res_purch.violations)

    # Probe 4: Mentioning period only as topup amount calculation without a purchase step (Sol review finding)
    topup_amount_only = "Пополните баланс в боте @PeipivoSalesBot на сумму выбранного периода."
    res_topup_only = score_case(case, topup_amount_only, clients)
    assert res_topup_only.passed is False
    assert any("both mandatory payment steps" in v for v in res_topup_only.violations)

    # Probe 5 (Coordinator review finding): Purchasing from balance without an explicit balance top-up step
    probe_balance_no_deposit = "В боте @PeipivoSalesBot приобретите подписку за счёт баланса."
    res_balance_no_dep = score_case(case, probe_balance_no_deposit, clients)
    assert res_balance_no_dep.passed is False
    assert any("both mandatory payment steps" in v for v in res_balance_no_dep.violations)

    # Probe 6: Purchasing with "с баланса" without explicit deposit instruction
    probe_from_balance = "В боте @PeipivoSalesBot приобретите подписку с баланса."
    res_from_balance = score_case(case, probe_from_balance, clients)
    assert res_from_balance.passed is False
    assert any("both mandatory payment steps" in v for v in res_from_balance.violations)

    # Probe 7 (Sol review round 4 finding): Noun-only purchase mention lacks an affirmative purchase step
    probe_noun_only = "Пополните баланс в боте @PeipivoSalesBot для покупки подписки."
    res_noun_only = score_case(case, probe_noun_only, clients)
    assert res_noun_only.passed is False
    assert any("both mandatory payment steps" in v for v in res_noun_only.violations)

    # Probe 8: Prepositional noun phrase with acquisition without purchase directive
    probe_noun_acq = (
        "Для оплаты пополните баланс в личном кабинете lk.peipivo.top с целью приобретения периода."
    )
    res_noun_acq = score_case(case, probe_noun_acq, clients)
    assert res_noun_acq.passed is False
    assert any("both mandatory payment steps" in v for v in res_noun_acq.violations)

    # Probe 9 (Sol review round 4 finding): Negated purchase action claiming separate purchase is not needed
    probe_negated_purchase = (
        "Пополните баланс в боте @PeipivoSalesBot. Приобретать период отдельно не нужно."
    )
    res_neg_purch = score_case(case, probe_negated_purchase, clients)
    assert res_neg_purch.passed is False
    assert any("both mandatory payment steps" in v for v in res_neg_purch.violations)

    # Probe 10: Negated purchase action with 'не требуется'
    probe_negated_purch_2 = (
        "Пополните баланс в боте @PeipivoSalesBot. Покупать подписку отдельно не требуется."
    )
    res_neg_purch_2 = score_case(case, probe_negated_purch_2, clients)
    assert res_neg_purch_2.passed is False
    assert any("both mandatory payment steps" in v for v in res_neg_purch_2.violations)

    # Positive probe 1: Covers both mandatory steps without claiming auto-renewal
    valid_two_step = (
        "Чтобы оплатить подписку, выполните два шага:\n"
        "1. Пополните баланс в боте @PeipivoSalesBot или на сайте lk.peipivo.top;\n"
        "2. Перейдите в раздел покупки и приобретите необходимый период подписки."
    )
    res_valid = score_case(case, valid_two_step, clients)
    assert res_valid.passed is True
    assert res_valid.violations == []

    # Positive probe 2: Correct two-step answer concluding with explicit explanation that top-up does not auto-renew (Sol review finding)
    valid_with_negated_auto_renew = (
        "Чтобы оплатить подписку, выполните два шага:\n"
        "1. Пополните баланс в боте @PeipivoSalesBot или на сайте lk.peipivo.top;\n"
        "2. Перейдите в раздел подписки и приобретите необходимый период.\n"
        "Пополнение само по себе не означает автоматическое продление."
    )
    res_neg = score_case(case, valid_with_negated_auto_renew, clients)
    assert res_neg.passed is True
    assert res_neg.violations == []

    # Positive probe 3: Another legitimate phrasing negating auto-renewal
    valid_negated_v2 = (
        "Пополните баланс в боте @PeipivoSalesBot, затем выберите период подписки во вкладке подписок. "
        "Помните: подписка не продлится автоматически без покупки периода."
    )
    res_neg_v2 = score_case(case, valid_negated_v2, clients)
    assert res_neg_v2.passed is True
    assert res_neg_v2.violations == []

    # Positive probe 4: Explicit deposit action with "Внесите средства на баланс..."
    valid_explicit_dep = (
        "Внесите средства на баланс в боте @PeipivoSalesBot или на сайте lk.peipivo.top, "
        "а затем приобретите необходимый период подписки."
    )
    res_explicit_dep = score_case(case, valid_explicit_dep, clients)
    assert res_explicit_dep.passed is True
    assert res_explicit_dep.violations == []

    # Positive probe 5 (Sol review round 4 finding): Explicit two-step with topup and activation
    valid_topup_and_activate = (
        "Пополните баланс в боте @PeipivoSalesBot, а затем активируйте период подписки в меню."
    )
    res_topup_act = score_case(case, valid_topup_and_activate, clients)
    assert res_topup_act.passed is True
    assert res_topup_act.violations == []

    # Positive probe 6: Numbered steps with topup and ordering
    valid_numbered_steps = (
        "Чтобы оплатить тариф, следуйте инструкции:\n"
        "1. Пополните баланс в боте @PeipivoSalesBot;\n"
        "2. Оформите подписку на выбранный период во вкладке подписок."
    )
    res_num_steps = score_case(case, valid_numbered_steps, clients)
    assert res_num_steps.passed is True
    assert res_num_steps.violations == []


def test_zero_tools_expectation_enforced_for_payment_cases() -> None:
    cases = {c.name: c for c in load_cases()}
    _, clients = build_synthetic_router()

    # 1. Vague complaint case: expect_no_tools is True
    vague_case = cases["vague_payment_complaint_requires_clarification"]
    assert vague_case.expect_no_tools is True
    assert "bedolaga_subscription_get" in vague_case.forbidden_tools
    assert "users_get_subscription_url_by_telegram_id" in vague_case.forbidden_tools

    reply = "Уточните, где вы оплачиваете и какая ошибка?"
    trace_vague = [("bedolaga_subscription_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})]
    res_vague = score_case(vague_case, reply, clients, trace=trace_vague)
    assert res_vague.passed is False
    assert any("expected no tools to be called" in v for v in res_vague.violations)
    assert any("forbidden tools: bedolaga_subscription_get" in v for v in res_vague.violations)

    # 2. General howto case: expect_no_tools is True
    howto_case = cases["general_payment_howto_uses_faq_without_tools"]
    assert howto_case.expect_no_tools is True
    assert "bedolaga_subscription_get" in howto_case.forbidden_tools
    assert "users_get_subscription_url_by_telegram_id" in howto_case.forbidden_tools

    howto_reply = "Пополните баланс в @peipivosalesbot и приобретите период подписки."
    trace_howto = [
        ("users_get_subscription_url_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID})
    ]
    res_howto = score_case(howto_case, howto_reply, clients, trace=trace_howto)
    assert res_howto.passed is False
    assert any("expected no tools to be called" in v for v in res_howto.violations)
    assert any(
        "forbidden tools: users_get_subscription_url_by_telegram_id" in v
        for v in res_howto.violations
    )

    # 3. Follow-up case: expect_no_tools is True
    followup_case = cases["followup_with_known_context_avoids_repeated_questions"]
    assert followup_case.expect_no_tools is True
    trace_followup = [("users_get_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID})]
    followup_reply = "Попробуйте другой способ оплаты в боте."
    res_followup = score_case(followup_case, followup_reply, clients, trace=trace_followup)
    assert res_followup.passed is False
    assert any("expected no tools to be called" in v for v in res_followup.violations)


def test_deposit_without_purchase_forbids_remnawave_lookups() -> None:
    case = next(c for c in load_cases() if c.name == "deposit_without_purchase")
    assert "users_get_by_telegram_id" in case.forbidden_tools
    assert "subscriptions_get_by_user_id" in case.forbidden_tools
    assert "bedolaga_subscription_get" in case.forbidden_tools

    _, clients = build_synthetic_router()

    # Remnawave query after billing
    trace_panel = [
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
        ("users_get_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID}),
    ]
    reply = "Баланс пополнен, приобретите период подписки."
    res = score_case(case, reply, clients, trace=trace_panel)
    assert res.passed is False
    assert any("forbidden tools: users_get_by_telegram_id" in v for v in res.violations)


def test_user_not_found_rejects_causal_ui_diagnosis_and_unscoped_claims() -> None:
    case = next(
        c
        for c in load_cases()
        if c.name == "lookup_user_not_found_explains_search_without_ui_diagnosis"
    )
    assert case.require_scoped_user_not_found is True
    _, clients = build_synthetic_router()
    trace = [("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})]

    # Probe 1: Sol finding probe: 'У вас нет аккаунта, поэтому кнопка не работает.'
    probe_causal = "У вас нет аккаунта, поэтому кнопка не работает."
    res_causal = score_case(case, probe_causal, clients, trace=trace)
    assert res_causal.passed is False
    assert any("causal UI diagnosis" in v for v in res_causal.violations)
    assert any("broad unscoped claim" in v for v in res_causal.violations)

    # Probe 2: Attributing button failure to user not found
    probe_btn = "Кнопка не работает, потому что пользователь не найден."
    res_btn = score_case(case, probe_btn, clients, trace=trace)
    assert res_btn.passed is False
    assert any("causal UI diagnosis" in v for v in res_btn.violations)

    # Probe 3: Broad claim without scope
    probe_broad = "У вас нет аккаунта в нашей системе."
    res_broad = score_case(case, probe_broad, clients, trace=trace)
    assert res_broad.passed is False
    assert any("broad unscoped claim" in v for v in res_broad.violations)

    # Positive probe: Scoped explanation stating user was not found by Telegram ID in Bedolaga
    valid_reply = (
        "Пользователь с вашим Telegram ID не найден в сервисе оплаты Bedolaga. "
        "Пожалуйста, запустите бота заново с помощью команды /start."
    )
    res_valid = score_case(case, valid_reply, clients, trace=trace)
    assert res_valid.passed is True
    assert res_valid.violations == []


def test_upstream_unavailable_rejects_absent_payment_assertions() -> None:
    case = next(
        c for c in load_cases() if c.name == "upstream_unavailable_reports_temporary_failure"
    )
    assert case.reject_absent_payment_on_error is True
    _, clients = build_synthetic_router()
    trace = [("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID})]

    # Probe 1: Sol finding probe: 'Платежа нет. Попробуйте позже.'
    probe_absent = "Платежа нет. Попробуйте позже."
    res_absent = score_case(case, probe_absent, clients, trace=trace)
    assert res_absent.passed is False
    assert any("asserts payment is absent" in v for v in res_absent.violations)

    # Probe 2: Money didn't arrive assertion
    probe_money = "Деньги не дошли, так как сервис биллинга временно недоступен."
    res_money = score_case(case, probe_money, clients, trace=trace)
    assert res_money.passed is False
    assert any("asserts payment is absent" in v for v in res_money.violations)

    # Probe 3: You have not paid assertion
    probe_unpaid = "Вы не оплатили подписку, попробуйте позже."
    res_unpaid = score_case(case, probe_unpaid, clients, trace=trace)
    assert res_unpaid.passed is False
    assert any("asserts payment is absent" in v for v in res_unpaid.violations)

    # Probe 4 (Coordinator review finding): False account absence claim on upstream technical failure
    probe_absence_up_1 = "Не удалось найти вашу учётную запись в системе оплаты. Попробуйте позже."
    res_absence_up_1 = score_case(case, probe_absence_up_1, clients, trace=trace)
    assert res_absence_up_1.passed is False
    assert any(
        "asserts payment is absent" in v or "forbidden response text" in v
        for v in res_absence_up_1.violations
    )

    # Probe 5: Claiming account not found on upstream error
    probe_absence_up_2 = "Учётная запись не найдена, сервис временно недоступен. Попробуйте позже."
    res_absence_up_2 = score_case(case, probe_absence_up_2, clients, trace=trace)
    assert res_absence_up_2.passed is False
    assert any(
        "asserts payment is absent" in v or "forbidden response text" in v
        for v in res_absence_up_2.violations
    )

    # Probe 6: Claiming user not found on upstream error
    probe_absence_up_3 = "Пользователь не найден в системе. Попробуйте позже."
    res_absence_up_3 = score_case(case, probe_absence_up_3, clients, trace=trace)
    assert res_absence_up_3.passed is False
    assert any(
        "asserts payment is absent" in v or "forbidden response text" in v
        for v in res_absence_up_3.violations
    )

    # Positive probe: Explaining temporary impossibility of verifying records
    valid_reply = (
        "Сервис биллинга временно недоступен, поэтому сейчас не удалось проверить поступление средств. "
        "Пожалуйста, повторите попытку позже."
    )
    res_valid = score_case(case, valid_reply, clients, trace=trace)
    assert res_valid.passed is True
    assert res_valid.violations == []


@pytest.mark.asyncio
async def test_offline_run_once_integration_with_mock_transport(
    valid_settings_dict: dict[str, Any],
) -> None:
    """Offline integration test exercising run_once end-to-end with mock HTTP transport."""
    captured_requests: list[dict[str, Any]] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content.decode("utf-8"))
        captured_requests.append(data)
        messages = data.get("input", [])

        last_message = messages[-1] if messages else {}
        if last_message.get("type") == "function_call_output":
            output = [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Баланс пополнен на 750 рублей, но период подписки не куплен. Приобретите период подписки в боте.",
                        }
                    ],
                }
            ]
            return httpx.Response(200, json={"output": output})

        user_text = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    for part in content:
                        if part.get("type") == "input_text":
                            user_text = part.get("text", "")
                break

        if "Проверьте баланс" in user_text:
            output = [
                {
                    "type": "function_call",
                    "call_id": "call_bill_1",
                    "name": "bedolaga_billing_get",
                    "arguments": json.dumps({"telegram_id": SYNTHETIC_TELEGRAM_ID}),
                }
            ]
            return httpx.Response(200, json={"output": output})

        output = [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Для оплаты в боте выберите другой способ оплаты, например картой.",
                    }
                ],
            }
        ]
        return httpx.Response(200, json={"output": output})

    mock_transport = httpx.MockTransport(mock_handler)
    mock_http_client = httpx.AsyncClient(transport=mock_transport)

    test_settings = Settings(
        **{
            **valid_settings_dict,
            "llm_provider": "openai",
            "openai_api_key": SecretStr("sk-synthetic-offline-test"),
        }
    )

    # Case 1: Critical facts ("Оплачиваю в боте через СБП") reside EXCLUSIVELY in history!
    case_history_only = BehaviorCase(
        name="case_history_only",
        user_message="Вылезает ошибка банка",
        history=[
            {"role": "user", "content": "Оплачиваю в боте через СБП"},
            {"role": "assistant", "content": "На каком этапе возникает сбой?"},
        ],
        faq_candidates=[
            "Как пополнить баланс / приобрести период / продлить подписку / оплатить подписку?"
        ],
        expect_no_tools=True,
        must_contain_any=["другой способ", "карт", "боте"],
    )

    # Case 2: Uses custom tool results
    case_tool = BehaviorCase(
        name="case_tool",
        user_message="Проверьте баланс",
        expected_tools=["bedolaga_billing_get"],
        must_contain_any=["баланс", "период"],
        tool_results={"bedolaga_billing_get": make_billing_deposit_without_purchase(1250.0, 750.0)},
    )

    test_cases = [case_history_only, case_tool]

    # First run
    results_run1 = await run_once(
        cases=test_cases, settings=test_settings, http_client=mock_http_client
    )
    assert len(results_run1) == 2
    assert results_run1[0].passed is True
    assert results_run1[1].passed is True

    # Verify captured requests for case 1:
    # 1. Production FAQ context reached the client
    req1 = captured_requests[0]
    messages_case1 = req1["input"]
    system_prompts = [m["content"] for m in messages_case1 if m.get("role") == "system"]
    assert any("Кандидаты FAQ (проверь соответствие вопросу" in sp for sp in system_prompts)
    assert any("Как пополнить баланс / приобрести период" in sp for sp in system_prompts)

    # 2. History with history-only facts reached the client in exact roles and content
    assert {"role": "user", "content": "Оплачиваю в боте через СБП"} in messages_case1
    assert {"role": "assistant", "content": "На каком этапе возникает сбой?"} in messages_case1

    # 3. Custom tool result reached the client in case 2
    req_tool_turn2 = captured_requests[2]
    fn_outputs = [m for m in req_tool_turn2["input"] if m.get("type") == "function_call_output"]
    assert len(fn_outputs) == 1
    tool_data = json.loads(fn_outputs[0]["output"])
    assert tool_data["ok"] is True
    assert tool_data["data"]["balance_rubles"] == 1250.0
    assert tool_data["data"]["balance_kopeks"] == 125000
    assert tool_data["data"]["latest_completed_deposit"]["amount_rubles"] == 750.0
    assert tool_data["data"]["purchased_after_latest_deposit"] is False
    assert tool_data["data"]["transactions"][0]["amount_rubles"] == 750.0

    # 4. Strict isolation: Case 2 did NOT contain Case 1 history or messages
    req_case2_turn1 = captured_requests[1]
    messages_case2 = req_case2_turn1["input"]
    assert not any("Оплачиваю в боте через СБП" in str(m) for m in messages_case2)

    # Second run to test repeat isolation
    captured_requests.clear()
    results_run2 = await run_once(
        cases=test_cases, settings=test_settings, http_client=mock_http_client
    )
    assert len(results_run2) == 2
    assert results_run2[0].passed is True
    assert results_run2[1].passed is True
    req_run2_case1 = captured_requests[0]
    assert len(req_run2_case1["input"]) == len(messages_case1)

    await mock_http_client.aclose()
