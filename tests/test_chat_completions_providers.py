"""Offline contracts for OpenRouter/Z.AI, including real fallback orchestration."""

import asyncio
import io
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm import create_llm_client
from app.llm.base import LlmProcessingException, TokenUsage
from app.llm.fallback import is_fallback_eligible
from app.llm.openrouter import OpenRouterClient
from app.llm.zai import ZaiClient
from app.logging_config import setup_logging
from app.logging_redaction import register_settings_secrets, safe_serialize
from app.rag.types import FaqContext

PROVIDERS = ("openrouter", "zai")


def settings_for(provider, **overrides):
    values = {
        "telegram_bot_token": "test-token",
        "telegram_support_group_chat_id": -1001234567890,
        "llm_provider": provider,
        "embedding_provider": "gemini",
        "gemini_api_key": "embedding-test-key",
        "openrouter_api_key": "router.arbitrary.secret",
        "openrouter_model": "z-ai/glm-4.7",
        "zai_api_key": "zai.arbitrary.secret",
        "zai_model": "glm-4.7",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def dependencies():
    router = MagicMock()
    router.list_tools.return_value = []
    router.call_tool = AsyncMock(return_value="completed action")
    history = MagicMock()
    history.get_history = AsyncMock(return_value=[{"role": "user", "content": "earlier question"}])
    history.get_last_user_message.return_value = None
    history.get_rejected_faq_questions.return_value = set()
    history.add_user_message = AsyncMock()
    history.add_assistant_message = AsyncMock()
    faq = MagicMock()
    faq.build_faq_context = AsyncMock(return_value=FaqContext.EMPTY)
    return router, history, faq


def client_for(provider, dependencies, http=None, **overrides):
    return create_llm_client(settings_for(provider, **overrides), *dependencies, http_client=http)


def completion(content="Final answer", finish="stop", calls=None, **message):
    return {
        "choices": [
            {
                "finish_reason": finish,
                "message": {"content": content, "tool_calls": calls, **message},
            }
        ]
    }


def tool(call_id="first", arguments='{"id":42}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "change", "arguments": arguments},
    }


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "field,value", [("api_key", None), ("api_key", "  "), ("model", None), ("model", " ")]
)
def test_primary_credentials_and_model_required(provider, field, value):
    with pytest.raises(ValidationError, match=f"{provider.upper()}_{field.upper()}"):
        settings_for(provider, **{f"{provider}_{field}": value})


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "url",
    [
        "",
        " ",
        "ftp://example.com/v1",
        "https:///v1",
        "https://user:secret@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#secret",
        "https://host/chat/completions/",
        "https://host:wrong/v1",
        "https://[bad/v1",
    ],
)
def test_active_base_url_validation_does_not_expose_values(provider, url):
    with pytest.raises(ValidationError) as exc:
        settings_for(provider, **{f"{provider}_base_url": url})
    assert f"{provider.upper()}_BASE_URL" in str(exc.value)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeouts(provider, timeout):
    with pytest.raises(ValidationError):
        settings_for(provider, **{f"{provider}_timeout_seconds": timeout})


