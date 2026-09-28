---
status: accepted
date: 2026-09-28
deciders:
  - Priyanshu
related:
  - 0015
informed-by:
  - ~/.flow/yelo/2026-09-28-main/03-contracts.md
---

# 0019. Put reset credits on the account rows

In the context of the usage snapshot that `yelo usage show --json` emits and the HUD reads,
facing the need to show each Codex account's usage limit reset credits, we chose two keys on
both data rows of the account, `resetCredits` and `resetCreditsExpireAt`, with `null` in the
list meaning a credit that never expires, and neglected a third row per account and a
top-level accounts object, to achieve a change that every consumer of the flat row list can
ignore or read without a new shape, accepting that the same values repeat on the 5h and 7d
rows and that a reader of one row cannot see that they belong to the account.
