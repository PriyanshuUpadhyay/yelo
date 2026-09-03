#!/usr/bin/env bash
# Fixture: every group offline — no asOf on any row.
[ "${1:-}" = "--json" ] || exit 1
cat <<'JSON'
[
  {"label":"cl·pri","provider":"claude","state":"offline","reason":"no data"},
  {"label":"cl·sir","provider":"claude","state":"offline","reason":"no data"},
  {"label":"cx","provider":"codex","state":"offline","reason":"not set up"}
]
JSON
