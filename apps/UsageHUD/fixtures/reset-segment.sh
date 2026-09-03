#!/usr/bin/env bash
# §6 reset-segment: cl·pri 5h at 30%, resets in 2h — pairs with seed-history-reset.sh, which seeds
# a pre-reset 90% sample that must be ignored by trend math. Correct segment-only burn is
# +20%/hr (20%->30% over 30m); if the reset weren't detected, burn would come out large and
# negative instead (90%->30%).
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":30,"reset":"2h","state":"ok","asOf":$now,"active":true},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":20,"reset":"5d","state":"ok","asOf":$now,"active":true},
  {"label":"cx","provider":"codex","window":"5h","pct":10,"reset":"3h","state":"ok","asOf":$now},
  {"label":"cx","provider":"codex","window":"7d","pct":30,"reset":"2d","state":"ok","asOf":$now}
]
JSON
