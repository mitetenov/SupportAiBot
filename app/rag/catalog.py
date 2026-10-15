"""Local FAQ references and lossless deduplication of shared opening steps."""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from pathlib import Path

from app.logging_config import log_failure
from app.rag.types import FaqResult

DEFAULT_FAQ_PATH = Path(__file__).resolve().parents[2] / "faq" / "faq.json"
logger = logging.getLogger(__name__)


class FaqCatalog:
    """Keep explicit support instructions available even when retrieval misses."""

    def __init__(self, entries: list[dict]) -> None:
        self.by_id: dict[str, dict] = {}
        for entry in entries:
            if entry.get("id"):
                if entry["id"] in self.by_id:
                    raise ValueError(f"Duplicate FAQ id: {entry['id']}")
                self.by_id[entry["id"]] = entry

    @classmethod
    def load(cls, path: Path = DEFAULT_FAQ_PATH) -> FaqCatalog:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            # As with FaqInitializer, a missing/unreadable source must not stop
            # the bot or prevent ordinary search against its retained index.
            log_failure(logger, "Local FAQ references unavailable")
            return cls([])
        return cls(entries)

    def required_ids(self, query: str) -> list[str]:
        text = query.lower()
        ids: list[str] = []
        if re.search(r"автопрод|автоплат|продлен\w*.*(?:баланс|сч[её]т)", text):
            ids.append("auto_renewal")
        if re.search(r"триал|пробн|на пробу", text):
            if re.search(r"подар|получател", text):
                ids.append("gift_recipient")
            else:
                ids.append("trial")
        if re.search(r"ссылк|\burl\b|\bурл\b|qr", text) and re.search(r"подпис|vpn|впн", text):
            ids.append("subscription_link")
        if re.search(r"установить|скачать|подключить|установк", text) and re.search(
            r"vpn|впн|happ|incy|устройств|телефон|планшет|ноутбук|компьютер|linux|линукс", text
        ):
            ids.append("device_setup")
        if re.search(r"n/a|не пингу|нет пинга|серверы.*не отвечают", text):
            ids.extend(("na_diagnosis", "na_apple", "na_windows_android"))
        if re.search(r"где.*кноп|найти.*кноп|какая.*кноп", text):
            ids.append("app_buttons")
        return ids

    def supplement(
        self, query: str, results: list[FaqResult], excluded: set[str] | None = None
    ) -> list[FaqResult]:
        """Append exact local references; never claim they were semantic hits."""
        combined = list(results)
        seen = {result.question for result in results}
        for key in self.required_ids(query):
            entry = self.by_id.get(key)
            if not entry or entry["question"] in seen or entry["question"] in (excluded or set()):
                continue
            combined.append(
                FaqResult(entry["question"], entry["answer"], 0.0, 0.0, entry.get("image"))
            )
            seen.add(entry["question"])
        return combined


def format_faq_context(results: list[FaqResult]) -> str:
    """Show identical long two-paragraph prefixes once, preserving their order.

    Indexed answers, retrieval scores, illustration metadata and the remaining
    instructions are untouched. Only exact opening text is shared; similar or
    differently conditioned instructions must remain separate.
    """
    prefixes = []
    for result in results:
        paragraphs = result.answer.split("\n\n")
        prefix = "\n\n".join(paragraphs[:2]) if len(paragraphs) > 2 else ""
        prefixes.append(prefix if len(prefix) >= 240 else "")
    counts = Counter(prefix for prefix in prefixes if prefix)
    shared = {
        prefix: index + 1
        for index, prefix in enumerate(
            dict.fromkeys(prefix for prefix in prefixes if prefix and counts[prefix] > 1)
        )
    }
    parts = ["Кандидаты FAQ — справочные данные; применяй только подходящие условия и шаги.\n"]
    for prefix, number in shared.items():
        parts.append(
            f"Общие первые шаги {number} (для статей со ссылкой на этот блок):\n{prefix}\n\n"
        )
    for result, prefix in zip(results, prefixes, strict=True):
        answer = result.answer
        if prefix in shared:
            answer = f"Сначала общие шаги {shared[prefix]}.\n\n{answer[len(prefix) + 2 :]}"
        parts.append(f"Вопрос: {result.question}\nИнструкция: {answer}\n\n")
    return "".join(parts)
