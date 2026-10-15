"""Guards on the shipped knowledge base and its agreement with the system prompt.

The bot may paraphrase FAQ answers. These tests pin the underlying facts and
conditions that operators or the service owner confirmed.
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
    """Optional renewal uses internal balance for an active paid subscription."""

    def test_states_that_nothing_is_charged_to_the_card(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "карт" in answer
        assert "автоматического списания с банковской карты" in answer
        assert "нет" in answer

    def test_explains_renewal_from_the_bot_balance(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "баланс" in answer

    def test_explains_the_confirmed_controls_and_renewal_scope(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "автопродление")["answer"].lower()
        assert "«автоплатеж» → «включить»" in answer
        assert "переключатель «автопродление»" in answer
        assert "только для действующей оплаченной подписки" in answer
        assert "не покупает первый платный период после триала" in answer
        assert "не возобновляет уже истёкшую подписку" in answer

    def test_is_reachable_from_how_users_actually_phrase_it(
        self, entries: list[dict[str, Any]]
    ) -> None:
        keywords = " ".join(find(entries, "автопродление").get("keywords", [])).lower()
        assert "отвязать карту" in keywords


class TestSubscriptionLink:
    """The FAQ directs users to supported ways to get their link."""

    def test_an_entry_explains_where_to_find_the_link(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "ссылк", "копировать")["answer"].lower()
        assert "отправит" in answer
        assert "вручную" in answer
        assert "@peipivosalesbot" in answer
        assert "lk.peipivo.top" in answer

    def test_faq_does_not_instruct_users_to_copy_via_three_dots(
        self, entries: list[dict[str, Any]]
    ) -> None:
        assert all("три точки" not in e["answer"].lower() for e in entries)

    def test_the_prompt_does_not_restore_the_removed_copying_instruction(self) -> None:
        assert "Только главный экран" not in SupportPrompt.SYSTEM
        assert "три точки" not in SupportPrompt.SYSTEM
        assert "верни саму ссылку целиком без изменений" in SupportPrompt.SYSTEM


class TestEmptyServerListAfterInstallation:
    """Installing the client alone does not import the user's subscription."""

    def test_tells_the_user_to_press_the_second_button(self, entries: list[dict[str, Any]]) -> None:
        answer = find(entries, "нет серверов")["answer"].lower()
        assert "первая кнопка" in answer
        assert "только устанавливает приложение" in answer
        assert "вторую кнопку" in answer
        assert "добавить подписку" in answer

    def test_explains_how_to_reopen_the_subscription_page(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "нет серверов")["answer"]
        assert "@PeipivoSalesBot" in answer
        assert "https://lk.peipivo.top" in answer


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
        assert "аккаунт" in answer
        assert "пробную подписку необязательно" in answer

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


