#!/usr/bin/env bash
# AMBER: cl·pri 5h at 80%, resets in 2h. Seed history 75→80 over 60m (5%/hr) → eta100=4h (>2h, not red);
# projected = 80 + 5*2 = 90 (≥85) → amber.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":80,"reset":"2h","state":"ok","asOf":$now,"active":true},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":20,"reset":"5d","state":"ok","asOf":$now,"active":true},
  {"label":"cx","provider":"codex","window":"5h","pct":10,"reset":"3h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":30,"reset":"2d","state":"ok","asOf":$now}
]
JSON
