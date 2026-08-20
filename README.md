# tokscale

Local Codex, Claude, Pi/OpenRouter, and Antigravity token reports in Python,
Go, and Rust. All three read the local CLI records directly and use the same
compact provider/model labels.

## Python

No build or dependencies:

```bash
python3 tokscale.py models --breakdown --daily
```

Pi responses are read from `~/.pi/agent/sessions` and use the exact usage and
cost fields recorded by Pi. Provider IDs are retained as recorded, including
providers other than OpenRouter.

Antigravity's transcript and SQLite files do not contain historical token
usage. To opt in to exact per-session counters, configure the included
status-line logger:

```bash
chmod +x storage/antigravity_statusline.py
```

Add this block to `~/.gemini/antigravity-cli/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /absolute/path/to/tokentracker/storage/antigravity_statusline.py"
  }
}
```

After Antigravity emits status-line updates, Tokscale reads the resulting
`~/.gemini/antigravity-cli/tokscale-usage.jsonl` file. Existing Antigravity
sessions cannot be reconstructed exactly without those counters.

## Go

One source file, standard library only:

```bash
go build -trimpath -ldflags="-s -w" -o tokscale-go ./go/tokscale.go
./tokscale-go models --breakdown --daily
```

## Rust

Reproducible Cargo build using the committed lockfile:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
./rust/target/release/tokscale-rust models --breakdown --daily
```

Replace `--daily` with `--weekly`, `--monthly`, or `--all`.
Use `--daily3` for a compact daily report containing only the top three models.

On the development Mac with warm filesystem caches, ten real daily scans
averaged roughly 0.34 seconds each for Python, 0.14 seconds for Go, and 0.05
seconds for Rust. Results vary with log size and storage speed.

The tools use per-turn Codex rollout usage, with its SQLite database as a
fallback. Claude pricing includes input, output, cache-read, and five-minute
cache-write rates. Codex cached input is displayed separately but is already
included in input and is never counted twice. Pi/OpenRouter costs come from
Pi's recorded response cost. Antigravity costs use an API-equivalent Gemini
pricing proxy; they are estimates, not charges on the Antigravity subscription.
The current Gemini 3.6/3.7 Flash standard proxy is $0.75 per million input
tokens and $3.75 per million output tokens through 2026, matching Google's
published [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing).

## Account scope

Reports cover the current operating-system user's `~/.codex`, `~/.claude`, and
`~/.pi` folders, plus the opt-in Antigravity status-line log. Other macOS user
accounts are not included. If several ChatGPT accounts are used from the same
macOS user, their retained Codex rollout files are combined because historical
rollout events do not contain an account ID.

## PostgreSQL history

Postgres.app stores one row per date, provider, and model. Running the snapshot
again updates existing rows instead of duplicating them and backfills every
date still present in the local logs. Stored history never decreases if an old
source log is later truncated or removed:

```bash
./storage/run_snapshot.sh
```

Connect with pgAdmin using host `127.0.0.1`, port `55432`, database `tokscale`,
and your macOS username. Local connections do not require a password. Useful
queries:

```sql
SELECT * FROM daily_usage_summary;
SELECT * FROM daily_model_usage ORDER BY usage_date DESC, total_tokens DESC;
```

On this Mac, a local Codex automation runs the snapshot every day at 09:00 in
the local timezone. The usage table is about 48 KB after the initial backfill;
the PostgreSQL database is about 8 MB before normal PostgreSQL cluster overhead.
