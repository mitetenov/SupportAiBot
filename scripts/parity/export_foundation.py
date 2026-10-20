"""Regenerate offline Go parity fixtures from the pinned Python implementation."""

import json
import re
from pathlib import Path

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.config import Settings
from app.llm.rejection import is_rejection
from app.rag.catalog import FaqCatalog, format_faq_context
from app.rag.embedding import GeminiEmbeddingProvider, OpenAiEmbeddingProvider
from app.rag.service import FaqEmbeddingService
from app.rag.types import FaqResult
from app.storage.models import Base

queries = [
    "Как скачать VPN на iPhone?",
    "Автопродление с баланса",
    "Пробный подарок другу",
    "Где ссылка подписки QR?",
    "N/A серверы не отвечают",
    "Где найти кнопку?",
    "ёжик and --- !",
    "🚀",
    "Установить Linux",
    "Это только вопрос",
]
long = "Шаги " + ("общий текст " * 30)
results = [
    FaqResult("A", long + "\n\n" + long + "\n\nОстальное А", 0.9, 0.04),
    FaqResult("B", long + "\n\n" + long + "\n\nОстальное Б", 0.8, 0.03),
]
cat = FaqCatalog([])
fp = []
for p in [OpenAiEmbeddingProvider("unused"), GeminiEmbeddingProvider("unused")]:
    fp.append(
        {
            "provider": p.__class__.__name__,
            "fingerprint": FaqEmbeddingService(
                db_manager=None, embedding_provider=p
            ).get_index_fingerprint("source-hash"),
        }
    )
output = {
    "queries": [
        {
            "input": q,
            "clean": re.sub(r"[^a-zA-Zа-яА-Я0-9\s]", " ", q).strip() or q,
            "required": cat.required_ids(q),
        }
        for q in queries
    ],
    "format": {"results": [r.__dict__ for r in results], "text": format_faq_context(results)},
    "fingerprints": fp,
}
Path("internal/rag/testdata/python_foundation.json").write_text(
    json.dumps(output, ensure_ascii=False, indent=2) + "\n"
)
texts = [
    "Не только",
    "другое устройство",
    "не товарищ",
    "подключить другое",
    "другое!",
    "это не то.",
    "Всё ещё не заработало",
    "по-прежнему",
    "Всё равно не помогает",
    "другое?!",
    "не помогите",
    "не та-версия",
]
Path("internal/storage/testdata/python_rejection.json").write_text(
    json.dumps(
        [{"text": q, "rejected": is_rejection(q)} for q in texts], ensure_ascii=False, indent=2
    )
    + "\n"
)
Path("internal/storage/testdata/python_schema.sql").write_text(
    "CREATE EXTENSION IF NOT EXISTS vector;\n"
    + ";\n".join(
        str(CreateTable(t).compile(dialect=postgresql.dialect()))
        for t in Base.metadata.sorted_tables
    )
    + ";\n"
)


cases = [
    {},
    {"BOT_LOG_LEVEL": "trace"},
    {"BOT_LOG_LEVEL": "debug"},
    {"REASONING_EFFORT": "auto"},
    {"REASONING_EFFORT": " HIGH "},
    {"TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS": "1,garbage,5000000001"},
    {"TELEGRAM_SUPPORT_ADMIN_TELEGRAM_IDS": "[1,2]"},
    {"BEDOLAGA_MCP_ENABLED": "yes", "BEDOLAGA_MCP_URL": "http://fixture"},
    {"BEDOLAGA_MCP_ENABLED": "off"},
    {"PGVECTOR_PORT": " 5432 "},
    {"LLM_FALLBACK_CHAIN": " "},
    {"LLM_FALLBACK_CHAIN": "groq:model,"},
    {"LLM_FALLBACK_CHAIN": "groq:model,,zai:m"},
    {"OPENAI_TEMPERATURE": ""},
]
rows = []
for values in cases:
    env = {
        "TELEGRAM_BOT_TOKEN": "fixture",
        "TELEGRAM_SUPPORT_GROUP_CHAT_ID": "-100",
        "OPENAI_API_KEY": "sk-test",
        "GEMINI_API_KEY": "fixture",
        **values,
    }
    try:
        Settings(_env_file=None, **{k.lower(): v for k, v in env.items()})
        valid = True
    except ValueError:
        valid = False
    rows.append({"env": env, "valid": valid})
Path("internal/config/testdata").mkdir(exist_ok=True)
Path("internal/config/testdata/python_config.json").write_text(json.dumps(rows, indent=2) + "\n")
