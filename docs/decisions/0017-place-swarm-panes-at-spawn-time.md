---
status: accepted
date: 2026-09-18
deciders:
  - Priyanshu
related:
  - 0016
informed-by:
  - assets/claude/scripts/swarm-spawn-role.py
  - dotfiles home/.config/herdr/bin/swarm-split.py
  - ~/.swarm/adapters/herdr.conf
  - session 31e04a9e-692f-45de-80e2-2aa5f20e76fa
---

# 0017. Place swarm panes in the main-grid shape at spawn time

## Context and Problem Statement

The Herdr adapter split the orchestrator pane to the right for every child, so a run with three
seats left the orchestrator in a thin column and the user pressed `ctrl+alt+shift+m` by hand to
get the main grid, which is the orchestrator on the left and the children stacked on the right.
Calling that same layout action automatically after each spawn gave the right shape but moved
the view, because the action ends with `herdr tab focus` and pulled the user into the swarm
workspace from wherever they were working.

Polling probes found the rest. Herdr refuses a pane move inside one tab, so the layout action
parks the children in a scratch tab named `reshaping` and then moves them back. That tab lives
about 150 ms per rebuild and blinks in the tab bar. Restoring the tab that was focused before
the rebuild removed the workspace switch, but not the blink, because the scratch tab is what
the rebuild itself needs.

## Considered Options

- Keep the rebuild after every spawn and accept one blink for each child.
- Rebuild once, after the orchestrator reports that the last child is spawned.
- Split each child directly into its final place and equalise the column with `herdr pane resize`.

## Decision Outcome

Chosen: split each child into its final place. The adapter `spawn` verb calls
`$HOME/.config/herdr/bin/swarm-split.py`, which splits the orchestrator to the right for the
first child, splits the bottom child down for each later one, equalises the right column, and
prints the new pane id. No pane moves, so the run needs no scratch tab and no focus call, and
`swarm-spawn-role.py` keeps no layout step.

Two measured Herdr rules carry the code. `herdr pane resize --pane P --direction down|up
--amount <delta>` moves the border on that side of P and always grows P. A split ratio is the
top pane's share of that split's own rect and is scale free, so one layout read gives every
delta in the column.

### Consequences

- Good: a spawn changes only the panes it adds, so the user keeps their workspace, tab, and focus.
- Good: every spawn path gets the layout, because the adapter owns it and no caller has to ask.
- Good: `ctrl+alt+shift+m` still rebuilds by hand, and the focus fix in dotfiles a0d7982 keeps
  that rebuild from moving the view.
- Bad: the Herdr adapter now depends on a dotfiles script, so a machine without that file cannot spawn.
- Bad: the helper treats every pane right of the orchestrator as a child, so a user pane parked there is resized too.
- Bad: child heights land within two terminal rows of each other, because a ratio rounds to whole cells.
