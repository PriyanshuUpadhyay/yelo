#!/usr/bin/env bash
# RED: cl·pri 5h at 60%, resets in 2h. Seed history 30→60 over 60m (30%/hr) → eta100 ≈ 1.3h < 2h.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":60,"reset":"2h","state":"ok","asOf":$now,"active":true},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":20,"reset":"5d","state":"ok","asOf":$now,"active":true},
  {"label":"cx","provider":"codex","window":"5h","pct":10,"reset":"3h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":30,"reset":"2d","state":"ok","asOf":$now}
]
JSON
