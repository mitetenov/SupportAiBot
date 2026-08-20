"""Guards on the shipped knowledge base and its agreement with the system prompt.

The bot quotes ``faq/faq.json`` verbatim, so a wrong entry is a wrong answer to
every user who asks. These tests pin the facts operators had to correct by hand.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from app.constants import SupportPrompt

FAQ_PATH = Path("faq/faq.json")


@pytest.fixture(scope="module")
def entries() -> list[dict[str, Any]]:
    return json.loads(FAQ_PATH.read_text(encoding="utf-8"))


def find(entries: list[dict[str, Any]], *needles: str) -> dict[str, Any]:
    """The single entry whose question or keywords mention every needle."""
    matches = [
        e
        for e in entries
        if all(
            needle.lower() in (e["question"] + " " + " ".join(e.get("keywords", []))).lower()
            for needle in needles
        )
    ]
    assert matches, f"no FAQ entry covers {needles}"
    return matches[0]


class TestKnowledgeBaseIsWellFormed:
    def test_every_entry_has_a_question_an_answer_and_keywords(
        self, entries: list[dict[str, Any]]
    ) -> None:
        for e in entries:
            assert e.get("question", "").strip()
            assert e.get("answer", "").strip()
            assert isinstance(e.get("keywords"), list)

    def test_questions_are_unique(self, entries: list[dict[str, Any]]) -> None:
        questions = [e["question"] for e in entries]
        assert len(questions) == len(set(questions))


class TestAutoRenewal:
    """No charge ever leaves the card; the subscription renews off the bot balance.

    The old entry promised an autopay toggle in the bot, so the bot sent people
    hunting for a control that does not exist and confirmed a card charge that
    never happens.
    """

    def test_states_that_nothing_is_charged_to_the_card(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "карт" in answer
        assert "не списыва" in answer

    def test_explains_renewal_from_the_bot_balance(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "баланс" in answer

    def test_does_not_promise_a_toggle_in_the_bot(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "отключить автопродление" not in answer

    def test_is_reachable_from_how_users_actually_phrase_it(
        self, entries: list[dict[str, Any]]
    ) -> None:
        keywords = " ".join(find(entries, "автопродление").get("keywords", [])).lower()
        assert "отвязать карту" in keywords


class TestSubscriptionLink:
    """Operators keep explaining the three-dots menu by hand."""

    def test_an_entry_explains_how_to_copy_the_link(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "ссылк", "копировать")["answer"].lower()
        assert "три точки" in answer

    def test_the_prompt_no_longer_denies_that_menu_exists(self) -> None:
        assert "Только главный экран" not in SupportPrompt.SYSTEM
        assert "три точки" in SupportPrompt.SYSTEM


class TestGapsOperatorsAnsweredByHand:
    def test_tribute_is_documented_as_the_legacy_payment_provider(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "tribute")["answer"].lower()
        assert "больше не" in answer
        assert "отпишитесь" in answer

    def test_gifting_to_someone_without_an_account_is_documented(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "подар", "нет аккаунта")["answer"].lower()
        assert "пробн" in answer

    def test_telegram_id_lookup_is_documented(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "telegram id")["answer"].lower()
        assert "@myidbot" in answer
        assert "номер" in answer

    def test_switching_tun_to_proxy_is_offered_when_traffic_stops(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "не грузится")["answer"].lower()
        assert "proxy" in answer

    def test_the_prompt_offers_the_proxy_fallback_during_diagnosis(self) -> None:
        assert "TUN" in SupportPrompt.SYSTEM
        assert "Proxy" in SupportPrompt.SYSTEM


class TestIllustratedEntries:
    """An entry may name a screenshot; the file has to actually ship with it."""

    def test_every_named_image_exists_in_the_image_directory(
        self, entries: list[dict[str, Any]]
    ) -> None:
        missing = [
            e["image"]
            for e in entries
            if e.get("image") and not (Path("faq/images") / e["image"]).is_file()
        ]
        assert not missing, f"faq.json names images that are not in faq/images/: {missing}"

    def test_the_button_question_is_covered_and_illustrated(
        self, entries: list[dict[str, Any]]
    ) -> None:
        """Six repeats in /gaps and no entry at all — every instruction said
        "press the left button" and nothing said where that button is."""
        entry = find(entries, "где кнопка обновить")
        assert "в её правой части" in entry["answer"].lower()
        assert entry.get("image")

    def test_no_entry_sends_people_to_the_left_of_the_vpn_label(
        self, entries: list[dict[str, Any]]
    ) -> None:
        """The two icons sit to the RIGHT of "VPN", with the three-dot menu
        further right — faq/images/happ-buttons.png shows exactly that.

        Eight entries used to open by sending people to the left, which is very
        likely why "где кнопка обновить?" was the most repeated question in the
        whole /gaps report. "Left" and "right" are still correct *within* the
        pair; it is the pair's position that was wrong.
        """
        wrong = [e["question"] for e in entries if "слева от надписи vpn" in e["answer"].lower()]
        assert not wrong, f"these entries put the buttons on the wrong side: {wrong}"

    def test_the_answers_that_open_with_those_buttons_carry_the_picture(
        self, entries: list[dict[str, Any]]
    ) -> None:
        """A connection answer's first step is pressing them, so it is worth showing."""
        expected_illustrated = [
            e["question"]
            for e in entries
            if "«Обновить подписку»" in e["answer"] and "«Пинг»" in e["answer"]
        ]
        assert expected_illustrated
        missing = [q for q in expected_illustrated if not find(entries, q[:30]).get("image")]
        assert not missing, f"these instruct pressing the buttons but show nothing: {missing}"