def test_fallback_targets_preserve_models_and_only_require_active_credentials(dependencies):
    settings = settings_for(
        "zai",
        openrouter_model=None,
        llm_fallback_chain=" OpenRouter :vendor/model:free,openrouter:vendor/second,zai:backup",
    )
    client = create_llm_client(settings, *dependencies)
    assert [c.model for c in client._clients] == [
        "glm-4.7",
        "vendor/model:free",
        "vendor/second",
        "backup",
    ]
    with pytest.raises(ValidationError, match="OPENROUTER_API_KEY"):
        settings_for("zai", openrouter_api_key=None, llm_fallback_chain="openrouter:backup")
    settings_for(
        "zai", openrouter_api_key=None, openrouter_model=None, openrouter_base_url="unused"
    )
    with pytest.raises(ValidationError, match="EMBEDDING_PROVIDER"):
        settings_for("zai", embedding_provider="zai")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "effort,native",
    [
        ("minimal", "low"),
        ("low", "low"),
        ("medium", "high"),
        ("high", "high"),
        ("xhigh", "max"),
        ("max", "max"),
    ],
)
def test_known_reasoning_modes(provider, effort, native, dependencies):
    prefix = "z-ai/" if provider == "openrouter" else ""
    client = client_for(
        provider,
        dependencies,
        **{f"{provider}_model": prefix + "glm-5.3", "reasoning_effort": effort},
    )
    body = client.build_request_body([])
    if provider == "openrouter":
        assert body["reasoning"] == {"effort": native}
        assert "thinking" not in body and "reasoning_effort" not in body
    else:
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == native
    assert client.get_effective_reasoning_effort() == native
    with pytest.raises(ValueError, match="REASONING_EFFORT"):
        client_for(provider, dependencies, **{f"{provider}_model": prefix + "glm-5.3"})


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("effort", ["none", "high"])
def test_toggle_and_unknown_models(provider, effort, dependencies):
    client = client_for(provider, dependencies, reasoning_effort=effort)
    body = client.build_request_body([])
    assert client.get_effective_reasoning_effort() == ("none" if effort == "none" else "enabled")
    assert "reasoning_effort" not in body
    client = client_for(
        provider,
        dependencies,
        **{f"{provider}_model": "vendor/custom:free", "reasoning_effort": effort},
    )
    body = client.build_request_body([])
    assert body == {"model": "vendor/custom:free", "messages": [], "stream": False}
    assert client.get_effective_reasoning_effort() == "unsupported/ignored"


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_transport_headers_timeout_and_ownership(provider, dependencies):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=30) as http:
        client = client_for(
            provider,
            dependencies,
            http,
            **{
                f"{provider}_base_url": "https://example.invalid/v1/",
                f"{provider}_timeout_seconds": 123,
            },
        )
        await client.call_api([], "", 42)
        await client.close()
        assert not http.is_closed
        assert "authorization" not in http.headers
    assert str(seen[0].url) == "https://example.invalid/v1/chat/completions"
    expected_key = "router.arbitrary.secret" if provider == "openrouter" else "zai.arbitrary.secret"
    assert seen[0].headers["authorization"] == f"Bearer {expected_key}"
    assert all(value == 123 for value in seen[0].extensions["timeout"].values())
    own = client_for(provider, dependencies)
    assert own._http_client is None
    owned_http = own.http_client
    assert owned_http.event_hooks["request"] and owned_http.event_hooks["response"]
    await own.close()
    assert owned_http.is_closed


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "finish",
    [
        None,
        "length",
        "content_filter",
        "error",
        "sensitive",
        "model_context_window_exceeded",
        "unknown",
    ],
)
async def test_incomplete_response_never_runs_tools_or_saves_history(
    provider, finish, dependencies
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=completion("partial", finish, [tool()]))
        )
    ) as http:
        with pytest.raises(LlmProcessingException) as exc:
            await client_for(provider, dependencies, http).chat("Help", 42)
    assert not is_fallback_eligible(exc.value)
    dependencies[0].call_tool.assert_not_awaited()
    dependencies[1].add_assistant_message.assert_not_awaited()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"message": None, "finish_reason": "stop"}]},
        completion(None),
        completion([]),
        completion("partial", "stop", [tool()]),
        completion(None, "tool_calls", []),
        completion(None, "tool_calls", [tool(), tool()]),
        completion(None, "tool_calls", [tool(arguments="bad")]),
        completion(None, "tool_calls", [tool(arguments="[]")]),
        completion(None, "tool_calls", [tool(arguments="null")]),
        completion(None, "tool_calls", [tool(arguments=3)]),
        completion(None, "tool_calls", [tool(call_id="")]),
        completion(None, "tool_calls", [{"id": "a", "function": {"name": "", "arguments": "{}"}}]),
        completion(None, "tool_calls", [{"id": "a", "function": None}]),
        completion(None, "tool_calls", [None]),
        completion(calls={}),
    ],
)
def test_malformed_completions_are_domain_errors(provider, payload, dependencies):
    with pytest.raises(LlmProcessingException):
        client_for(provider, dependencies).parse_response(payload)


