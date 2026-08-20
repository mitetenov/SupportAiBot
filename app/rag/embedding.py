"""Embedding provider protocols and implementations for Gemini and OpenAI APIs."""

import logging
from typing import Protocol, runtime_checkable

import httpx

from app.config import Settings, reveal

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol defining the interface for vector embedding providers."""

    def get_dimension(self) -> int:
        """Return the vector dimensionality produced by this provider."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Generate a vector embedding for the given text.

        Returns an empty list on failure or when input is blank.
        """
        ...


class GeminiEmbeddingProvider:
    """Embedding provider using Google Gemini's REST API."""

    DIMENSION: int = 2000
    MODEL: str = "gemini-embedding-001"
    DEFAULT_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client = client

    def get_dimension(self) -> int:
        """Return the vector dimensionality (2000)."""
        return self.DIMENSION

    async def embed(self, text: str) -> list[float]:
        """Generate a 2000-dimensional embedding using Gemini."""
        if not text or not text.strip():
            return []

        url = f"{self.base_url}/models/{self.MODEL}:embedContent"
        headers = {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "content": {
                "parts": [{"text": text}],
            },
            "outputDimensionality": self.DIMENSION,
        }

        try:
            if self._client is not None:
                response = await self._client.post(url, headers=headers, json=payload, timeout=60.0)
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                logger.error(
                    "Gemini embedding failed with status %d: %s",
                    response.status_code,
                    response.text,
                )
                return []

            data = response.json()
            embedding_node = data.get("embedding")
            if not embedding_node or not isinstance(embedding_node, dict):
                logger.error("No embedding object in Gemini response: %s", response.text)
                return []

            values = embedding_node.get("values")
            if not values or not isinstance(values, list):
                logger.error("Unexpected values in Gemini embedding response: %s", response.text)
                return []

            return [float(x) for x in values]

        except Exception as e:
            logger.error("Gemini embedding request exception: %s", e)
            return []


class OpenAiEmbeddingProvider:
    """Embedding provider using OpenAI's REST API."""

    DIMENSION: int = 1536
    DEFAULT_MODEL: str = "text-embedding-3-small"
    DEFAULT_BASE_URL: str = "https://api.openai.com/v1"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._client = client

    def get_dimension(self) -> int:
        """Return the vector dimensionality (1536)."""
        return self.DIMENSION

    async def embed(self, text: str) -> list[float]:
        """Generate a 1536-dimensional embedding using OpenAI."""
        if not text or not text.strip():
            return []

        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "input": text,
            "dimensions": self.DIMENSION,
        }

        try:
            if self._client is not None:
                response = await self._client.post(url, headers=headers, json=payload, timeout=60.0)
            else:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    response = await client.post(url, headers=headers, json=payload)

            if response.status_code != 200:
                logger.error(
                    "OpenAI embedding failed with status %d: %s",
                    response.status_code,
                    response.text,
                )
                return []

            data = response.json()
            data_list = data.get("data")
            if not data_list or not isinstance(data_list, list):
                logger.error("No data array in OpenAI embedding response: %s", response.text)
                return []

            first_entry = data_list[0]
            embedding = first_entry.get("embedding") if isinstance(first_entry, dict) else None
            if not embedding or not isinstance(embedding, list):
                logger.error("Unexpected embedding format in OpenAI response: %s", response.text)
                return []

            return [float(x) for x in embedding]

        except Exception as e:
            logger.error("OpenAI embedding request exception: %s", e)
            return []


def create_embedding_provider(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> EmbeddingProvider:
    """Instantiate and return the configured EmbeddingProvider."""
    provider_name = settings.embedding_provider.strip().lower()
    if provider_name == "openai":
        return OpenAiEmbeddingProvider(
            api_key=reveal(settings.openai_api_key),
            base_url=settings.openai_base_url,
            model=settings.openai_embedding_model,
            client=client,
        )
    elif provider_name == "gemini":
        return GeminiEmbeddingProvider(
            api_key=reveal(settings.gemini_api_key),
            base_url=settings.gemini_base_url,
            client=client,
        )
    else:
        raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
