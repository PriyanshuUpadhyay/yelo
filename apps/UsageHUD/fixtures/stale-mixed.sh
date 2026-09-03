#!/usr/bin/env bash
# Fixture: cl·sir's asOf is 40m old (> 30m staleness threshold); the other two groups are fresh.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
stale=$(( now - 2400 ))
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":43,"reset":"21m","state":"ok","asOf":$now},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":56,"reset":"5d7h","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"5h","pct":32,"reset":"1h51m","state":"ok","asOf":$stale},
  {"label":"cl·sir","provider":"claude","window":"7d","pct":7,"reset":"6d7h","state":"ok","asOf":$stale},
  {"label":"cx","provider":"codex","window":"5h","pct":15,"reset":"2h33m","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":62,"reset":"1d3h","state":"ok","asOf":$now}
]
JSON
