---
status: accepted
date: 2026-10-06
deciders:
  - Priyanshu
related:
  - 0015
  - 0016
informed-by:
  - src/profile.rs command_pick
---

# 0020. Spread a burst of automatic picks across accounts

In the context of the automatic account pick, which reads a usage cache that only a HUD refresh
updates, facing a swarm that starts several panes in seconds and puts every one on the same
account, we chose to log each automatic pick in `$XDG_CONFIG_HOME/yelo/picks.log` and divide an
account's score by 1 + its picks in the last 10 minutes, under an exclusive file lock from read
to append, and neglected a hard per-account threshold with a fallback account and a count of live
sessions, to achieve launches that spread in proportion to each account's score with no number to
tune, accepting that the count measures launches and not work, so a heavy session older than 10
minutes no longer weighs on its account, and that `--profile NAME` and resume launches are not
counted.
