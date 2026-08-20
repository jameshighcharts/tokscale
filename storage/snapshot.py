#!/usr/bin/env python3
"""Upsert retained local token usage into the tokscale PostgreSQL database."""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from itertools import chain
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

import tokscale  # noqa: E402


PSQL = os.environ.get(
    "TOKSCALE_PSQL",
    "/Applications/Postgres.app/Contents/Versions/18/bin/psql",
)
DATABASE = os.environ.get("TOKSCALE_DATABASE", "tokscale")
TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tokens",
)


def aggregate_days() -> list[dict]:
    rows: dict[tuple[str, str, str], dict] = {}
    for event in chain(
        tokscale.scan_codex(),
        tokscale.scan_claude(),
        tokscale.scan_pi(),
        tokscale.scan_antigravity(),
    ):
        provider = event["provider"]
        key = (event["timestamp"].date().isoformat(), provider, event["model"])
        row = rows.setdefault(
            key,
            {
                "usage_date": key[0],
                "provider": provider,
                "model": event["model"],
                **dict.fromkeys(TOKEN_FIELDS, 0),
                "event_count": 0,
                "estimated_cost_usd": 0.0,
                "cost_known": True,
            },
        )
        for field in TOKEN_FIELDS:
            row[field] += event[field]
        row["event_count"] += 1
        cost = tokscale.event_cost(event)
        if cost is None:
            row["cost_known"] = False
        else:
            row["estimated_cost_usd"] += cost
    return [rows[key] for key in sorted(rows)]


def copy_script(rows: list[dict]) -> str:
    output = io.StringIO()
    output.write(
        """BEGIN;
CREATE TEMP TABLE incoming_usage (LIKE daily_model_usage INCLUDING DEFAULTS) ON COMMIT DROP;
COPY incoming_usage (
    usage_date, provider, model, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, total_tokens, event_count,
    estimated_cost_usd
) FROM STDIN WITH (FORMAT csv, NULL '');
"""
    )
    writer = csv.writer(output, lineterminator="\n")
    for row in rows:
        writer.writerow(
            (
                row["usage_date"],
                row["provider"],
                row["model"],
                row["input_tokens"],
                row["output_tokens"],
                row["cache_read_tokens"],
                row["cache_write_tokens"],
                row["tokens"],
                row["event_count"],
                f"{row['estimated_cost_usd']:.6f}" if row["cost_known"] else "",
            )
        )
    output.write(
        """\.
INSERT INTO daily_model_usage AS stored (
    usage_date, provider, model, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, total_tokens, event_count,
    estimated_cost_usd
)
SELECT
    usage_date, provider, model, input_tokens, output_tokens,
    cache_read_tokens, cache_write_tokens, total_tokens, event_count,
    estimated_cost_usd
FROM incoming_usage
ON CONFLICT (usage_date, provider, model) DO UPDATE SET
    input_tokens = EXCLUDED.input_tokens,
    output_tokens = EXCLUDED.output_tokens,
    cache_read_tokens = EXCLUDED.cache_read_tokens,
    cache_write_tokens = EXCLUDED.cache_write_tokens,
    total_tokens = EXCLUDED.total_tokens,
    event_count = EXCLUDED.event_count,
    estimated_cost_usd = EXCLUDED.estimated_cost_usd,
    updated_at = now()
WHERE EXCLUDED.total_tokens >= stored.total_tokens;
COMMIT;
"""
    )
    return output.getvalue()


def main() -> None:
    rows = aggregate_days()
    if not rows:
        print("tokscale storage: no local usage records found")
        return
    subprocess.run(
        [PSQL, "-X", "-v", "ON_ERROR_STOP=1", "-d", DATABASE],
        input=copy_script(rows),
        text=True,
        check=True,
    )
    dates = [row["usage_date"] for row in rows]
    print(
        f"tokscale storage: upserted {len(rows)} model-day rows "
        f"for {dates[0]} through {dates[-1]}"
    )


if __name__ == "__main__":
    main()