class TestNaPlatformDiagnosis:
    """App-specific prechecks precede internet checks and OS-specific recovery."""

    @staticmethod
    def answer_for(entries: list[dict[str, Any]], question_start: str) -> str:
        entry = next(e for e in entries if e["question"].startswith(question_start))
        return str(entry["answer"])

    def test_generic_answer_asks_for_the_app_before_os_specific_recovery(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = self.answer_for(entries, "Серверы показывают n/a")
        for platform in ("iOS", "iPadOS", "macOS", "Windows", "Android"):
            assert platform in answer
        assert answer.index("Если приложение неизвестно") < answer.index("напишите вашу ОС")
        assert "в Incy пропустите обновление приложения" in answer
        assert "обновите приложение Happ до последней версии или установите" not in answer

    def test_apple_platforms_are_sent_to_incy(self, entries: list[dict[str, Any]]) -> None:
        answer = self.answer_for(entries, "Все серверы n/a на iPhone")
        for platform in ("iOS", "iPadOS", "macOS"):
            assert platform in answer
        assert "установите Incy" in answer
        assert "@PeipivoSalesBot" in answer
        assert "https://lk.peipivo.top" in answer
        assert answer.index("обновите Happ") < answer.index("https://google.com")
        assert answer.index("https://google.com") < answer.index("установите Incy")
        assert "в обновлённом Happ" in answer
        assert "Если уже используете Incy" in answer
        assert (
            "повторную установку или обновление приложения на этом этапе не предлагайте" in answer
        )

    def test_windows_and_android_update_then_reinstall_happ(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = self.answer_for(entries, "Все серверы n/a на Windows")
        assert "Windows" in answer
        assert "Android" in answer
        assert "обновите Happ до последней версии" in answer
        assert "Happ последней версии" in answer
        assert "можно переустановить Happ" in answer
        assert answer.index("https://google.com") < answer.index("можно переустановить Happ")
        assert "Переустановку не предлагайте до проверки Google" in answer
        assert "@PeipivoSalesBot" in answer
        assert "https://lk.peipivo.top" in answer
        assert "Этот шаг относится к Happ" in answer
        assert "в Incy пропустите обновление приложения" in answer

    def test_system_prompt_enforces_the_same_os_split(self) -> None:
        prompt = SupportPrompt.SYSTEM
        assert "выясни используемое приложение, если оно неизвестно" in prompt
        assert "Для Happ — обновить Happ" in prompt
        assert "Для Incy — пропустить обновление приложения" in prompt
        assert "При оставшихся n/a уточни ОС" in prompt
        assert "не советуй переустановку/смену приложения" in prompt


class TestConnectionDiagnosisOrder:
    @pytest.mark.parametrize("index", [0, 27, 35, 36, 38, 39, 40])
    def test_overlapping_articles_keep_the_confirmed_order(self, entries, index) -> None:
        answer = entries[index]["answer"]
        assert answer.index("«Обновить подписку»") < answer.index("«Пинг»")
        assert answer.index("«Пинг»") < answer.index("обновите Happ")
        assert answer.index("обновите Happ") < answer.index("https://google.com")
        assert answer.index("https://google.com") < answer.index("https://ya.ru")
        assert "строго с выключенным VPN" in answer
        assert "ошибка обновления подписки или все серверы" in answer
        assert "в Incy пропустите обновление приложения" in answer
        assert "Если Happ уже последней версии, этот шаг тоже пропустите" in answer

    def test_google_stage_does_not_repeat_completed_refresh_and_ping(self, entries) -> None:
        for index in (0, 35, 36, 38):
            answer = entries[index]["answer"]
            tail = (
                answer[answer.index("Если Google открылся") :]
                if "Если Google открылся" in answer
                else answer.split("Если после этих шагов", 1)[1]
            )
            assert "нажмите" not in tail
            assert "ошибки и не все серверы" in answer

    def test_deduplicated_real_context_preserves_first_steps(self, entries) -> None:
        from app.rag.catalog import format_faq_context
        from app.rag.types import FaqResult

        selected = [entries[i] for i in (0, 35, 38)]
        context = format_faq_context(
            [FaqResult(e["question"], e["answer"], 0.8, 0.02, e.get("image")) for e in selected]
        )
        assert context.count("Напишите, появилась ли ошибка обновления и что показал пинг") == 1
        assert context.index("«Обновить подписку»") < context.index("«Пинг»")
        assert (
            context.index("«Пинг»")
            < context.index("обновите Happ")
            < context.index("https://google.com")
        )
        assert "в Incy пропустите обновление приложения" in context

    def test_prompt_waits_for_each_step_in_order(self) -> None:
        prompt = SupportPrompt.SYSTEM.split("ДИАГНОСТИКА:", 1)[1].split(
            "УСТРОЙСТВА И ЭСКАЛАЦИЯ:", 1
        )[0]
        assert prompt.index("«Обновить подписку»") < prompt.index("«Пинг»")
        assert (
            prompt.index("«Пинг»")
            < prompt.index("обновить Happ")
            < prompt.index("https://google.com")
        )
        assert "ошибка обновления подписки или все серверы n/a" in prompt
        assert "Давай только следующий невыполненный шаг" in prompt
        assert "дождись его результата" in prompt
        assert "известное не переспрашивай" in prompt


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


class TestPaymentTroubleshooting:
    def test_payment_failure_faq_specifies_application_conditions(
        self, entries: list[dict[str, Any]]
    ) -> None:
        answer = find(entries, "ошибка при пополнении")["answer"].lower()
        assert "этапе" in answer
        assert "выбор" in answer or "проведен" in answer
        assert "другой способ пополнения" in answer
        assert "@peipivosalesbot" in answer


class TestConfirmedProductPolicies:
    def test_cabinet_navigation_uses_the_confirmed_labels(self, entries) -> None:
        for entry in entries:
            assert "«Баланс и платежи»" not in entry["answer"]
            assert "«Подписки»" not in entry["answer"]
        assert "«Баланс»" in entries[5]["answer"]
        assert "«Подписка»" in entries[5]["answer"]

    def test_trial_and_paid_traffic_agree_across_articles(self, entries) -> None:
        for index in (8, 9, 12):
            assert "20 ГБ" in entries[index]["answer"]
            assert "оплачен" in entries[index]["answer"]

    def test_telegram_proxy_recovery_does_not_require_vpn(self, entries) -> None:
        answer = find(entries, "прокси", "не работает")["answer"]
        assert "отдельно от VPN-подписки" in answer
        assert "Обновите подписку" not in answer

    def test_windows_modes_keep_all_prerequisites(self, entries) -> None:
        for index in (19, 35):
            answer = entries[index]["answer"].lower()
            for fact in ("windows", "подписк", "приложени", "сервер", "активен", "последн"):
                assert fact in answer

    def test_linux_has_ready_instructions_and_button_guide_covers_both_apps(self, entries) -> None:
        assert "Linux" in entries[10]["answer"]
        assert "на странице своей подписки" in entries[10]["answer"]
        assert "В Happ и Incy кнопки расположены одинаково" in entries[53]["answer"]
