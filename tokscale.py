#!/usr/bin/env python3
"""Local token usage collector and terminal dashboard backend.

No third-party packages are required. It reads local Codex/Claude records,
optionally accepts JSONL hook events, and emits usage-data.js for index.html.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
HOME = Path.home()
CODEX_DB = HOME / ".codex" / "state_5.sqlite"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_ARCHIVED_SESSIONS = HOME / ".codex" / "archived_sessions"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
EVENT_LOG = HOME / ".tokscale" / "events.jsonl"
DEFAULT_OUTPUT = ROOT / "usage-data.js"
LOCAL_TZ = ZoneInfo(os.environ.get("TOKSCALE_TZ", "Europe/Oslo"))


# USD per million tokens. Claude values are Anthropic's standard global API
# prices; cache writes use the 5-minute TTL because local Claude logs omit TTL.
CLAUDE_RATES = {
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cache_read": 1.0, "cache_write": 12.5},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
}

# These are the existing API-equivalent rates configured for the local Codex
# view. Codex's local thread database exposes one cumulative counter, not an
# input/output/cache breakdown.
OPENAI_FLAT_RATES = {
    "gpt-5.6-sol": 0.8,
    "codex-auto-review": 0.459224,
    "gpt-5.6-luna": 0.0177545,
}


def number(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def parse_time(value) -> datetime:
    if isinstance(value, (int, float)):
        # Unix milliseconds are common in Claude metadata; seconds are used by
        # the Codex SQLite database.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(LOCAL_TZ)
    text = str(value or "")
    if not text:
        return datetime.now(LOCAL_TZ)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(LOCAL_TZ)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def event_id(event: dict) -> str:
    stable = json.dumps(event, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()[:16]


def make_event(*, provider, model, timestamp, source, input_tokens=0,
               output_tokens=0, cache_read=0, cache_write=0, total=None,
               note=None) -> dict:
    input_tokens = number(input_tokens)
    output_tokens = number(output_tokens)
    cache_read = number(cache_read)
    cache_write = number(cache_write)
    total = number(total) or input_tokens + output_tokens + cache_read + cache_write
    when = parse_time(timestamp)
    event = {
        "id": event_id({"provider": provider, "model": model, "timestamp": when.isoformat(), "source": source, "total": total}),
        "provider": provider,
        "model": model or "unknown",
        "timestamp": when.isoformat(),
        "date": when.date().isoformat(),
        "source": source,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "tokens": total,
    }
    if note:
        event["note"] = note
    return event


def scan_codex_rollouts() -> list[dict]:
    """Read per-turn Codex usage from local rollout JSONL files.

    Codex's cached input count is a subset of input_tokens, so it is recorded
    for the breakdown but is not added again to the event total.
    """
    events = []
    bases = [base for base in (CODEX_SESSIONS, CODEX_ARCHIVED_SESSIONS) if base.exists()]
    for base in bases:
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
                        if not usage or not model:
                            continue
                        events.append(make_event(
                            provider="ChatGPT / Codex",
                            model=model,
                            timestamp=record.get("timestamp"),
                            source="codex-rollout",
                            input_tokens=usage.get("input_tokens"),
                            output_tokens=usage.get("output_tokens"),
                            cache_read=usage.get("cached_input_tokens"),
                            cache_write=usage.get("cache_write_input_tokens"),
                            total=usage.get("total_tokens"),
                            note="Codex per-turn response usage",
                        ))
            except OSError:
                continue
    return events


def scan_codex() -> list[dict]:
    rollout_events = scan_codex_rollouts()
    if rollout_events:
        return rollout_events
    if not CODEX_DB.exists():
        return []
    events = []
    try:
        db = sqlite3.connect(f"file:{CODEX_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT model, model_provider, updated_at, tokens_used "
            "FROM threads WHERE tokens_used > 0"
        ).fetchall()
    except sqlite3.Error as exc:
        print(f"warning: could not read Codex database: {exc}", file=sys.stderr)
        return []
    finally:
        try:
            db.close()
        except UnboundLocalError:
            pass
    for row in rows:
        events.append(make_event(
            provider="ChatGPT / Codex",
            model=row["model"] or "unknown",
            timestamp=row["updated_at"],
            source="codex-local",
            total=row["tokens_used"],
            note="Fallback: Codex thread total assigned to its last-updated date",
        ))
    return events


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
                    if not usage:
                        continue
                    model = message.get("model")
                    if not model or model == "<synthetic>":
                        continue
                    events.append(make_event(
                        provider="Claude",
                        model=model,
                        timestamp=record.get("timestamp"),
                        source="claude-local",
                        input_tokens=usage.get("input_tokens"),
                        output_tokens=usage.get("output_tokens"),
                        cache_read=usage.get("cache_read_input_tokens"),
                        cache_write=usage.get("cache_creation_input_tokens"),
                    ))
        except OSError:
            continue
    return events


def scan_hooks() -> list[dict]:
    if not EVENT_LOG.exists():
        return []
    events = []
    try:
        with EVENT_LOG.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not raw.get("model"):
                    continue
                events.append(make_event(
                    provider=raw.get("provider", "Hook"),
                    model=raw.get("model"),
                    timestamp=raw.get("timestamp") or raw.get("created_at"),
                    source="hook",
                    input_tokens=raw.get("input_tokens"),
                    output_tokens=raw.get("output_tokens"),
                    cache_read=raw.get("cache_read_input_tokens", raw.get("cache_read_tokens")),
                    cache_write=raw.get("cache_creation_input_tokens", raw.get("cache_write_tokens")),
                    total=raw.get("total_tokens"),
                ))
    except OSError:
        pass
    return events


def pricing(model: str) -> dict | None:
    if model in CLAUDE_RATES:
        rates = CLAUDE_RATES[model]
        return {
            "kind": "claude",
            "input": rates["input"],
            "output": rates["output"],
            "cache_read": rates["cache_read"],
            "cache_write": rates["cache_write"],
            "label": f"${rates['input']:g} in · ${rates['output']:g} out",
        }
    if model in OPENAI_FLAT_RATES:
        rate = OPENAI_FLAT_RATES[model]
        return {"kind": "flat", "rate": rate, "label": f"${rate:g} / 1M"}
    return None


def event_cost(event: dict) -> float | None:
    rate = pricing(event["model"])
    if not rate:
        return None
    if rate["kind"] == "claude":
        return (
            event["input_tokens"] * rate["input"]
            + event["output_tokens"] * rate["output"]
            + event["cache_read_tokens"] * rate["cache_read"]
            + event["cache_write_tokens"] * rate["cache_write"]
        ) / 1_000_000
    return event["tokens"] / 1_000_000 * rate["rate"]


def bucket_for(dt: datetime, period: str) -> str:
    if period == "hourly":
        return dt.strftime("%Y-%m-%d %H:00")
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    if period == "weekly":
        iso = dt.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return dt.strftime("%Y-%m")


def aggregate(events: list[dict], period: str) -> list[dict]:
    buckets = {}
    for event in events:
        dt = parse_time(event["timestamp"])
        key = bucket_for(dt, period)
        bucket = buckets.setdefault(key, {
            "period": key, "tokens": 0, "cost": 0.0, "cost_known": True,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "events": 0, "models": {}, "providers": {},
        })
        bucket["tokens"] += event["tokens"]
        bucket["input_tokens"] += event["input_tokens"]
        bucket["output_tokens"] += event["output_tokens"]
        bucket["cache_read_tokens"] += event["cache_read_tokens"]
        bucket["cache_write_tokens"] += event["cache_write_tokens"]
        bucket["events"] += 1
        cost = event_cost(event)
        if cost is None:
            bucket["cost_known"] = False
        else:
            bucket["cost"] += cost
        model = bucket["models"].setdefault(event["model"], {"tokens": 0, "cost": 0.0})
        model["tokens"] += event["tokens"]
        if cost is not None:
            model["cost"] += cost
        provider = bucket["providers"].setdefault(event["provider"], {"tokens": 0, "cost": 0.0})
        provider["tokens"] += event["tokens"]
        if cost is not None:
            provider["cost"] += cost
    return [buckets[key] for key in sorted(buckets)]


def build_payload() -> dict:
    events = scan_codex() + scan_claude() + scan_hooks()
    events.sort(key=lambda item: item["timestamp"])
    models = {}
    providers = {}
    for event in events:
        cost = event_cost(event)
        model = models.setdefault(event["model"], {
            "model": event["model"], "provider": event["provider"], "tokens": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_write_tokens": 0, "cost": 0.0, "cost_known": True,
            "events": 0, "pricing": pricing(event["model"]),
        })
        model["tokens"] += event["tokens"]
        model["input_tokens"] += event["input_tokens"]
        model["output_tokens"] += event["output_tokens"]
        model["cache_read_tokens"] += event["cache_read_tokens"]
        model["cache_write_tokens"] += event["cache_write_tokens"]
        model["events"] += 1
        if cost is None:
            model["cost_known"] = False
        else:
            model["cost"] += cost
        provider = providers.setdefault(event["provider"], {"tokens": 0, "cost": 0.0, "cost_known": True})
        provider["tokens"] += event["tokens"]
        if cost is None:
            provider["cost_known"] = False
        else:
            provider["cost"] += cost

    periods = {unit: aggregate(events, unit) for unit in ("hourly", "daily", "weekly", "monthly")}
    known_cost = sum(event_cost(event) or 0 for event in events)
    dates = [event["timestamp"] for event in events]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "timezone": str(LOCAL_TZ),
        "period_start": dates[0] if dates else None,
        "period_end": dates[-1] if dates else None,
        "event_count": len(events),
        "totals": {
            "tokens": sum(event["tokens"] for event in events),
            "cost": known_cost,
            "cost_known": all(event_cost(event) is not None for event in events) if events else False,
            "models": len(models),
            "providers": len(providers),
        },
        "models": sorted(models.values(), key=lambda item: item["tokens"], reverse=True),
        "providers": providers,
        "periods": periods,
        "notes": [
            "Claude costs use Anthropic standard global API pricing.",
            "Claude cache writes use the 5-minute rate because local logs omit TTL.",
            "Codex uses per-turn rollout usage when available; SQLite thread totals are a fallback.",
        ],
    }


def write_payload(output: Path) -> dict:
    payload = build_payload()
    output.write_text(
        "// Generated by tokscale.py — do not edit manually.\n"
        "window.TOKSCALE_DATA = "
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        + ";\n",
        encoding="utf-8",
    )
    return payload


def format_tokens(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def format_cost(value: float, known: bool = True) -> str:
    if not known:
        return "—"
    if value >= 1000:
        return f"${value / 1000:.1f}K"
    if 0 < value < 0.01:
        return f"${value:.3f}"
    return f"${value:.2f}"


def short_interval(start: str | None, end: str | None) -> str:
    if not start or not end:
        return "No data"
    first = parse_time(start)
    last = parse_time(end)
    first_label = f"{first.strftime('%b')} {first.day}"
    last_label = f"{last.strftime('%b')} {last.day}"
    return first_label if first.date() == last.date() else f"{first_label} – {last_label}"


def report(payload: dict, period: str, limit: int) -> None:
    rows = payload["periods"][period][-limit:]
    print(f"tokscale · {period} · {payload['timezone']}")
    print(f"range: {short_interval(payload.get('period_start'), payload.get('period_end'))}")
    print()
    print(f"{'period':<18} {'tokens':>12} {'cost':>12} {'events':>8}")
    print("-" * 54)
    for row in rows:
        cost = format_cost(row["cost"], row["cost_known"])
        print(f"{row['period']:<18} {format_tokens(row['tokens']):>12} {cost:>12} {row['events']:>8}")


def model_report(payload: dict, breakdown: bool) -> None:
    print("tokscale · Models")
    print(f"range: {short_interval(payload.get('period_start'), payload.get('period_end'))}")
    print()
    print(f"{'Model':<30} {'Tokens':>12} {'Cost':>12}")
    print("-" * 58)
    for model in payload["models"]:
        print(f"{model['model']:<30} {format_tokens(model['tokens']):>12} {format_cost(model['cost'], model['cost_known']):>12}")
    if breakdown:
        print()
        print("Breakdown")
        print("---------")
        print(f"{'Model':<24} {'Input':>10} {'Output':>10} {'Cache read':>12} {'Cache write':>13} {'Events':>8}")
        print("-" * 83)
        for model in payload["models"]:
            print(f"{model['model']:<24} {format_tokens(model['input_tokens']):>10} {format_tokens(model['output_tokens']):>10} {format_tokens(model['cache_read_tokens']):>12} {format_tokens(model['cache_write_tokens']):>13} {model['events']:>8}")
        print()
        print("Note: Codex cache read is included inside Input and is shown separately, not added again.")


def ingest(stdin) -> int:
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with EVENT_LOG.open("a", encoding="utf-8") as output:
        for line in stdin:
            if not line.strip():
                continue
            raw = json.loads(line)
            if not raw.get("model"):
                raise ValueError("each hook event needs a model")
            raw.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
            output.write(json.dumps(raw, separators=(",", ":")) + "\n")
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(prog="tokscale", description="Local token usage dashboard and terminal reporter")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh = sub.add_parser("refresh", help="scan local records and generate usage-data.js")
    refresh.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)

    report_parser = sub.add_parser("report", help="print a period summary in the terminal")
    report_parser.add_argument("--period", choices=("hourly", "daily", "weekly", "monthly"), default="daily")
    report_parser.add_argument("--limit", type=int, default=31)

    models_parser = sub.add_parser("models", help="print the compact Model / Tokens / Cost report")
    models_parser.add_argument("--breakdown", action="store_true", help="include input/output/cache details")

    serve = sub.add_parser("serve", help="refresh data and serve the dashboard locally")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    sub.add_parser("ingest", help="append JSONL usage events from stdin")

    args = parser.parse_args()
    if args.command == "ingest":
        count = ingest(sys.stdin)
        print(f"recorded {count} event(s) in {EVENT_LOG}")
        return 0
    if args.command == "refresh":
        payload = write_payload(args.output)
        print(f"wrote {args.output}")
        print(f"scanned {payload['event_count']} events across {payload['totals']['models']} models")
        return 0
    if args.command == "report":
        report(build_payload(), args.period, args.limit)
        return 0
    if args.command == "models":
        model_report(build_payload(), args.breakdown)
        return 0
    if args.command == "serve":
        write_payload(DEFAULT_OUTPUT)
        url = f"http://{args.host}:{args.port}/index.html"
        print(f"tokscale running at {url}")
        print("Press Ctrl-C to stop.")
        return subprocess.call([sys.executable, "-m", "http.server", str(args.port), "--bind", args.host], cwd=ROOT)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        raise SystemExit(0)
