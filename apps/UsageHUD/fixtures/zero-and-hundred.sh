#!/usr/bin/env bash
# Fixture: pct edge values — 0% and 100% on every group.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":0,"reset":"4h59m","state":"ok","asOf":$now},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":100,"reset":"now","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"5h","pct":100,"reset":"now","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"7d","pct":0,"reset":"6d23h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"5h","pct":0,"reset":"4h59m","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":100,"reset":"now","state":"ok","asOf":$now}
]
JSON
