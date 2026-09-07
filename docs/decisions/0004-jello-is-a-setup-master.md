---
status: accepted
date: 2026-09-04
deciders:
  - Priyanshu
supersedes-in-part:
  - docs/decisions/0001-use-one-tool-named-jello.md
  - docs/decisions/0002-use-python-and-the-standard-library-only.md
  - docs/decisions/0003-cut-over-from-dotfiles-one-group-at-a-time.md
---

# 0004. jello is a setup master, not a launch-path dependency

In the context of `jello` having grown into a dependency of every launch -- the `claude`,
`codex`, and `prime-agent` wrappers ran `jello profile resolve` before each start, the two
Claude hooks ran `jello resume map-session` on every prompt, and `agent-teammate.py` shelled
out to `jello profile resolve` before each spawn -- facing a choice between keeping that and
confining `jello` to the work it does once, we chose to make `jello` a setup tool: it writes
files and links, and nothing on a launch path executes it.

That reverses two things 0001 and 0003 had settled. 0001 read "one CLI" as "one CLI that
owns the behavior too", and every ported group became a `jello` subcommand; the parts that
run on a launch path -- the wrappers' resolver, the session-map hook, `agent-teammate.py`,
`agent-handoff.py`, `agent-host-context.py`, `herdr-bus.py`, `herdr-registry.py`,
`herdr-codex-canary.py`, and `ensure-agent-cwd-trust.py` -- go back to being scripts,
carried in `assets/` and installed as symlinks. 0003's cutover shape is unchanged, but its
unit is now the file rather than the command group: the same paths, so a cutover is still
`git rm` in `~/dotfiles`, restow, `jello herdr setup`.

Three costs bought it. A Python interpreter start no longer sits between a user and a
prompt, or between a session and its own hook. A `jello` that is broken, half-installed, or
absent can no longer stop a launch -- the wrappers read the account layout in zsh, off the
same directories `jello profile` reads. And the ops scripts stay the files their own tests,
the `herdr-ops` skill, and the Prime extension already name, so those pointers need no
rewriting and cannot drift.

The costs accepted: the wrappers carry a second resolver in zsh, which is a copy of the
matching rules in `src/jello/profile/core.py` rather than a call into it -- for claude all
four stages and both address sources (the `email` file, then the profile's own
`.claude.json`), and for codex and prime the name and directory alone, because those homes
keep their address inside an id_token nothing on a launch path should decode -- and the
usage-based
auto-pick is gone -- judging which account to spend needs the usage data only `jello` reads,
so a launch with no account named now asks (a menu on a terminal, one refusal anywhere else)
instead of guessing. `jello profile`, `jello usage`, and `jello hud` keep the full resolver
for the commands a person types.

What stays a `jello` command: `profile create`, `profile sessions`, `setup`, `herdr setup`,
`prime setup`, `prime probe`, `doctor`, `herdr doctor`, `usage`, `hud`, and `resume prune`.

Two facts in 0002 are replaced rather than amended, because a record body is immutable here:
the shell text is no longer cached under `~/.cache/jello` but installed at
`$XDG_CONFIG_HOME/jello/shell.zsh` (else `~/.config/jello/shell.zsh`) by `jello setup shell`,
which is what `~/.zshrc` sources, and the Python 3.11 floor is now owed to
`assets/claude/scripts/herdr-codex-canary.py`, an installed script that reads the Codex
config through `tomllib`, rather than to a `jello herdr canary` command, which this record
removed. 0002's choice of Python and the standard library alone is untouched.
