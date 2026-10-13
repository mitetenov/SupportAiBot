"""Media routing through real Telegram, buffering, pipeline and Z.AI clients.

Only external Telegram/LLM/MCP/FAQ and account storage boundaries are mocked.
"""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from aiogram import Dispatcher
from aiogram.types import Chat, Document, Message, PhotoSize, Update, User

from app.bot.buffer import UserMessageBuffer
from app.bot.conversation_state import ConversationState
from app.bot.photo_downloader import PhotoDownloader
from app.bot.pipeline import UserMessagePipeline
from app.bot.rate_limiter import UserRateLimiter
from app.bot.router import setup_router
from app.bot.sender import TelegramMessageSender
from app.config import Settings
from app.constants import get_message
from app.llm.base import LlmProcessingException
from app.llm.fallback import LlmFallbackClient
from app.llm.mcp_client import McpTool
from app.llm.zai import ZaiClient
from app.rag.types import FaqContext
from app.storage.chat_history import ChatHistoryService
from tests.test_router import MockDatabaseSessionManager


def completion(text="Ответ по вложению", **fields):
    message = {"role": "assistant", "content": text, **fields}
    finish = "tool_calls" if fields.get("tool_calls") else "stop"
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": {}}


@pytest.fixture
async def media_client(valid_settings_dict):
    clients = []
    history = ChatHistoryService()
    faq = MagicMock()
    faq.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        McpTool(
            name="test_tool", description="check", input_schema={"type": "object", "properties": {}}
        )
    ]
    mcp.call_tool = AsyncMock(return_value='{"ok": true}')

    def make(handler, model="glm-5.3-flash", effort="none"):
        settings = Settings(
            **{
                **valid_settings_dict,
                "llm_provider": "zai",
                "zai_model": model,
                "zai_api_key": "test-zai-key",
                "reasoning_effort": effort,
            }
        )
        client = ZaiClient(
            settings,
            mcp,
            history,
            faq,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        clients.append(client)
        return client

    yield SimpleNamespace(make=make, history=history, mcp=mcp, faq=faq)
    for client in clients:
        await client.http_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["glm-5.3-flash", "glm-5.3-flashx"])
@pytest.mark.parametrize("kind", ["photo", "pdf"])
@pytest.mark.parametrize("caption", [None, "Квитанция за сегодня"])
async def test_telegram_media_reaches_glm_and_survives_mcp_turn(media_client, model, kind, caption):
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    "",
                    reasoning_content="private reasoning",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "test_tool", "arguments": "{}"},
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=completion())

    zai = media_client.make(handler, model=model)
    backup = MagicMock()
    backup.supports_images.return_value = True
    backup.supports_pdf.return_value = False
    backup.do_chat = AsyncMock(side_effect=AssertionError("fallback must not run"))
    llm = LlmFallbackClient([zai, backup])
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.get_file = AsyncMock(
        return_value=SimpleNamespace(
            file_path="documents/receipt.pdf" if kind == "pdf" else "photos/img.png"
        )
    )
    content = b"%PDF-1.4\nreceipt\n%%EOF" if kind == "pdf" else b"\x89PNG\r\n\x1a\nimage"

    async def download(_path, destination):
        destination.write(content)

    bot.download_file = AsyncMock(side_effect=download)
    sender = MagicMock(spec=TelegramMessageSender)
    sender.send = AsyncMock()
    forwarder = MagicMock()
    forwarder.forward_to_support = AsyncMock()
    forwarder.forward_error_to_topic = AsyncMock()
    gaps = MagicMock()
    gaps.evaluate = AsyncMock()
    state = ConversationState()
    typing = MagicMock()
    buffer = UserMessageBuffer(window_ms=2500)
    pipeline = UserMessagePipeline(
        llm, sender, forwarder, UserRateLimiter(min_interval=0), gaps, state, typing
    )
    router = setup_router(
        sender,
        llm,
        forwarder,
        MockDatabaseSessionManager(),
        media_client.history,
        gaps,
        MagicMock(is_command=MagicMock(return_value=False)),
        PhotoDownloader(bot),
        buffer,
        pipeline,
        state,
        MagicMock(),
        -100123,
    )
    dp = Dispatcher()
    dp.include_router(router)
    user = User(id=100, is_bot=False, first_name="Test")
    chat = Chat(id=100, type="private")
    media = (
        {
            "document": Document(
                file_id="pdf-1",
                file_unique_id="pdf-u",
                file_name="receipt.pdf",
                mime_type="application/pdf",
            )
        }
        if kind == "pdf"
        else {
            "photo": [PhotoSize(file_id="photo-1", file_unique_id="photo-u", width=100, height=100)]
        }
    )
    await dp.feed_update(
        bot,
        Update(
            update_id=1,
            message=Message(
                message_id=1, date=123456, chat=chat, from_user=user, caption=caption, **media
            ),
        ),
    )
    await dp.feed_update(
        bot,
        Update(
            update_id=2,
            message=Message(
                message_id=2,
                date=123457,
                chat=chat,
                from_user=user,
                text="Оплатила, проверьте квитанцию",
            ),
        ),
    )
    await buffer.drain(pipeline.handle)

    assert len(requests) == 2
    for body in requests:
        assert body["model"] == model
        assert body["thinking"] == {"type": "enabled", "clear_thinking": False}
        assert body["reasoning_effort"] == "low"
        assert body["tool_choice"] == "auto"
        parts = next(m for m in body["messages"] if m["role"] == "user")["content"]
        assert "Оплатила, проверьте квитанцию" in parts[0]["text"]
        assert (caption or get_message(f"bot.{kind}.default.prompt")) in parts[0]["text"]
        attachment = parts[1]
        if kind == "pdf":
            assert attachment["type"] == "file"
            uri = attachment["file"]["file_data"]
            assert uri.startswith("data:application/pdf;base64,")
        else:
            assert attachment["type"] == "image_url"
            uri = attachment["image_url"]["url"]
            assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == content
        assert "api.telegram.org/file" not in json.dumps(body)
    assert requests[1]["messages"][-2]["reasoning_content"] == "private reasoning"
    assert requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"ok": true}',
    }
    media_client.mcp.call_tool.assert_awaited_once_with("test_tool", {}, 100)
    backup.do_chat.assert_not_awaited()
    sender.send.assert_awaited_once_with(100, "Ответ по вложению")
    forwarder.forward_to_support.assert_awaited_once()
    history = await media_client.history.get_history(100)
    assert len(history) == 2
    assert history[-1]["content"] == "Ответ по вложению"
    assert all("base64," not in m["content"] for m in history)
    gaps.evaluate.assert_awaited_once()
    assert gaps.evaluate.await_args.args[0] == (
        f"{caption}\nОплатила, проверьте квитанцию" if caption else "Оплатила, проверьте квитанцию"
    )


