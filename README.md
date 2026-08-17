# tokscale

Local Codex and Claude token reports in Python, Go, and Rust. All three produce
the same output and read `~/.codex` plus `~/.claude` directly.

## Python

No build or dependencies:

```bash
python3 tokscale.py models --breakdown -- daily
```

## Go

One source file, standard library only:

```bash
go build -trimpath -ldflags="-s -w" -o tokscale-go ./go/tokscale.go
./tokscale-go models --breakdown -- daily
```

## Rust

Reproducible Cargo build using the committed lockfile:

```bash
cargo build --release --locked --manifest-path rust/Cargo.toml
./rust/target/release/tokscale-rust models --breakdown -- daily
```

Replace `daily` with `weekly`, `monthly`, or `all`. The `--` ends options so
the final word is treated as the period.

On the development Mac with warm filesystem caches, ten real daily scans
averaged roughly 0.34 seconds each for Python, 0.14 seconds for Go, and 0.05
seconds for Rust. Results vary with log size and storage speed.

The tools use per-turn Codex rollout usage, with its SQLite database as a
fallback. Claude pricing includes input, output, cache-read, and five-minute
cache-write rates. Codex cached input is displayed separately but is already
included in input and is never counted twice.
