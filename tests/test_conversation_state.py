"""Unit tests for ConversationState tracking last query and operator suppression."""

from datetime import timedelta
from typing import Any

from app.bot.conversation_state import ConversationState


class MockFaqContext:
    """Mock FAQ retrieval context object for testing."""

    EMPTY: Any = None

    def __init__(self, text: str = "FAQ...", results: list[Any] | None = None) -> None:
        self.text = text
        self.results = results or []


MockFaqContext.EMPTY = MockFaqContext("EMPTY")


class MockTime:
    """Controllable time source in seconds."""

    def __init__(self, initial_time: float = 100_000.0) -> None:
        self._now = initial_time

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class TestConversationState:
    """Validate operator suppression window and last query TTL expiration."""

    USER_ID: int = 100

    def test_should_report_no_operator_activity_for_an_unknown_user(self) -> None:
        state = ConversationState()
        assert state.is_operator_recently_active(self.USER_ID) is False

    def test_should_suppress_the_ai_right_after_an_operator_replies(self) -> None:
        state = ConversationState()
        state.record_operator_reply(self.USER_ID)
        assert state.is_operator_recently_active(self.USER_ID) is True

    def test_should_stop_suppressing_once_the_window_elapses(self) -> None:
        mock_time = MockTime()
        state = ConversationState(
            operator_suppression_window=timedelta(minutes=30),
            time_func=mock_time,
        )
        state.record_operator_reply(self.USER_ID)
        assert state.is_operator_recently_active(self.USER_ID) is True

        # Advance beyond 30 minutes
        mock_time.advance(30 * 60 + 1)
        assert state.is_operator_recently_active(self.USER_ID) is False

    def test_zero_duration_suppression_window(self) -> None:
        state = ConversationState(operator_suppression_window=timedelta(seconds=0))
        state.record_operator_reply(self.USER_ID)
        assert state.is_operator_recently_active(self.USER_ID) is False

    def test_should_track_users_independently(self) -> None:
        state = ConversationState()
        state.record_operator_reply(self.USER_ID)
        assert state.is_operator_recently_active(self.USER_ID) is True
        assert state.is_operator_recently_active(200) is False

    def test_should_remember_the_last_query_with_its_retrieval(self) -> None:
        state = ConversationState()
        context = MockFaqContext("FAQ content")

        state.record_query(self.USER_ID, "не работает впн", context)

        last = state.last_query(self.USER_ID)
        assert last is not None
        assert last.text == "не работает впн"
        assert last.faq_context is context

    def test_should_overwrite_the_last_query_on_each_turn(self) -> None:
        state = ConversationState()
        context = MockFaqContext()

        state.record_query(self.USER_ID, "первый", context)
        state.record_query(self.USER_ID, "второй", context)

        last = state.last_query(self.USER_ID)
        assert last is not None
        assert last.text == "второй"

    def test_should_ignore_a_blank_or_none_query(self) -> None:
        state = ConversationState()
        context = MockFaqContext()

        state.record_query(self.USER_ID, "  ", context)
        assert state.last_query(self.USER_ID) is None

        state.record_query(self.USER_ID, None, context)
        assert state.last_query(self.USER_ID) is None

    def test_should_forget_the_last_query_once_it_expires(self) -> None:
        mock_time = MockTime()
        state = ConversationState(
            last_query_ttl=timedelta(hours=6),
            time_func=mock_time,
        )
        state.record_query(self.USER_ID, "давний вопрос", MockFaqContext())
        assert state.last_query(self.USER_ID) is not None

        # Advance beyond 6 hours
        mock_time.advance(6 * 3600 + 1)
        assert state.last_query(self.USER_ID) is None

    def test_should_substitute_an_empty_retrieval_when_none_was_recorded(self) -> None:
        state = ConversationState()
        state.record_query(self.USER_ID, "вопрос", None)

        last = state.last_query(self.USER_ID)
        assert last is not None
        assert last.faq_context_or_empty() is not None

    def test_should_drop_everything_for_a_user_on_clear(self) -> None:
        state = ConversationState()
        state.record_query(self.USER_ID, "вопрос", MockFaqContext())
        state.record_operator_reply(self.USER_ID)

        state.clear(self.USER_ID)

        assert state.last_query(self.USER_ID) is None
        assert state.is_operator_recently_active(self.USER_ID) is False

    def test_should_evict_expired_entries_on_the_scheduled_sweep(self) -> None:
        mock_time = MockTime()
        state = ConversationState(
            operator_suppression_window=timedelta(minutes=30),
            last_query_ttl=timedelta(hours=6),
            time_func=mock_time,
        )

        for uid in range(1, 51):
            state.record_query(uid, f"вопрос {uid}", MockFaqContext())
            state.record_operator_reply(uid)

        # Advance time so all are expired
        mock_time.advance(7 * 3600)
        removed = state.evict_expired()
        assert removed == 100  # 50 queries + 50 replies

        for uid in range(1, 51):
            assert state.last_query(uid) is None
            assert state.is_operator_recently_active(uid) is False

    def test_should_keep_live_entries_during_the_sweep(self) -> None:
        state = ConversationState()
        state.record_query(self.USER_ID, "свежий вопрос", MockFaqContext())
        state.record_operator_reply(self.USER_ID)

        removed = state.evict_expired()
        assert removed == 0

        assert state.last_query(self.USER_ID) is not None
        assert state.is_operator_recently_active(self.USER_ID) is True
