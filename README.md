# tokscale

Local Codex and Claude token reports in Python, Go, and Rust. All three produce
the same output and read `~/.codex` plus `~/.claude` directly.

## Python

No build or dependencies:

```bash
python3 tokscale.py models --breakdown --daily
```

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

On the development Mac with warm filesystem caches, ten real daily scans
averaged roughly 0.34 seconds each for Python, 0.14 seconds for Go, and 0.05
seconds for Rust. Results vary with log size and storage speed.

The tools use per-turn Codex rollout usage, with its SQLite database as a
fallback. Claude pricing includes input, output, cache-read, and five-minute
cache-write rates. Codex cached input is displayed separately but is already
included in input and is never counted twice.

## Account scope

Reports cover the current operating-system user's `~/.codex` and `~/.claude`
folders. Other macOS user accounts are not included. If several ChatGPT
accounts are used from the same macOS user, their retained Codex rollout files
are combined because historical rollout events do not contain an account ID.

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
