#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
pg_bin="/Applications/Postgres.app/Contents/Versions/18/bin"
pg_data="${TOKSCALE_PGDATA:-$HOME/.tokscale/postgres}"
pg_log="${TOKSCALE_PGLOG:-$HOME/.tokscale/postgres.log}"
pg_port="${TOKSCALE_PGPORT:-55432}"
pg_user="${TOKSCALE_PGUSER:-$(id -un)}"

mkdir -p "${pg_data:h}"

if [[ ! -f "$pg_data/PG_VERSION" ]]; then
    "$pg_bin/initdb" \
        -D "$pg_data" \
        --username="$pg_user" \
        --auth-local=trust \
        --auth-host=trust \
        --encoding=UTF8 \
        --no-locale
fi

if ! "$pg_bin/pg_ctl" -D "$pg_data" status >/dev/null 2>&1; then
    "$pg_bin/pg_ctl" \
        -D "$pg_data" \
        -l "$pg_log" \
        -o "-p $pg_port -k /tmp -c listen_addresses=127.0.0.1" \
        start -w
fi

export PGHOST=127.0.0.1
export PGPORT="$pg_port"
export TOKSCALE_PSQL="$pg_bin/psql"

if [[ "$("$pg_bin/psql" -X -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname = 'tokscale'")" != "1" ]]; then
    "$pg_bin/createdb" -T template0 -E UTF8 tokscale
fi

"$pg_bin/psql" -X -v ON_ERROR_STOP=1 -d tokscale -f "$project_dir/storage/schema.sql"
exec python3 "$project_dir/storage/snapshot.py"
