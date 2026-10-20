"""Offline customer/ticket goldens from the unchanged pinned Python source."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from app.bedolaga.types import ticket_from_payload
from app.bot.buffer import BufferedMessage, MessageBatch
from app.bot.conversation_state import ConversationState
from app.bot.pipeline import UserMessagePipeline
from app.constants import get_message
from app.llm.base import LlmReply

logging.disable(logging.CRITICAL)


async def pipeline_case(name, text, response="answer", limited=False, operator=False):
    trace = []

    async def chat(query, user):
        trace.append({"event": "model", "text": query, "user": user})
        return LlmReply(text=response)

    async def send(chat_id, answer):
        trace.append({"event": "send", "text": answer, "user": chat_id})

    async def forward(chat_id, ids, user, answer, escalate, **kwargs):
        trace.append({"event": "forward", "text": answer, "user": chat_id, "escalate": escalate})

    async def evaluate(query, user, answer, faq):
        trace.append({"event": "gap", "text": query, "user": user})

    state = ConversationState()
    if operator:
        state.record_operator_reply(42)
    rate = MagicMock()
    rate.try_acquire.return_value = not limited
    model = MagicMock()
    model.chat = chat
    sender = MagicMock()
    sender.send = send
    forwarder = MagicMock()
    forwarder.forward_to_support = forward
    gaps = MagicMock()
    gaps.evaluate = evaluate
    typing = MagicMock()
    pipeline = UserMessagePipeline(model, sender, forwarder, rate, gaps, state, typing)
    message = NS(message_id=1, chat=NS(id=42), from_user=NS(id=42))
    await pipeline.handle(MessageBatch.of([BufferedMessage.from_text(message, text)]))
    return {
        "name": name,
        "text": text,
        "response": response,
        "limited": limited,
        "operator": operator,
        "trace": trace,
    }


async def main():
    customers = [
        await pipeline_case("normal", "check subscription"),
        await pipeline_case("model escalation", "connection", "I need help [ESCALATE]"),
        await pipeline_case("human request", "нужен оператор"),
        await pipeline_case("rate limited", "check", limited=True),
        await pipeline_case("operator active", "check", operator=True),
    ]
    tickets = []
    for title, messages, status in [
        ("Проблема оплаты", [{"id": 10, "message_text": "уже третий раз"}], "open"),
        ("Оплата", [{"id": 10, "message_text": "Оплата не проходит"}], "pending"),
        (
            "старое название",
            [
                {"id": 10, "message_text": "старый вопрос"},
                {"id": 11, "message_text": "новый вопрос"},
            ],
            "open",
        ),
        (
            "Проблема",
            [
                {"id": 10, "message_text": "старое"},
                {"id": 11, "message_text": None, "has_media": True},
            ],
            "open",
        ),
        (" ", [{"id": 10, "message_text": "   "}], "open"),
        ("closed", [{"id": 10, "message_text": "check"}], "closed"),
        ("admin", [{"id": 10, "message_text": "reply", "is_from_admin": True}], "answered"),
        ("empty", [], "open"),
    ]:
        payload = {"id": 7, "user_id": 12, "title": title, "status": status, "messages": messages}
        ticket = ticket_from_payload(payload)
        tickets.append(
            {
                "input": payload,
                "question": ticket.question,
                "awaits": ticket.awaits_answer,
                "last_user_id": ticket.last_user_message.id if ticket.last_user_message else None,
            }
        )
    Path("internal/bot/testdata").mkdir(exist_ok=True)
    Path("internal/bot/testdata/python_runtime.json").write_text(
        json.dumps(
            {
                "customers": customers,
                "tickets": tickets,
                "commands": {
                    key: get_message(key)
                    for key in ["bot.help", "bot.unknown.command", "bot.start.welcome"]
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


asyncio.run(main())
