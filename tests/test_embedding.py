"""Unit tests for EmbeddingProvider implementations (Gemini and OpenAI)."""

import json

import httpx
import pytest

from app.rag.embedding import (
    EmbeddingProvider,
    GeminiEmbeddingProvider,
    OpenAiEmbeddingProvider,
)


class TestEmbeddingProviderProtocol:
    """Test that embedding providers conform to the EmbeddingProvider protocol."""

    def test_gemini_is_instance_of_protocol(self) -> None:
        provider = GeminiEmbeddingProvider(api_key="test-key")
        assert isinstance(provider, EmbeddingProvider)
        assert provider.get_dimension() == 2000

    def test_openai_is_instance_of_protocol(self) -> None:
        provider = OpenAiEmbeddingProvider(api_key="sk-test-key")
        assert isinstance(provider, EmbeddingProvider)
        assert provider.get_dimension() == 1536


class TestGeminiEmbeddingProvider:
    """Test suite for GeminiEmbeddingProvider."""

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        base_url = "https://generativelanguage.googleapis.com/v1beta"
        mock_embedding = [0.1] * 2000

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/models/gemini-embedding-001:embedContent")
            assert request.headers.get("x-goog-api-key") == "gemini-test-key"
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["content"]["parts"][0]["text"] == "Как настроить VPN?"
            assert payload["outputDimensionality"] == 2000
            return httpx.Response(200, json={"embedding": {"values": mock_embedding}})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = GeminiEmbeddingProvider(
                api_key="gemini-test-key",
                base_url=base_url,
                client=client,
            )
            result = await provider.embed("Как настроить VPN?")

        assert len(result) == 2000
        assert result == mock_embedding

    @pytest.mark.asyncio
    async def test_embed_handles_http_error(self) -> None:
        base_url = "https://generativelanguage.googleapis.com/v1beta"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Internal Server Error"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = GeminiEmbeddingProvider(
                api_key="gemini-test-key",
                base_url=base_url,
                client=client,
            )
            result = await provider.embed("test text")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_handles_malformed_json_response(self) -> None:
        base_url = "https://generativelanguage.googleapis.com/v1beta"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"unexpected": {}})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = GeminiEmbeddingProvider(
                api_key="gemini-test-key",
                base_url=base_url,
                client=client,
            )
            result = await provider.embed("test text")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_empty_text_returns_empty(self) -> None:
        provider = GeminiEmbeddingProvider(api_key="gemini-test-key")
        result = await provider.embed("")
        assert result == []
        result_none_or_blank = await provider.embed("   ")
        assert result_none_or_blank == []


class TestOpenAiEmbeddingProvider:
    """Test suite for OpenAiEmbeddingProvider."""

    @pytest.mark.asyncio
    async def test_embed_success(self) -> None:
        base_url = "https://api.openai.com/v1"
        mock_embedding = [0.05] * 1536

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/embeddings")
            assert request.headers.get("authorization") == "Bearer sk-openai-key"
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["model"] == "text-embedding-3-small"
            assert payload["input"] == "Как оплатить подписку?"
            assert payload["dimensions"] == 1536
            return httpx.Response(200, json={"data": [{"embedding": mock_embedding}]})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = OpenAiEmbeddingProvider(
                api_key="sk-openai-key",
                base_url=base_url,
                model="text-embedding-3-small",
                client=client,
            )
            result = await provider.embed("Как оплатить подписку?")

        assert len(result) == 1536
        assert result == mock_embedding

    @pytest.mark.asyncio
    async def test_embed_handles_http_error(self) -> None:
        base_url = "https://api.openai.com/v1"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = OpenAiEmbeddingProvider(
                api_key="sk-invalid",
                base_url=base_url,
                client=client,
            )
            result = await provider.embed("test query")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_handles_malformed_response(self) -> None:
        base_url = "https://api.openai.com/v1"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url=base_url) as client:
            provider = OpenAiEmbeddingProvider(
                api_key="sk-key",
                base_url=base_url,
                client=client,
            )
            result = await provider.embed("test query")

        assert result == []

    @pytest.mark.asyncio
    async def test_embed_empty_text_returns_empty(self) -> None:
        provider = OpenAiEmbeddingProvider(api_key="sk-key")
        result = await provider.embed("")
        assert result == []
        result_blank = await provider.embed("   \t\n ")
        assert result_blank == []
