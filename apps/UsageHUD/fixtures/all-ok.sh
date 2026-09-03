#!/usr/bin/env bash
# Fixture: 3 groups all reporting, no offline/failure state.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":43,"reset":"21m","state":"ok","asOf":$now,"active":false},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":56,"reset":"5d7h","state":"ok","asOf":$now,"active":false},
  {"label":"cl·sir","provider":"claude","window":"5h","pct":32,"reset":"1h51m","state":"ok","asOf":$now,"active":true},
  {"label":"cl·sir","provider":"claude","window":"7d","pct":7,"reset":"6d7h","state":"ok","asOf":$now,"active":true},
  {"label":"cx","provider":"codex","window":"5h","pct":15,"reset":"2h33m","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":62,"reset":"1d3h","state":"ok","asOf":$now}
]
JSON
