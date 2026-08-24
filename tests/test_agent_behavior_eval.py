"""Deterministic checks for the live behavioral evaluator's local boundaries."""

import pytest

from benchmarks.agent_behavior_eval import (
    SYNTHETIC_TELEGRAM_ID,
    BehaviorCase,
    build_synthetic_router,
    load_cases,
    score_case,
)


def test_behavior_cases_cover_payment_identity_panel_and_escalation() -> None:
    names = {case.name for case in load_cases()}
    assert names == {
        "installation_uses_ready_instruction",
        "deposit_without_purchase",
        "effective_status_requires_panel_check",
        "foreign_identity_is_rejected",
        "refund_escalates",
    }


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

