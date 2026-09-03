#!/usr/bin/env bash
# §6 reset-segment fixture: cl·pri 5h had a pre-reset high (90%), dropped to 20% (a >10pt drop —
# Pressure.swift's resetDropThreshold — starts a new segment), then rose to 30% (current, matches
# fixtures/reset-segment.sh's --json output). If currentSegment() incorrectly kept the pre-reset
# sample, burn rate would compute a large NEGATIVE %/hr (90→30 looks like a drop) instead of the
# correct +20%/hr climb from the new segment alone (20→30) — the bubble's ▲ only shows on a
# positive burn ≥3%/hr, so a wrong sign/magnitude is visible at a glance.
# Usage: USAGE_HUD_STATE_DIR=/tmp/uhud-reset fixtures/seed-history-reset.sh
set -euo pipefail
dir="${USAGE_HUD_STATE_DIR:?set USAGE_HUD_STATE_DIR}"
mkdir -p "$dir"
now=$(date +%s)
pre_reset_at=$(( now - 50 * 60 ))
post_reset_at=$(( now - 30 * 60 ))
cat > "$dir/history.json" <<JSON
[
  {"label":"cl·pri","window":"5h","pct":90,"asOf":$pre_reset_at},
  {"label":"cl·pri","window":"5h","pct":20,"asOf":$post_reset_at},
  {"label":"cl·pri","window":"5h","pct":30,"asOf":$now}
]
JSON
echo "seeded $dir/history.json: cl·pri 5h reset segment 90 (pre-reset, must be ignored) -> 20 -> 30 (current)"
