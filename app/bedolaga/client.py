"""The only code in this bot that talks to the Bedolaga Web API."""

import logging

import httpx

from app.bedolaga.types import OPEN_STATUSES, Ticket, ticket_from_payload
from app.retry import post_with_retry

logger = logging.getLogger(__name__)

#: What `TicketReplyRequest.message_text` accepts; longer bodies are rejected.
MAX_REPLY_LENGTH: int = 4000

#: How many tickets one status query brings back per sweep.
DEFAULT_LIST_LIMIT: int = 50


class BedolagaClient:
    """Reads tickets from Bedolaga and answers them under a service API key."""

    def __init__(self, base_url: str, api_key: str, http_client: httpx.AsyncClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.http_client = http_client
        # A panel user id never changes its Telegram id, and a busy ticket asks
        # for the same one on every turn.
        self._telegram_ids: dict[int, int] = {}

    @property
    def headers(self) -> dict[str, str]:
        """The service-token header every Web API endpoint requires."""
        return {"X-API-Key": self.api_key}

    async def get_ticket(self, ticket_id: int) -> Ticket | None:
        """Read one ticket with its messages, or None when it cannot be read.

        Events carry a truncated preview at best, so every answer starts here.
        """
        try:
            response = await self.http_client.get(
                f"{self.base_url}/tickets/{ticket_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read ticket %d: %s", ticket_id, e)
            return None

        if response.status_code != 200:
            logger.warning(
                "Bedolaga: reading ticket %d returned %d", ticket_id, response.status_code
            )
            return None

        try:
            return ticket_from_payload(response.json())
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Bedolaga: ticket %d came back malformed: %s", ticket_id, e)
            return None

    async def list_awaiting_ticket_ids(self, limit: int = DEFAULT_LIST_LIMIT) -> list[int]:
        """Ids of tickets in a status that means somebody is still waiting.

        The list endpoint serialises tickets without their messages, so it can
        only ever answer "which ones" — the caller reads each one in full.
        """
        ids: list[int] = []
        for status in sorted(OPEN_STATUSES):
            try:
                response = await self.http_client.get(
                    f"{self.base_url}/tickets",
                    headers=self.headers,
                    params={"status": status, "limit": limit},
                )
            except httpx.HTTPError as e:
                logger.warning("Bedolaga: could not list %s tickets: %s", status, e)
                continue

            if response.status_code != 200:
                logger.warning(
                    "Bedolaga: listing %s tickets returned %d", status, response.status_code
                )
                continue

            for item in response.json() or []:
                ticket_id = item.get("id")
                if ticket_id is not None:
                    ids.append(int(ticket_id))
        return ids

    async def reply(self, ticket_id: int, text: str) -> bool:
        """Post an answer into the ticket. True when Bedolaga accepted it.

        The panel takes it from here: the message is stored as an admin reply,
        the ticket flips to `answered`, and the user gets a Telegram
        notification plus a live update in the cabinet.
        """
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/tickets/{ticket_id}/reply",
                headers=self.headers,
                json={"message_text": text[:MAX_REPLY_LENGTH]},
                description=f"bedolaga reply to ticket {ticket_id}",
            )
        except httpx.HTTPError as e:
            logger.error("Bedolaga: replying to ticket %d failed: %s", ticket_id, e)
            return False

        if response.status_code not in (200, 201):
            logger.error(
                "Bedolaga: replying to ticket %d returned %d: %s",
                ticket_id,
                response.status_code,
                response.text[:200],
            )
            return False
        return True

    async def set_priority(self, ticket_id: int, priority: str) -> bool:
        """Raise or lower a ticket's priority. Best effort: never raises."""
        try:
            response = await post_with_retry(
                self.http_client,
                f"{self.base_url}/tickets/{ticket_id}/priority",
                headers=self.headers,
                json={"priority": priority},
                description=f"bedolaga priority for ticket {ticket_id}",
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: setting priority on ticket %d failed: %s", ticket_id, e)
            return False
        return response.status_code == 200

    async def resolve_telegram_id(self, user_id: int) -> int | None:
        """The Telegram id behind a panel user id, or None when there is none.

        Cabinet accounts created by email or OAuth have no Telegram id at all,
        which is a normal answer here rather than a failure.
        """
        cached = self._telegram_ids.get(user_id)
        if cached is not None:
            return cached

        try:
            response = await self.http_client.get(
                f"{self.base_url}/users/{user_id}",
                headers=self.headers,
            )
        except httpx.HTTPError as e:
            logger.warning("Bedolaga: could not read user %d: %s", user_id, e)
            return None

        if response.status_code != 200:
            logger.warning("Bedolaga: reading user %d returned %d", user_id, response.status_code)
            return None

        telegram_id = (response.json() or {}).get("telegram_id")
        if telegram_id is None:
            return None

        resolved = int(telegram_id)
        self._telegram_ids[user_id] = resolved
        return resolved
