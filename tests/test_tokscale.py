import io
import json
import tempfile
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

import tokscale


class TokscaleTests(unittest.TestCase):
    def test_provider_and_model_labels(self) -> None:
        for provider, model, expected in (
            ("codex", "GPT-5.6-Sol", "gpt-5.6-sol"),
            ("openrouter", "moonshotai/kimi-k2.6", "or-moonshotai-kimi-k2.6"),
            ("antigravity", "Gemini 3.7 Flash (High)", "agy-gemini-3.7-flash-(high)"),
            ("google", "Gemini Pro", "google-gemini-pro"),
        ):
            with self.subTest(provider=provider, model=model):
                self.assertEqual(tokscale.display_label(provider, model), expected)

    def test_growth_and_resets(self) -> None:
        self.assertEqual(
            [
                tokscale.counter_delta(*values)
                for values in ((150, 100), (25, 100), (100, 100), (-1, 100))
            ],
            [50, 25, 0, 0],
        )

    def test_models_table_ends_with_total(self) -> None:
        models = [
            {"label": "known", "tokens": 1_500_000, "cost": 2.5, "cost_known": True},
            {"label": "unknown", "tokens": 500_000, "cost": 0.0, "cost_known": False},
        ]
        output = io.StringIO()
        with patch.object(tokscale, "collect", return_value=(models, None, None)):
            with patch("sys.stdout", output):
                tokscale.print_models(False, "all")

        self.assertEqual(output.getvalue().splitlines()[-1], "total           2.0M        $2.50")


class DeepBreakdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {
            "CODEX_DIRS": (self.root / "codex",),
            "CODEX_DB": self.root / "missing.sqlite",
            "CLAUDE_PROJECTS": self.root / "claude",
            "PI_SESSIONS": self.root / "pi",
            "ANTIGRAVITY_USAGE_LOG": self.root / "agy.jsonl",
        }.items():
            patcher = patch.object(tokscale, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, name, records):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n{unfinished")

    def record(self, kind, payload, timestamp="2026-09-10T10:00:00Z"):
        return {"type": kind, "payload": payload, "timestamp": timestamp}

    def count(self, total, timestamp="2026-09-10T10:00:00Z"):
        return self.record("event_msg", {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total - 10, "output_tokens": 10,
                                  "cached_input_tokens": 20, "total_tokens": total},
            "last_token_usage": {"total_tokens": total}}}, timestamp)

    def test_cumulative_usage_duplicates_resets_and_period_boundary(self):
        self.write("codex/a.jsonl", [
            self.record("session_meta", {"id": "a", "timestamp": "2026-09-09T08:00:00Z"}),
            self.record("turn_context", {"model": "test-model"}),
            self.count(100, "2026-09-09T10:00:00Z"), self.count(150), self.count(150), self.count(40),
        ])
        with patch.object(tokscale, "period_start", return_value=tokscale.local_time("2026-09-10T00:00:00Z")):
            row, = tokscale.scan_deep_sessions("daily")
        self.assertEqual(row["tokens"], 90)
        self.assertEqual(row["events"], 2)
        self.assertEqual(row["started"].day, 9)

    def test_modern_usage_and_tool_deduplication(self):
        usage = {"input_tokens": 80, "output_tokens": 20, "cached_input_tokens": 40, "total_tokens": 100}
        modern = self.record("token_usage_record", {"response_id": "r1", "usage": usage,
                                                   "thread_token_usage": usage})
        call = self.record("response_item", {"type": "custom_tool_call", "call_id": "c1",
                            "name": "exec", "input": "await tools.mcp__drive__search({})"})
        self.write("codex/a.jsonl", [self.record("session_meta", {"id": "a"}),
            self.record("turn_context", {"model": "test-model"}), call, call, modern, modern, self.count(100),
            self.record("response_item", {"type": "function_call", "call_id": "c2", "name": "mcp__web__search"})])
        self.write("session_index.jsonl", [{"id": "a", "thread_name": "Find reports"}])
        row, = tokscale.scan_deep_sessions("all")
        self.assertEqual(row["tokens"], 100)
        self.assertEqual(row["cache_read_tokens"], 40)
        self.assertEqual(row["title"], "Find reports")
        self.assertEqual(row["tools"], {"exec": 1, "mcp__web__search": 1})
        self.assertEqual(row["mcp_refs"], {"mcp__drive__search": 1})

    def test_sessions_without_model_do_not_inherit_previous_model(self):
        self.write("codex/a.jsonl", [self.record("turn_context", {"model": "first"}), self.count(100)])
        self.write("codex/b.jsonl", [self.count(50)])
        rows = list(tokscale.scan_deep_sessions("all"))
        self.assertEqual(rows[1]["models"], {"unknown"})
        self.assertEqual(sum(row["tokens"] for row in rows), 150)

    def test_mcp_references_skip_strings_and_comments(self):
        record = self.record("response_item", {"type": "custom_tool_call", "call_id": "c1",
            "name": "exec", "input": '''
            const sample = "tools.mcp__fake__search({})";
            // tools.mcp__comment__search({})
            /* tools.mcp__comment__search({}) */
            await tools.mcp__real__search({});
            '''})
        self.assertEqual(list(tokscale.tool_calls(record, "codex")), [
            ("c1", "exec", False), ("c1:0", "mcp__real__search", True)])

    def test_claude_and_pi_sessions(self):
        self.write("claude/a.jsonl", [
            {"type": "user", "sessionId": "a", "timestamp": "2026-09-10T08:00:00Z",
             "message": {"content": "Fix tests"}},
            {"type": "assistant", "sessionId": "a", "timestamp": "2026-09-10T09:00:00Z",
             "message": {"id": "m1", "model": "claude-test", "usage": {"input_tokens": 10,
                         "output_tokens": 5, "cache_read_input_tokens": 30},
                         "content": [{"type": "tool_use", "id": "t1", "name": "Read"}]}}])
        self.write("pi/b.jsonl", [
            {"type": "session", "id": "b", "timestamp": "2026-09-10T07:00:00Z"},
            {"type": "session_info", "name": "Pi task"},
            {"type": "message", "id": "m2", "timestamp": "2026-09-10T09:00:00Z",
             "message": {"role": "assistant", "model": "pi-test", "usage": {"input": 10,
                         "output": 5, "cacheRead": 20, "totalTokens": 35},
                         "content": [{"type": "toolCall", "id": "t2", "name": "bash"}]}}])
        rows = list(tokscale.scan_deep_sessions("all"))
        self.assertEqual([(r["title"], r["tokens"]) for r in rows], [("Fix tests", 45), ("Pi task", 35)])
        self.assertEqual(rows[0]["tools"], {"Read": 1})
        self.assertEqual(rows[1]["tools"], {"bash": 1})

    def test_database_display_title_and_start(self):
        with sqlite3.connect(tokscale.CODEX_DB) as db:
            db.execute("CREATE TABLE threads (id TEXT, title TEXT, name TEXT, created_at INTEGER)")
            db.execute("INSERT INTO threads VALUES ('a', 'Original prompt', 'Display title', 1789000000)")
        self.write("codex/a.jsonl", [self.record("session_meta", {"id": "a"}), self.count(100)])
        row, = tokscale.scan_deep_sessions("all")
        self.assertEqual(row["title"], "Display title")
        output = io.StringIO()
        with patch("sys.stdout", output):
            tokscale.print_deep_bd("all", "start", "DISPLAY", "", False,
                                  started_after=tokscale.local_time("2027-01-01T00:00:00Z"))
        self.assertIn("0 sessions", output.getvalue())

    def test_antigravity_counter_deltas(self):
        self.write("agy.jsonl", [
            {"captured_at": "2026-09-10T09:00:00Z", "payload": {
                "session_id": "agy-session", "model": {"id": "gemini-test"},
                "context_window": {"total_input_tokens": value, "total_output_tokens": 10}}}
            for value in (100, 100, 150)])
        row, = tokscale.scan_deep_sessions("all")
        self.assertEqual(row["tokens"], 160)
        self.assertEqual(row["events"], 2)
        self.assertEqual(row["tools"], {})

    def test_tool_and_mcp_filters(self):
        self.write("codex/a.jsonl", [self.record("session_meta", {"id": "a"}), self.count(100),
            self.record("response_item", {"type": "function_call", "call_id": "c1", "name": "mcp__drive__search"})])
        self.write("codex/b.jsonl", [self.record("session_meta", {"id": "b"}), self.count(200)])
        output = io.StringIO()
        with patch("sys.stdout", output):
            tokscale.print_deep_bd("all", "mcp", "", "DRIVE", True)
        self.assertIn("1 sessions | 100 tokens", output.getvalue())
        self.assertIn("MCP calls 1", output.getvalue())

    def test_cli_shorthand_filters_and_empty_results(self):
        output = io.StringIO()
        with patch("sys.argv", ["tokscale", "--deep-bd", "--title", "missing", "--sort", "start"]), patch("sys.stdout", output):
            tokscale.main()
        self.assertIn("0 sessions | 0 tokens", output.getvalue())


if __name__ == "__main__":
    unittest.main()
