"""Unit tests for parsing the Bedolaga ticket payloads."""

from app.bedolaga.types import Ticket, TicketMessage, ticket_from_payload

PAYLOAD = {
    "id": 17,
    "user_id": 55,
    "title": "Не подключается VPN",
    "status": "open",
    "priority": "normal",
    "messages": [
        {
            "id": 100,
            "user_id": 55,
            "message_text": "На телефоне пишет ошибку",
            "is_from_admin": False,
            "has_media": True,
            "media_type": "photo",
        },
        {
            "id": 101,
            "user_id": 55,
            "message_text": "Проверьте подписку",
            "is_from_admin": True,
            "has_media": False,
            "media_type": None,
        },
    ],
}


class TestTicketFromPayload:
    """The API payload becomes the shape the rest of the code reads."""

    def test_parses_ticket_fields(self) -> None:
        ticket = ticket_from_payload(PAYLOAD)
        assert ticket.id == 17
        assert ticket.user_id == 55
        assert ticket.title == "Не подключается VPN"
        assert ticket.status == "open"
        assert ticket.priority == "normal"

    def test_parses_messages_in_order(self) -> None:
        ticket = ticket_from_payload(PAYLOAD)
        assert [m.id for m in ticket.messages] == [100, 101]
        assert ticket.messages[0].text == "На телефоне пишет ошибку"
        assert ticket.messages[0].has_media is True
        assert ticket.messages[0].media_type == "photo"
        assert ticket.messages[1].is_from_admin is True

    def test_tolerates_a_ticket_without_messages(self) -> None:
        ticket = ticket_from_payload({"id": 5, "user_id": 1, "title": "t", "status": "open"})
        assert ticket.messages == ()
        assert ticket.last_message is None
        assert ticket.awaits_answer is False

    def test_tolerates_null_text(self) -> None:
        payload = {
            "id": 5,
            "user_id": 1,
            "title": "t",
            "status": "open",
            "messages": [{"id": 1, "message_text": None, "is_from_admin": False}],
        }
        assert ticket_from_payload(payload).messages[0].text == ""


class TestAwaitsAnswer:
    """Only a ticket whose last word is the user's is ours to answer."""

    def _ticket(self, status: str, *messages: TicketMessage) -> Ticket:
        return Ticket(id=1, user_id=2, title="t", status=status, messages=messages)

    def test_true_when_the_last_message_is_from_the_user(self) -> None:
        ticket = self._ticket("open", TicketMessage(id=1, text="Помогите", is_from_admin=False))
        assert ticket.awaits_answer is True

    def test_true_for_a_pending_ticket_from_the_cabinet(self) -> None:
        ticket = self._ticket("pending", TicketMessage(id=1, text="Ещё вопрос", is_from_admin=False))
        assert ticket.awaits_answer is True

    def test_false_when_support_answered_last(self) -> None:
        ticket = self._ticket(
            "answered",
            TicketMessage(id=1, text="Помогите", is_from_admin=False),
            TicketMessage(id=2, text="Держите", is_from_admin=True),
        )
        assert ticket.awaits_answer is False

    def test_false_for_a_closed_ticket(self) -> None:
        ticket = self._ticket("closed", TicketMessage(id=1, text="Помогите", is_from_admin=False))
        assert ticket.awaits_answer is False


class TestQuestion:
    """The title carries the topic of a ticket nobody has followed up on yet."""

    def test_prepends_the_title_to_the_only_message(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Не работает оплата",
            status="open",
            messages=(TicketMessage(id=1, text="Карта не проходит", is_from_admin=False),),
        )
        assert ticket.question == "Не работает оплата\n\nКарта не проходит"

    def test_does_not_repeat_a_title_the_message_already_contains(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Карта не проходит",
            status="open",
            messages=(TicketMessage(id=1, text="Карта не проходит, помогите", is_from_admin=False),),
        )
        assert ticket.question == "Карта не проходит, помогите"

    def test_uses_only_the_latest_message_in_a_running_thread(self) -> None:
        ticket = Ticket(
            id=1,
            user_id=2,
            title="Не работает оплата",
            status="open",
            messages=(
                TicketMessage(id=1, text="Карта не проходит", is_from_admin=False),
                TicketMessage(id=2, text="Попробуйте другую", is_from_admin=True),
                TicketMessage(id=3, text="Та же ошибка", is_from_admin=False),
            ),
        )
        assert ticket.question == "Та же ошибка"
