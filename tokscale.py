#!/usr/bin/env python3
"""Print local Codex and Claude token usage by model."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from itertools import chain
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
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
CLAUDE_RATES = {
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
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


def scan_codex_rollouts():
    """Read per-turn usage; cached input is part of input, not extra tokens."""
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
                        yield usage_event(
                            model,
                            record.get("timestamp"),
                            input_tokens=usage.get("input_tokens"),
                            output_tokens=usage.get("output_tokens"),
                            cache_read=usage.get("cached_input_tokens"),
                            cache_write=usage.get("cache_write_input_tokens"),
                            total=usage.get("total_tokens"),
                        )
            except OSError:
                continue


def scan_codex():
    rollouts = iter(scan_codex_rollouts())
    first = next(rollouts, None)
    if first is not None:
        yield first
        yield from rollouts
        return
    if not CODEX_DB.exists():
        return

    database = None
    try:
        database = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        database.row_factory = sqlite3.Row
        for row in database.execute(
            "SELECT model, updated_at, tokens_used "
            "FROM threads WHERE tokens_used > 0"
        ):
            yield usage_event(row["model"], row["updated_at"], total=row["tokens_used"])
    except sqlite3.Error as error:
        print(f"warning: could not read Codex database: {error}", file=sys.stderr)
    finally:
        if database is not None:
            database.close()


def scan_claude():
    if not CLAUDE_PROJECTS.exists():
        return

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
                    yield usage_event(
                        model,
                        record.get("timestamp"),
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cache_read=usage.get("cache_read_input_tokens"),
                        cache_write=usage.get("cache_creation_input_tokens"),
                    )
        except OSError:
            continue


def event_cost(event: dict) -> float | None:
    model = event["model"]
    if model in CLAUDE_RATES:
        return sum(
            event[field] * rate
            for field, rate in zip(TOKEN_FIELDS, CLAUDE_RATES[model])
        ) / 1_000_000
    if model in OPENAI_RATES:
        return event["tokens"] * OPENAI_RATES[model] / 1_000_000
    return None


def period_start(period: str) -> datetime | None:
    if period == "all":
        return None
    now = datetime.now(LOCAL_TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        return start - timedelta(days=start.weekday())
    if period == "monthly":
        return start.replace(day=1)
    return start


def collect(period: str) -> tuple[list[dict], datetime | None, datetime | None]:
    since = period_start(period)
    models = {}
    first = last = None
    for event in chain(scan_codex(), scan_claude()):
        timestamp = event["timestamp"]
        if since and timestamp < since:
            continue
        first = timestamp if first is None or timestamp < first else first
        last = timestamp if last is None or timestamp > last else last
        row = models.setdefault(
            event["model"],
            {
                "model": event["model"],
                "tokens": 0,
                **dict.fromkeys(TOKEN_FIELDS, 0),
                "events": 0,
                "cost": 0.0,
                "cost_known": True,
            },
        )
        for field in ("tokens", *TOKEN_FIELDS):
            row[field] += event[field]
        row["events"] += 1
        cost = event_cost(event)
        if cost is None:
            row["cost_known"] = False
        else:
            row["cost"] += cost

    ordered = sorted(models.values(), key=lambda row: row["tokens"], reverse=True)
    return ordered, first, last


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


def print_models(breakdown: bool, period: str) -> None:
    models, first, last = collect(period)
    print(f"tokscale · Models · {period}")
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
    models.add_argument(
        "period",
        nargs="?",
        choices=("daily", "weekly", "monthly", "all"),
        default="all",
        help="current period to include (default: all)",
    )
    args = parser.parse_args()
    print_models(args.breakdown, args.period)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
