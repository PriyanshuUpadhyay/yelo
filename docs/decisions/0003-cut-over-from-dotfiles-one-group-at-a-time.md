---
status: accepted
date: 2026-09-02
deciders:
  - Priyanshu
informed-by:
  - docs/superpowers/plans/2026-09-02-repo-split-scope.md
---

# 0003. Cut over from dotfiles one group at a time

In the context of a single machine that runs the tooling it is also moving, facing a choice
between one cutover and a group-by-group one, we chose to dogfood each group and cut it over
alone — `git rm` its files in `~/dotfiles`, restow, then `jello setup <group>` and
`jello doctor` — and neglected the single cutover, to keep every step reversible by one
`git revert` and one restow, accepting that both copies exist side by side while a group is in
flight and that `jello doctor` reports `owned-by-dotfiles` until its group is done.
