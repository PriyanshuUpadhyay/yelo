#!/usr/bin/env bash
# Fixture: both Claude accounts ok, Codex offline ("not set up").
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":43,"reset":"21m","state":"ok","asOf":$now},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":56,"reset":"5d7h","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"5h","pct":32,"reset":"1h51m","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"7d","pct":7,"reset":"6d7h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","state":"offline","reason":"not set up"}
]
JSON