@pytest.mark.parametrize("location", ["top", "choice"])
@pytest.mark.parametrize(
    "code,eligible",
    [
        (400, False),
        (404, False),
        (422, False),
        (401, True),
        (402, True),
        (403, True),
        (408, True),
        (413, True),
        (429, True),
        (500, True),
        (502, True),
        (503, True),
        (504, True),
        ("unknown", False),
        (True, False),
    ],
)
@pytest.mark.parametrize("as_string", [False, True])
async def test_openrouter_error_codes_obey_fallback_policy(
    location, code, eligible, as_string, dependencies
):
    payload = completion("partial", "error", [tool()])
    error = {"code": str(code) if as_string else code, "message": "provider private diagnostic"}
    (payload if location == "top" else payload["choices"][0])["error"] = error
    attempts = []

    def respond(request):
        attempts.append(request.url.host)
        return httpx.Response(200, json=completion() if request.url.host == "api.z.ai" else payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = client_for("openrouter", dependencies, http, llm_fallback_chain="zai:glm-4.7")
        if eligible:
            assert (await client.chat("Help", 42)).text == "Final answer"
            assert attempts == ["openrouter.ai", "api.z.ai"]
        else:
            with pytest.raises(LlmProcessingException) as exc:
                await client.chat("Help", 42)
            assert not is_fallback_eligible(exc.value)
            assert "provider private diagnostic" not in str(exc.value)
            assert attempts == ["openrouter.ai"]
            dependencies[1].add_assistant_message.assert_not_awaited()
    dependencies[0].call_tool.assert_not_awaited()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "status,attempts,eligible",
    [
        (400, 1, False),
        (404, 1, False),
        (422, 1, False),
        (402, 1, True),
        (429, 3, True),
        (503, 3, True),
    ],
)
async def test_actual_http_error_status_survives_invalid_json(
    provider, status, attempts, eligible, dependencies
):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(status, text="<html>bad upstream</html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LlmProcessingException) as exc:
            await client_for(provider, dependencies, http).call_api([], "", 42)
    assert exc.value.status_code == status
    assert is_fallback_eligible(exc.value) is eligible
    assert len(seen) == attempts


@pytest.mark.parametrize(
    "code,eligible",
    [("1113", True), (1113, True), ("1210", False), (1210, False), ("unknown", False)],
)
@pytest.mark.parametrize("flat", [True, False])
def test_zai_business_errors(code, eligible, flat, dependencies):
    payload = {"code": code, "message": "private"}
    if not flat:
        payload = {**completion(), "error": payload}
    with pytest.raises(LlmProcessingException) as exc:
        client_for("zai", dependencies).parse_response(payload)
    assert exc.value.status_code is None
    assert is_fallback_eligible(exc.value) is eligible


def test_zai_network_finish_reason_is_retryable_without_fake_http_status(dependencies):
    with pytest.raises(LlmProcessingException) as exc:
        client_for("zai", dependencies).parse_response(
            completion("partial", "network_error", [tool()])
        )
    assert exc.value.status_code is None
    assert is_fallback_eligible(exc.value)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_tools_reasoning_and_usage_survive_multiple_iterations(provider, dependencies):
    dependencies[0].list_tools.return_value = [
        SimpleNamespace(name="change", description="Change", input_schema={})
    ]
    details = [
        {
            "type": "reasoning.encrypted",
            "data": "opaque",
            "signature": "sig",
            "unknown": {"field": [1, 2]},
        }
    ]
    extra = (
        {"reasoning": "private reasoning", "reasoning_details": details}
        if provider == "openrouter"
        else {"reasoning_content": "private reasoning"}
    )
    responses = [
        completion(None, "tool_calls", [tool(), tool("second", {})], **extra),
        completion(None, "tool_calls", [tool("third", {})], **extra),
        completion(),
    ]
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                **responses[len(requests) - 1],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    token_session = MagicMock()
    db = MagicMock()
    db.session.return_value = AsyncMock()
    db.session.return_value.__aenter__.return_value = token_session
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = create_llm_client(
            settings_for(provider), *dependencies, db_manager=db, http_client=http
        )
        reply = await client.chat("Help", 42)
    assert reply.text == "Final answer"
    for index in (1, 2):
        assistant = [m for m in requests[index]["messages"] if m["role"] == "assistant"][-1]
        for key, value in extra.items():
            assert assistant[key] == value
    assert requests[1]["messages"][-2]["tool_call_id"] == "first"
    assert requests[1]["messages"][-1]["tool_call_id"] == "second"
    assert requests[0]["tool_choice"] == "auto"
    assert requests[0]["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert dependencies[0].call_tool.await_count == 3
    dependencies[2].build_faq_context.assert_awaited_once()
    dependencies[1].add_assistant_message.assert_awaited_once_with(42, "Final answer")
    assert token_session.add.call_count == 3
    assert all(call.args[0].total_tokens == 12 for call in token_session.add.call_args_list)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "usage,expected",
    [
        (None, None),
        ({}, TokenUsage()),
        (
            {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
            TokenUsage(2, 3, 5),
        ),
        ({"total_tokens": 8}, TokenUsage(0, 0, 8)),
        ({"prompt_tokens": -1}, None),
        ({"completion_tokens": "3"}, None),
        ({"total_tokens": None}, None),
        ({"total_tokens": True}, None),
    ],
)
def test_usage_is_optional_and_never_double_counts(provider, usage, expected, dependencies):
    assert client_for(provider, dependencies).extract_usage({"usage": usage}) == expected


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("secret transport detail"),
        httpx.ConnectError("secret transport detail"),
        asyncio.CancelledError(),
    ],
)
async def test_transport_retries_and_cancellation(provider, exception, dependencies):
    seen = []

    def respond(request):
        seen.append(request)
        raise exception

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = client_for(provider, dependencies, http)
        if isinstance(exception, asyncio.CancelledError):
            with pytest.raises(asyncio.CancelledError):
                await client.call_api([], "", 42)
            assert len(seen) == 1
        else:
            with pytest.raises(LlmProcessingException) as exc:
                await client.call_api([], "", 42)
            assert is_fallback_eligible(exc.value)
            assert "secret transport detail" not in str(exc.value)
            assert len(seen) == 3


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_images_rejected_before_http(provider, dependencies):
    client = client_for(provider, dependencies)
    assert not client.supports_images()
    with pytest.raises(LlmProcessingException):
        await client.chat_with_image("image", 42, "image-data", "image/png")
    with pytest.raises(LlmProcessingException):
        client.build_initial_conversation("image", 42, "", "image-data")
    assert client._http_client is None


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("level", ["ERROR", "INFO", "TRACE"])
async def test_logs_hide_keys_and_only_trace_contains_payloads(provider, level, dependencies):
    stream = io.StringIO()
    setup_logging(level, stream=stream)
    settings = settings_for(provider)
    register_settings_secrets(settings)
    payload = completion(
        "private final answer", reasoning="private reasoning", reasoning_content="private reasoning"
    )
    payload["echo"] = "router.arbitrary.secret zai.arbitrary.secret"
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
        ) as http:
            client = create_llm_client(settings, *dependencies, http_client=http)
            await client.call_api([{"role": "user", "content": "private user question"}], "", 42)
        output = stream.getvalue()
        assert "router.arbitrary.secret" not in output
        assert "zai.arbitrary.secret" not in output
        for text in ("private final answer", "private reasoning", "private user question"):
            assert (text in output) is (level == "TRACE")
        assert "arbitrary.secret" not in safe_serialize(settings)
    finally:
        setup_logging("INFO")


