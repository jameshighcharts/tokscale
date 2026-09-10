import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CLIParityTests(unittest.TestCase):
    def test_deep_report_across_python_go_and_rust(self):
        binaries = [ROOT / "tokscale-go", ROOT / "rust/target/release/tokscale-rust"]
        if not all(path.exists() for path in binaries):
            self.skipTest("build Go and Rust release binaries to check CLI parity")
        commands = [[sys.executable, str(ROOT / "tokscale.py")]]
        commands += [[str(path)] for path in binaries]
        with tempfile.TemporaryDirectory() as home:
            codex = Path(home) / ".codex"
            sessions = codex / "sessions"
            sessions.mkdir(parents=True)
            codex.joinpath("session_index.jsonl").write_text(json.dumps({
                "id": "session-a", "thread_name": "Parity task",
            }) + "\n")
            usage = {"input_tokens": 80, "output_tokens": 20,
                     "cached_input_tokens": 40, "total_tokens": 100}
            records = [
                ("session_meta", {"id": "session-a"}),
                ("turn_context", {"model": "test-model"}),
                ("response_item", {"type": "function_call", "call_id": "call-a",
                                   "name": "mcp__drive__search"}),
                ("token_usage_record", {"response_id": "response-a", "usage": usage}),
                ("event_msg", {"type": "token_count", "info": {"last_token_usage": usage}}),
            ]
            sessions.joinpath("session.jsonl").write_text("\n".join(json.dumps({
                "type": kind, "payload": payload, "timestamp": "2026-09-10T10:00:00Z",
            }) for kind, payload in records))
            env = {**os.environ, "HOME": home, "TOKSCALE_TZ": "Europe/Oslo"}
            for arguments in (
                ["--deep-bd", "--all"],
                ["models", "--deep-bd", "--title", "PARITY", "--sort", "start"],
                ["--deep-bd", "--mcp", "--tool", "DRIVE", "--sort", "mcp", "--limit", "1"],
                ["--deep-bd", "--started-after", "2027-01-01"],
                ["--deep-bd", "--limit", "0"],
                ["--deep-bd", "--sort", "invalid"],
            ):
                with self.subTest(arguments=arguments):
                    results = [subprocess.run(command + arguments, env=env, cwd=home,
                               capture_output=True, text=True, timeout=20) for command in commands]
                    self.assertEqual([r.returncode for r in results], [results[0].returncode] * 3)
                    self.assertEqual([r.stdout for r in results], [results[0].stdout] * 3)
                    if arguments == ["--deep-bd", "--all"]:
                        self.assertIn("Parity task", results[0].stdout)
                        self.assertIn("1 sessions | 100 tokens", results[0].stdout)
                        self.assertIn("MCP calls 1", results[0].stdout)
                    if "0" in arguments or "invalid" in arguments:
                        self.assertEqual(results[0].returncode, 2)


if __name__ == "__main__":
    unittest.main()
