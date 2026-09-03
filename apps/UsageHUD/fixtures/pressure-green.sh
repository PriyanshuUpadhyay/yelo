#!/usr/bin/env bash
# GREEN: cl·pri 5h at 40%, resets in 4h. Seed history 38→40 over 60m (2%/hr) → projected ≈ 48 (<85) → green.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":40,"reset":"4h","state":"ok","asOf":$now,"active":true},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":20,"reset":"5d","state":"ok","asOf":$now,"active":true},
  {"label":"cx","provider":"codex","window":"5h","pct":10,"reset":"3h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":30,"reset":"2d","state":"ok","asOf":$now}
]
JSON