def test_reasoning_details_are_copied_without_dropping_unknown_fields(dependencies):
    client = client_for("openrouter", dependencies)
    payload = completion(None, "tool_calls", [tool()], reasoning_details=[{"opaque": ["original"]}])
    parsed = client.parse_response(payload)
    saved = deepcopy(parsed.reasoning_details)
    payload["choices"][0]["message"]["reasoning_details"][0]["opaque"].append("changed")
    conversation = []
    client.add_tool_calls_to_conversation(conversation, parsed)
    conversation[-1]["reasoning_details"][0]["opaque"].append("changed again")
    assert parsed.reasoning_details == saved


@pytest.mark.parametrize("errors", [[None], [{"code": 502}, {"code": 404}], [{}, {}]])
def test_openrouter_malformed_or_conflicting_errors_do_not_fallback(errors, dependencies):
    payload = completion("partial")
    payload["error"] = errors[0]
    if len(errors) > 1:
        payload["choices"][0]["error"] = errors[1]
    with pytest.raises(LlmProcessingException) as exc:
        client_for("openrouter", dependencies).parse_response(payload)
    assert not is_fallback_eligible(exc.value)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_fallback_reuses_completed_tool_effects(provider, dependencies):
    backup = "zai" if provider == "openrouter" else "openrouter"
    primary_host = "openrouter.ai" if provider == "openrouter" else "api.z.ai"
    requests = []
    calls_by_host = {}

    def respond(request):
        host = request.url.host
        calls_by_host[host] = calls_by_host.get(host, 0) + 1
        body = json.loads(request.content)
        requests.append((host, body))
        if calls_by_host[host] == 1:
            extra = (
                {"reasoning": "primary private", "reasoning_details": [{"opaque": "signature"}]}
                if provider == "openrouter"
                else {"reasoning_content": "primary private"}
            )
            return httpx.Response(
                200,
                json=completion(
                    None, "tool_calls", [tool(host)], **(extra if host == primary_host else {})
                ),
            )
        if host == primary_host:
            error = {"code": 502} if provider == "openrouter" else {"code": "1113"}
            return httpx.Response(200, json={"error": error})
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        reply = await client_for(
            provider, dependencies, http, llm_fallback_chain=f"{backup}:backup"
        ).chat("Help", 42)
    assert reply.text == "Final answer"
    assert len(requests) == 4
    dependencies[0].call_tool.assert_awaited_once_with("change", {"id": 42}, 42)
    dependencies[2].build_faq_context.assert_awaited_once()
    dependencies[1].add_user_message.assert_awaited_once_with(42, "Help")
    dependencies[1].add_assistant_message.assert_awaited_once_with(42, "Final answer")
    backup_json = json.dumps(requests[2][1])
    assert "completed action" in backup_json
    assert "primary private" not in backup_json and "signature" not in backup_json


