#!/usr/bin/env bash
# Fixture: Codex accounts with usage limit reset credits. `cx` has three, one without expiry;
# `cx·work` has a count but no list. Claude rows have none.
[ "${1:-}" = "--json" ] || exit 1
now=$(date +%s)
cat <<JSON
[
  {"label":"cl·pri","provider":"claude","window":"5h","pct":43,"reset":"21m","state":"ok","asOf":$now,"seenAt":$now},
  {"label":"cl·pri","provider":"claude","window":"7d","pct":56,"reset":"5d7h","state":"ok","asOf":$now,"seenAt":$now},
  {"label":"cx","provider":"codex","window":"5h","pct":15,"reset":"2h10m","state":"ok","asOf":$now,"seenAt":$now,"source":"api","resetCredits":3,"resetCreditsExpireAt":[$((now + 6 * 86400)),$((now + 7 * 86400)),null]},
  {"label":"cx","provider":"codex","window":"7d","pct":62,"reset":"3d1h","state":"ok","asOf":$now,"seenAt":$now,"source":"api","resetCredits":3,"resetCreditsExpireAt":[$((now + 6 * 86400)),$((now + 7 * 86400)),null]},
  {"label":"cx·work","provider":"codex","window":"5h","pct":40,"reset":"1h5m","state":"ok","asOf":$now,"seenAt":$now,"source":"api","resetCredits":2},
  {"label":"cx·work","provider":"codex","window":"7d","pct":10,"reset":"5d","state":"ok","asOf":$now,"seenAt":$now,"source":"api","resetCredits":2}
]
JSON
