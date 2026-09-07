---
status: accepted
date: 2026-09-02
deciders:
  - Priyanshu
informed-by:
  - docs/superpowers/plans/2026-09-02-repo-split-scope.md
---

# 0001. Use one tool named jello

In the context of moving the agent-workstation tooling out of `~/dotfiles`, facing a choice
between two repositories (profiles plus usage in one, Herdr ops in another) and one, we chose
one private repository `PriyanshuUpadhyay/jello` holding one CLI named `jello`, and neglected
the two-tool split, because resume and the Prime integration cross the profile/Herdr line — a
Herdr cold restart replays `claude --resume` with no environment and the pane must find its
account — accepting that one tool grows more command groups than either half would.
