#!/usr/bin/env bash
# Fixture: full 6-row layout (3 groups × 5h/7d) spanning all severity bands.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":45,"reset":"2h10m","state":"ok","asOf":$now},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":72,"reset":"3d4h","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"5h","pct":91,"reset":"38m","state":"ok","asOf":$now},
  {"label":"cl·sir","provider":"claude","window":"7d","pct":18,"reset":"6d2h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"5h","pct":63,"reset":"1h5m","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":95,"reset":"12h","state":"ok","asOf":$now}
]
JSON
