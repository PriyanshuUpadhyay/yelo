---
status: accepted
date: 2026-09-18
deciders:
  - Priyanshu
supersedes-in-part:
  - docs/decisions/0005-per-account-launchers.md
informed-by:
  - assets/claude/scripts/swarm-spawn-role.py
  - src/yelo/profile_shell.zsh
  - session aaaaaaaa-5cf8-475e-857f-611953d56475
---

# 0016. Swarm is the only spawn path, and a pane starts the CLI by its command word

## Context and Problem Statement

A council seat asked for `council.claude`, which resolves to a Fable model, and the pane booted
on the account whose Fable window was 93% spent. The route was correct and the account was not.
`swarm spawn` was given `/Users/priyanshu/.local/bin/claude`, an absolute path, so the pane's
shell ran the binary and never reached the `claude` function that ADR 0015 gave the
model-aware account pick. The pane then fell back to the bare `~/.claude` config.

The same cutover left three more things behind. `agent-teammate.py` was still the launcher for
`herdr-registry.py`, which nothing but `herdr-codex-canary.py` called. Worker panes were still
recognized by `AGENT_TEAMMATE_CHILD` and `HERDR_AGENT_PANE`, which no live code plants any
more, so every swarm worker read the orchestrator contract. And `_cprofile` still read
`~/.claude/.profiles/.session-map`, whose writer went away with ADR 0005.

## Considered Options

- Teach every spawn path to resolve the account itself, with `yelo profile pick --model`.
- Give `swarm spawn` a per-account launcher, `~/.local/bin/claude-<name>`, chosen by the route.
- Give `swarm spawn` the command word and let the interactive shell resolve the account.

## Decision Outcome

We will name the CLI by its command word, `claude`, `codex`, or `agy`. `swarm spawn` types its
line into the pane's interactive shell, so the yelo function receives it and picks the account
by the launch model, with no second copy of that rule. `swarm-spawn-role.py` carries the five
helpers it needs and is the only spawn path, so `agent-teammate.py`, `herdr-registry.py`,
`herdr-codex-canary.py`, and their tests are deleted. A worker is the seat that `swarm spawn`
named, a `SWARM_AGENT_ID` other than `orchestrator`, or a Herdr agent pane. The session-map
read is removed, because no writer has filled it since ADR 0005.

### Consequences

- Good: one rule picks the account, and a Fable seat lands on the account that has Fable left.
- Good: 8,914 lines of launcher, registry, and canary code leave the repository.
- Good: hooks and guards read one worker marker that the spawn itself sets.
- Bad: a pane that starts the CLI without an interactive shell gets the bare config again,
  because a command word resolves to the binary there.
- Bad: `claude --resume <id>` with no account named now always asks, where a stale map entry
  used to answer, sometimes with the wrong account.
- Bad: a sandboxed Codex root has no drop box any more, so it reports that it cannot spawn.
