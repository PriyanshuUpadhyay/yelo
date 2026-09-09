---
status: accepted
date: 2026-09-09
deciders:
  - Priyanshu
informed-by:
  - ~/.claude/council-log/2026-09-09-worker-wake-push.md
  - assets/claude/scripts/herdr-bus.py
  - assets/claude/scripts/agent-handoff.py
  - assets/claude/scripts/agent-teammate.py
---

# 0013. Push worker wakes to the parent pane, and let publish own the wake

## Context and Problem Statement

Under Herdr the chair spawns workers as visible panes. To learn that a worker finished, the
chair had to arm `herdr-bus.py watch` as a background task, check its lease, and re-arm after
every wake, TTL expiry, and context compaction. The model kept forgetting, so completed work sat
unnoticed until the user asked. The parent pane is already known at spawn (`HERDR_PANE_ID`), and
the worker's prompt carried a pasted emit command with the bus root and seat name typed by hand.

## Considered Options

- A. Keep the chair-armed watch (status quo).
- B. Worker-side push: emit types one fixed doorbell into the recorded parent pane.
- C. Herdr-side daemon or plugin that watches agent status transitions.
- D. B for `done`, plus a user-facing signal for `blocked`.

## Decision Outcome

Chosen: D, with identity supplied by the infrastructure. Spawn plants `HERDR_PARENT_PANE`,
`HERDR_BUS_DIR`, and `HERDR_SEAT`; the chair writes `dispatch/<target>.json` before every prompt;
the worker runs only `agent-handoff.py publish --file <path>`; publish writes the envelope, emits
the bus event, and pushes one fixed doorbell line. The artifact plus `verify` stay the only
completion truth, and `watch` stays as the fallback. Codex threads are hidden at teardown by
`agent-handoff.py close <seat>`, because `codex archive` refuses a live thread. A three-model
council was unanimous GO-WITH-CHANGES; C lost because Herdr exposes no event surface, so a daemon
is a poll loop that recreates the "who keeps it alive" problem outside the harness.

### Consequences

- Good: no model-owned arming loop; a wake survives compaction; the worker cannot mistype the
  bus root or seat; a lost push still leaves the bus file for the next scan.
- Bad: worker-typed text enters the chair's input, so the doorbell must stay a fixed template
  and be treated as data (scan then verify), never as an instruction; a chair sitting in a
  permission dialog rejects the push; a test suite run inside a worker pane must scrub the
  planted variables or it will push into the live chair.
