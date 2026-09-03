#!/usr/bin/env bash
# Seeds $USAGE_HUD_STATE_DIR/history.json so pressure fixtures produce deterministic burn/eta.
# Usage: USAGE_HUD_STATE_DIR=/tmp/uhud-red fixtures/seed-history.sh <label> <window> <old_pct> <new_pct> <span_min>
set -euo pipefail
dir="${USAGE_HUD_STATE_DIR:?set USAGE_HUD_STATE_DIR}"
label="$1"; window="$2"; old="$3"; new="$4"; span_min="${5:-60}"
mkdir -p "$dir"
now=$(date +%s)
old_at=$(( now - span_min * 60 ))
cat > "$dir/history.json" <<JSON
[
  {"label":"$label","window":"$window","pct":$old,"asOf":$old_at},
  {"label":"$label","window":"$window","pct":$new,"asOf":$now}
]
JSON
echo "seeded $dir/history.json: $label $window ${old}→${new} over ${span_min}m"
