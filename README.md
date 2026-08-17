# tokscale

Local-first token usage dashboard for Codex, Claude Code, and JSONL hook events.

## Run it

From this directory:

```bash
python3 tokscale.py refresh
python3 tokscale.py serve
```

Open [http://127.0.0.1:8765/index.html](http://127.0.0.1:8765/index.html).

The dashboard has Overview, Usage, Models, Daily, Weekly, Monthly, and Hourly views. `refresh` reads:

- Codex: `~/.codex/sessions/**/*.jsonl` (per-turn usage; SQLite is the fallback)
- Claude Code: `~/.claude/projects/**/*.jsonl`
- Hook events: `~/.tokscale/events.jsonl`

You can also open `index.html` directly after running `refresh`.

## Terminal reports

```bash
python3 tokscale.py report --period daily
python3 tokscale.py report --period weekly
python3 tokscale.py report --period monthly
python3 tokscale.py report --period hourly --limit 48
python3 tokscale.py models
python3 tokscale.py models --breakdown
```

Short Bash commands are also available:

```bash
./daily.sh
./weekly.sh
./monthly.sh
```

## Hook / JSONL ingestion

Send one JSON object per line. The only required fields are `provider`, `model`, and token counts:

```bash
printf '%s\n' '{"provider":"OpenAI API","model":"gpt-5.6-sol","input_tokens":1200,"output_tokens":350}' \
  | python3 tokscale.py ingest
python3 tokscale.py refresh
```

Accepted cache field names include `cache_read_input_tokens` and `cache_creation_input_tokens`. A `timestamp` is optional and defaults to now.

## Pricing notes

Claude uses Anthropic’s standard global API rates. Cache writes use the 5-minute rate because the local Claude transcript does not include the cache TTL. Codex uses per-turn `last_token_usage` records from its rollout JSONL files when available; its cached-input count is included inside input tokens and is shown separately, not added again. SQLite thread totals are the fallback.
