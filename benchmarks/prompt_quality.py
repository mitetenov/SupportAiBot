"""Measure real model behavior and request cost on synthetic support scenarios.

Production MCP servers are never contacted. Reports contain synthetic answers,
request sizes and provider usage, not API keys or raw HTTP requests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.constants import SupportPrompt
from benchmarks.agent_behavior_eval import CASES_PATH, FAQ_PATH, BehaviorCase, load_cases, run_once


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def context_sizes(payload: dict[str, Any]) -> dict[str, int]:
    """Break down serialized context size; these are characters, not tokens."""
    messages = payload.get("messages", [])
    sizes = {"system": 0, "dynamic": 0, "conversation": 0, "tool_results": 0}
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            group = "tool_results"
        elif message.get("role") == "system":
            group = "system" if index == 0 else "dynamic"
        else:
            group = "conversation"
        sizes[group] += json_size(message)
    sizes["tool_definitions"] = json_size(payload.get("tools", []))
    return sizes


def usage_counts(payload: dict[str, Any]) -> dict[str, int | None]:
    """Keep absent usage/cache counters unknown instead of reporting zero."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}

    def count(value: Any) -> int | None:
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    return {
        "input_tokens": count(usage.get("prompt_tokens")),
        "output_tokens": count(usage.get("completion_tokens")),
        "cached_input_tokens": count(details.get("cached_tokens")),
    }


class RequestObserver:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def on_request(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        record = {
            "context_chars": context_sizes(payload),
            "status": None,
            "seconds": None,
            "input_tokens": None,
            "output_tokens": None,
            "cached_input_tokens": None,
        }
        self.requests.append(record)
        request.extensions["quality_observation"] = (record, time.monotonic())

    async def on_response(self, response: httpx.Response) -> None:
        record, started = response.request.extensions["quality_observation"]
        await response.aread()
        record["seconds"] = round(time.monotonic() - started, 3)
        record["status"] = response.status_code
        try:
            payload = response.json()
        except ValueError:
            return
        if isinstance(payload, dict):
            record.update(usage_counts(payload))


async def measure_case(case: BehaviorCase, settings: Settings) -> dict[str, Any]:
    observer = RequestObserver()
    started = time.monotonic()
    async with httpx.AsyncClient(
        event_hooks={"request": [observer.on_request], "response": [observer.on_response]}
    ) as client:
        try:
            result = (await run_once([case], settings=settings, http_client=client))[0]
            record = asdict(result)
        except Exception as error:
            # Raw exception text can contain HTTP/authentication details.
            record = {"name": case.name, "passed": False, "error_type": type(error).__name__}
    return {
        **record,
        "seconds": round(time.monotonic() - started, 3),
        "requests": observer.requests,
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_case: dict[str, dict[str, int]] = {}
    for record in records:
        item = by_case.setdefault(record["name"], {"passed": 0, "runs": 0})
        item["runs"] += 1
        item["passed"] += int(record["passed"])
    requests = [request for record in records for request in record["requests"]]
    totals: dict[str, int | None] = {}
    for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
        values = [request.get(field) for request in requests]
        totals[field] = sum(values) if values and all(v is not None for v in values) else None
    return {"cases": by_case, "api_requests": len(requests), "usage": totals}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--cases-file", type=Path, default=CASES_PATH)
    parser.add_argument("--confirm-external-api", action="store_true")
    args = parser.parse_args()
    if args.runs < 1 or not 1 <= args.concurrency <= 4:
        parser.error("runs must be positive; concurrency must be between 1 and 4")
    if not args.confirm_external_api:
        parser.error("live evaluation requires --confirm-external-api")
    if args.report.exists():
        parser.error("report already exists; choose a new path to preserve earlier measurements")

    cases = load_cases(args.cases_file)
    if args.case:
        missing = set(args.case) - {case.name for case in cases}
        if missing:
            parser.error(f"unknown cases: {', '.join(sorted(missing))}")
        cases = [case for case in cases if case.name in args.case]

    settings = Settings(
        _env_file=args.env_file,
        llm_provider="zai",
        zai_model="glm-5.3-flash",
        llm_fallback_chain="",
    )
    report: dict[str, Any] = {
        "label": args.label,
        "model": settings.zai_model,
        "reasoning_effort": settings.reasoning_effort,
        "prompt_chars": len(SupportPrompt.SYSTEM),
        "prompt_sha256": hashlib.sha256(SupportPrompt.SYSTEM.encode()).hexdigest(),
        "faq_sha256": hashlib.sha256(FAQ_PATH.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256(args.cases_file.read_bytes()).hexdigest(),
        "results": [],
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    async def evaluate(run: int, case: BehaviorCase) -> None:
        async with semaphore:
            record = {"run": run, **await measure_case(case, settings)}
            report["results"].append(record)
            report["summary"] = summarize(report["results"])
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            verdict = "PASS" if record["passed"] else "FAIL"
            print(
                f"{run}/{args.runs} {verdict} {case.name}: {len(record['requests'])} API requests",
                flush=True,
            )

    # Retain partial reports on interruption; neither model failures nor missing
    # usage data disappear from the result set.
    await asyncio.gather(
        *(evaluate(run, case) for run in range(1, args.runs + 1) for case in cases)
    )
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)
    return 0 if all(record["passed"] for record in report["results"]) else 1


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    raise SystemExit(asyncio.run(main()))