@pytest.mark.asyncio
async def test_pdf_fallback_skips_incompatible_provider_and_preserves_file(media_client):
    bodies = []

    def unavailable(_request):
        return httpx.Response(503, json={"error": {"message": "unavailable"}})

    def available(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    primary = media_client.make(unavailable)
    incompatible = media_client.make(
        lambda _: pytest.fail("PDF leaked to text provider"), model="glm-4.7"
    )
    compatible = media_client.make(available, model="glm-5.3-flashx")
    llm = LlmFallbackClient([primary, incompatible, compatible])
    assert llm.supports_pdf()
    reply = await llm.chat_with_pdf("Квитанция", 100, "JVBERi0xLjQ=")
    assert reply.text == "Ответ по вложению"
    assert bodies[0]["messages"][-1]["content"][-1]["file"] == {
        "file_data": "data:application/pdf;base64,JVBERi0xLjQ="
    }
    assert len(await media_client.history.get_history(100)) == 2


@pytest.mark.asyncio
async def test_unsupported_pdf_is_rejected_before_api_and_history(media_client):
    client = media_client.make(lambda _: pytest.fail("must not call API"), model="glm-4.7")
    assert not client.supports_pdf()
    assert not LlmFallbackClient([client]).supports_pdf()
    for target in (client, LlmFallbackClient([client])):
        with pytest.raises(LlmProcessingException):
            await target.chat_with_pdf("Квитанция", 100, "JVBERi0xLjQ=")
    assert await media_client.history.get_history(100) == []
    media_client.faq.build_faq_context.assert_not_awaited()


@pytest.mark.parametrize(
    "effort,expected",
    [
        ("none", "low"),
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "max"),
        ("max", "max"),
    ],
)
@pytest.mark.parametrize("model", ["glm-5.3-flash", "glm-5.3-flashx"])
async def test_flash_requests_always_use_supported_thinking(media_client, effort, expected, model):
    client = media_client.make(
        lambda _: pytest.fail("not an HTTP test"), model=model, effort=effort
    )
    body = client.build_request_body([{"role": "user", "content": "test"}])
    assert body["thinking"] == {"type": "enabled", "clear_thinking": False}
    assert body["reasoning_effort"] == client.get_effective_reasoning_effort() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pdf", "photo"])
async def test_captionless_attachment_is_not_recorded_as_user_question(media_client, kind):
    client = media_client.make(lambda _: httpx.Response(200, json=completion()))
    method = client.chat_with_pdf if kind == "pdf" else client.chat_with_image
    await method("", 100, "JVBERi0xLjQ=")
    history = await media_client.history.get_history(100)
    assert history[0]["content"] == ("[PDF]" if kind == "pdf" else "[Скриншот]")
