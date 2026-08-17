#!/usr/bin/env python3
"""Print local Codex and Claude token usage by model."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


USER_HOME = Path.home()
CODEX_DB = USER_HOME / ".codex" / "state_5.sqlite"
CODEX_DIRS = (
    USER_HOME / ".codex" / "sessions",
    USER_HOME / ".codex" / "archived_sessions",
)
CLAUDE_PROJECTS = USER_HOME / ".claude" / "projects"
LOCAL_TZ = ZoneInfo(os.environ.get("TOKSCALE_TZ", "Europe/Oslo"))

# USD per million tokens. Claude cache writes use the 5-minute rate because
# local transcripts do not record the cache TTL.
CLAUDE_RATES = {
    "claude-fable-5": {
        "input": 10.0,
        "output": 50.0,
        "cache_read": 1.0,
        "cache_write": 12.5,
    },
    "claude-opus-5": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-sonnet-5": {
        "input": 2.0,
        "output": 10.0,
        "cache_read": 0.2,
        "cache_write": 2.5,
    },
}

# API-equivalent USD per million total tokens used by this report.
OPENAI_RATES = {
    "gpt-5.6-sol": 0.8,
    "codex-auto-review": 0.459224,
    "gpt-5.6-luna": 0.0177545,
}


def positive_int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def local_time(value) -> datetime:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(LOCAL_TZ)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(LOCAL_TZ)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ)


def usage_event(
    model,
    timestamp,
    *,
    input_tokens=0,
    output_tokens=0,
    cache_read=0,
    cache_write=0,
    total=None,
) -> dict:
    input_tokens = positive_int(input_tokens)
    output_tokens = positive_int(output_tokens)
    cache_read = positive_int(cache_read)
    cache_write = positive_int(cache_write)
    calculated_total = input_tokens + output_tokens + cache_read + cache_write
    return {
        "model": model or "unknown",
        "timestamp": local_time(timestamp),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "tokens": positive_int(total) if total is not None else calculated_total,
    }


def scan_codex_rollouts() -> list[dict]:
    """Read per-turn usage; cached input is part of input, not extra tokens."""
    events = []
    for base in CODEX_DIRS:
        if not base.exists():
            continue
        for path in base.rglob("*.jsonl"):
            model = None
            try:
                with path.open("r", encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        payload = record.get("payload") or {}
                        if record.get("type") == "turn_context" and payload.get("model"):
                            model = payload["model"]
                        usage = (payload.get("info") or {}).get("last_token_usage")
                        if not model or not usage:
                            continue
                        events.append(
                            usage_event(
                                model,
                                record.get("timestamp"),
                                input_tokens=usage.get("input_tokens"),
                                output_tokens=usage.get("output_tokens"),
                                cache_read=usage.get("cached_input_tokens"),
                                cache_write=usage.get("cache_write_input_tokens"),
                                total=usage.get("total_tokens"),
                            )
                        )
            except OSError:
                continue
    return events


def scan_codex() -> list[dict]:
    events = scan_codex_rollouts()
    if events or not CODEX_DB.exists():
        return events

    try:
        database = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT model, updated_at, tokens_used "
            "FROM threads WHERE tokens_used > 0"
        ).fetchall()
        database.close()
    except sqlite3.Error as error:
        print(f"warning: could not read Codex database: {error}", file=sys.stderr)
        return []

    return [
        usage_event(row["model"], row["updated_at"], total=row["tokens_used"])
        for row in rows
    ]


def scan_claude() -> list[dict]:
    if not CLAUDE_PROJECTS.exists():
        return []

    events = []
    for path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") != "assistant":
                        continue
                    message = record.get("message") or {}
                    usage = message.get("usage") or {}
                    model = message.get("model")
                    if not usage or not model or model == "<synthetic>":
                        continue
                    events.append(
                        usage_event(
                            model,
                            record.get("timestamp"),
                            input_tokens=usage.get("input_tokens"),
                            output_tokens=usage.get("output_tokens"),
                            cache_read=usage.get("cache_read_input_tokens"),
                            cache_write=usage.get("cache_creation_input_tokens"),
                        )
                    )
        except OSError:
            continue
    return events


def event_cost(event: dict) -> float | None:
    model = event["model"]
    if model in CLAUDE_RATES:
        rate = CLAUDE_RATES[model]
        return (
            event["input_tokens"] * rate["input"]
            + event["output_tokens"] * rate["output"]
            + event["cache_read_tokens"] * rate["cache_read"]
            + event["cache_write_tokens"] * rate["cache_write"]
        ) / 1_000_000
    if model in OPENAI_RATES:
        return event["tokens"] * OPENAI_RATES[model] / 1_000_000
    return None


def collect() -> tuple[list[dict], datetime | None, datetime | None]:
    events = scan_codex() + scan_claude()
    models = {}
    for event in events:
        row = models.setdefault(
            event["model"],
            {
                "model": event["model"],
                "tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "events": 0,
                "cost": 0.0,
                "cost_known": True,
            },
        )
        for field in (
            "tokens",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            row[field] += event[field]
        row["events"] += 1
        cost = event_cost(event)
        if cost is None:
            row["cost_known"] = False
        else:
            row["cost"] += cost

    dates = [event["timestamp"] for event in events]
    ordered = sorted(models.values(), key=lambda row: row["tokens"], reverse=True)
    return ordered, min(dates, default=None), max(dates, default=None)


def format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_cost(value: float, known: bool) -> str:
    if not known:
        return "—"
    if value >= 1000:
        return f"${value / 1000:.1f}K"
    if 0 < value < 0.01:
        return f"${value:.3f}"
    return f"${value:.2f}"


def interval(first: datetime | None, last: datetime | None) -> str:
    if not first or not last:
        return "No data"
    first_label = f"{first.strftime('%b')} {first.day}"
    last_label = f"{last.strftime('%b')} {last.day}"
    return first_label if first.date() == last.date() else f"{first_label} – {last_label}"


def print_models(breakdown: bool) -> None:
    models, first, last = collect()
    print("tokscale · Models")
    print(f"range: {interval(first, last)}")
    print()
    print(f"{'Model':<30} {'Tokens':>12} {'Cost':>12}")
    print("-" * 58)
    for model in models:
        cost = format_cost(model["cost"], model["cost_known"])
        print(f"{model['model']:<30} {format_tokens(model['tokens']):>12} {cost:>12}")

    if not breakdown:
        return
    print()
    print("Breakdown")
    print("---------")
    print(
        f"{'Model':<24} {'Input':>10} {'Output':>10} "
        f"{'Cache read':>12} {'Cache write':>13} {'Events':>8}"
    )
    print("-" * 83)
    for model in models:
        print(
            f"{model['model']:<24} "
            f"{format_tokens(model['input_tokens']):>10} "
            f"{format_tokens(model['output_tokens']):>10} "
            f"{format_tokens(model['cache_read_tokens']):>12} "
            f"{format_tokens(model['cache_write_tokens']):>13} "
            f"{model['events']:>8}"
        )
    print()
    print("Note: Codex cache read is included inside Input and is not added twice.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Report local Codex and Claude token usage")
    commands = parser.add_subparsers(dest="command", required=True)
    models = commands.add_parser("models", help="show usage grouped by model")
    models.add_argument("--breakdown", action="store_true", help="show token categories")
    args = parser.parse_args()
    print_models(args.breakdown)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
