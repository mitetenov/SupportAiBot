"""Deterministic checks for the live behavioral evaluator's local boundaries."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest

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

    # Repeating questions that user already answered in history
    repeat_reply = "Уточните, где вы оплачиваете: в боте или в личном кабинете?"
    result = score_case(case, repeat_reply, clients)
    assert result.passed is False
    assert any("forbidden response text" in v for v in result.violations)


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
    case = next(
        c
        for c in load_cases()
        if c.name == "completed_purchase_panel_discrepancy_checks_panel_and_escalates"
    )
    _, clients = build_synthetic_router()

    # Inverted order: Remnawave called before billing
    inverted_trace: list[tuple[str, dict[str, Any]]] = [
        ("users_get_by_telegram_id", {"telegramId": SYNTHETIC_TELEGRAM_ID}),
        ("bedolaga_billing_get", {"telegram_id": SYNTHETIC_TELEGRAM_ID}),
    ]
    reply = "Вижу расхождение. Передаю оператору /operator [ESCALATE]"
    result = score_case(case, reply, clients, trace=inverted_trace)
    assert result.passed is False
    assert any(
        "incorrect tool ordering: users_get_by_telegram_id was called before bedolaga_billing_get"
        in v
        for v in result.violations
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
