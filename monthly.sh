#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 tokscale.py refresh >/dev/null
python3 tokscale.py report --period monthly "$@"
