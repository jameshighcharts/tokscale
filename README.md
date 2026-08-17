# tokscale

One-file, standard-library CLI for local Codex and Claude token usage.

```bash
python3 tokscale.py models --breakdown
python3 tokscale.py models --breakdown -- daily
```

The first command reports all locally stored usage. The second reports today
only. Replace `daily` with `weekly`, `monthly`, or `all` as needed. The `--`
ends options so the final word is treated as the time period.

It reads `~/.codex/sessions`, `~/.codex/archived_sessions`, and
`~/.claude/projects`. No install or third-party package is required.