def test_factory_returns_concrete_types(dependencies):
    assert isinstance(client_for("openrouter", dependencies), OpenRouterClient)
    assert isinstance(client_for("zai", dependencies), ZaiClient)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("body", ["not JSON", "[]", "null"])
async def test_invalid_success_json_does_not_fallback(provider, body, dependencies):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body))
    ) as http:
        with pytest.raises(LlmProcessingException) as exc:
            await client_for(provider, dependencies, http).call_api([], "", 42)
    assert not is_fallback_eligible(exc.value)


@pytest.mark.parametrize(
    "provider,extra",
    [
        ("openrouter", {"reasoning": []}),
        ("openrouter", {"reasoning_details": {}}),
        ("openrouter", {"reasoning_details": [1]}),
        ("zai", {"reasoning_content": {}}),
        ("zai", {"error": None}),
    ],
)
def test_invalid_reasoning_metadata_rejected(provider, extra, dependencies):
    payload = completion(**extra)
    if "error" in extra:
        payload.update(extra)
    with pytest.raises(LlmProcessingException):
        client_for(provider, dependencies).parse_response(payload)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_unknown_tool_outcome_never_changes_provider(provider, dependencies):
    dependencies[0].call_tool.side_effect = RuntimeError("unknown outcome")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=completion(None, "tool_calls", [tool()]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(LlmProcessingException) as exc:
            await client_for(provider, dependencies, http, llm_fallback_chain="zai:backup").chat(
                "Help", 42
            )
    assert not is_fallback_eligible(exc.value)
    assert len(seen) == 1
    dependencies[1].add_assistant_message.assert_not_awaited()


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("has_text", [False, True])
async def test_bedolaga_photos_keep_operator_mirroring_and_text_only_behavior(
    provider, has_text, dependencies
):
    from app.bedolaga.types import TicketMedia, TicketMessage
    from tests.test_bedolaga_pipeline import TICKET_ID, _answerer, _ticket

    ticket = _ticket(
        TicketMessage(id=100, text="", is_from_admin=False, has_media=True, media_type="photo"),
        title="Не подключается" if has_text else "",
    )
    answerer, parts = _answerer(ticket=ticket)
    media = TicketMedia(
        media_type="photo",
        media_url="https://example.invalid/pic.png",
        filename="pic.png",
        mime_type="image/png",
    )
    parts["client"].describe_media.return_value = media
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        answerer.llm_client = client_for(provider, dependencies, http)
        await answerer.handle(TICKET_ID)
    parts["client"].download_image.assert_not_awaited()
    assert len(requests) == int(has_text)
    if has_text:
        assert requests[0]["messages"][-1]["content"] == "Не подключается"
        assert all(isinstance(m["content"], str) for m in requests[0]["messages"])
    else:
        assert "оператор" in parts["client"].reply.await_args.args[1].lower()
    kwargs = parts["forwarder"].forward_ticket_media.await_args.kwargs
    assert kwargs["ticket_media"] == media
    parts["state"].record_mirrored_media.assert_awaited_once_with(TICKET_ID, 100)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_offline_behavior_evaluator_uses_new_provider(provider):
    from benchmarks.agent_behavior_eval import BehaviorCase, run_once

    case = BehaviorCase(
        name="provider smoke",
        user_message="Помоги подключиться",
        expect_no_tools=True,
        must_contain_any=["ответ"],
        history=[{"role": "user", "content": "history marker"}],
    )
    seen = []

    def respond(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion("Вот ответ"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        results = await run_once(cases=[case], settings=settings_for(provider), http_client=http)
    assert len(results) == 1 and results[0].passed
    assert {"role": "user", "content": "history marker"} in seen[0]["messages"]
    assert seen[0]["tools"]


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    "content,expected",
    [
        ("<think>private</think>Answer", "Answer"),
        ("<THINK mode='x'>private\nreasoning</THINK>\nAnswer", "Answer"),
        ("Answer<think>private unfinished", "Answer"),
        ("<think>only reasoning", None),
    ],
)
async def test_raw_reasoning_never_reaches_reply_or_history(
    provider, content, expected, dependencies
):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=completion(content)))
    ) as http:
        client = client_for(provider, dependencies, http)
        if expected is None:
            with pytest.raises(LlmProcessingException):
                await client.chat("Help", 42)
            dependencies[1].add_assistant_message.assert_not_awaited()
        else:
            assert (await client.chat("Help", 42)).text == expected
            dependencies[1].add_assistant_message.assert_awaited_once_with(42, expected)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_image_fallback_skips_new_text_clients(provider, dependencies):
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Image answer"}],
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = client_for(
            provider,
            dependencies,
            http,
            llm_fallback_chain="openai:gpt-5.6-luna",
            openai_api_key="sk-test-vision-key",
        )
        assert client.supports_images()
        assert (
            await client.chat_with_image("Help", 42, "cGhvdG8=", "image/png")
        ).text == "Image answer"
    assert [r.url.host for r in seen] == ["api.openai.com"]
    assert "input_image" in seen[0].content.decode()


@pytest.mark.parametrize("provider,model", [("openrouter", "z-ai/glm-5.3"), ("zai", "glm-5.3")])
def test_invalid_reasoning_on_fallback_rejected_before_http(provider, model, dependencies):
    with pytest.raises(ValueError, match="REASONING_EFFORT=low"):
        client_for("zai", dependencies, llm_fallback_chain=f"{provider}:{model}")
