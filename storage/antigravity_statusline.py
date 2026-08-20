#!/usr/bin/env python3
"""Capture Antigravity status-line token totals for Tokscale.

Configure this as Antigravity's statusLine command. The CLI sends a JSON
payload on stdin whenever agent state changes; this script stores only the
conversation, model, context counters, and capture time (not account/quota
metadata) in ~/.gemini/antigravity-cli/tokscale-usage.jsonl.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


LOG = Path.home() / ".gemini" / "antigravity-cli" / "tokscale-usage.jsonl"


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return

    if not isinstance(payload, dict):
        return

    model = payload.get("model") or {}
    context = payload.get("context_window") or {}
    if not isinstance(model, dict) or not isinstance(context, dict):
        return
    record = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "conversation_id": payload.get("conversation_id"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "model": {
            "id": model.get("id"),
            "display_name": model.get("display_name"),
        },
        "context_window": {
            "total_input_tokens": context.get("total_input_tokens", 0),
            "total_output_tokens": context.get("total_output_tokens", 0),
        },
    }
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError:
        pass

    used = context.get("used_percentage")
    model_name = model.get("display_name") or model.get("id") or "antigravity"
    if isinstance(used, (int, float)):
        print(f"{model_name} · context {used:.1f}%")
    else:
        print(model_name)


if __name__ == "__main__":
    main()
