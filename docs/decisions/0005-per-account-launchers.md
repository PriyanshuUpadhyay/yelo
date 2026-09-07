---
status: accepted
date: 2026-09-04
deciders:
  - Priyanshu
supersedes-in-part:
  - docs/decisions/0004-jello-is-a-setup-master.md
---

# 0005. One launcher per account, and no wrappers at all

In the context of 0004 having moved the account logic out of `jello` and into a rendered zsh
file -- a second resolver, a session map, a hook on every prompt, and a pick that still
asked `jello` when nothing named an account -- facing a choice between keeping that shell
layer and removing the layer itself, we chose to remove it: `claude`, `codex`, and
`prime-agent` are the vendor binaries with no account logic anywhere, and `jello setup
launchers` writes one three-line file per account under `~/.local/bin`.

`claude-sid` is now a command. Its whole content is the environment the wrapper used to
export:

```sh
#!/bin/sh
# written by jello setup launchers: account sid
exec env AGENT_PROFILE_LABEL=sid CLAUDE_PROFILE_DIR=... CLAUDE_SECURESTORAGE_CONFIG_DIR=... claude "$@"
```

That reverses three things 0004 settled. The rendered `~/.config/jello/shell.zsh` and the
`.zshrc` line that sourced it are gone, so a shell start reads nothing. The session-map hook
and `jello resume prune` are gone, so no jello code runs on a prompt. And the zsh copy of
`profile/core.py`'s matching rules is gone with them: nothing resolves a name at launch,
because the name is the command. `jello profile`, `jello usage`, and `jello hud` keep the
full resolver for the commands a person types, and `profile create` writes the new account's
launcher so nobody has to remember the setup step.

Three things bought it. Nothing of jello runs at launch, in a hook, or in a spawn -- not as
a dependency and not as a question, which is what 0004 could not quite say. There is one
account rule instead of two, so a resolver in zsh cannot drift from the one in Python. And a
launcher is inspectable: `cat $(which claude-sid)` is three lines a person can read, where
the old answer was a 700-line generated file.

Two consequences are accepted rather than solved. `claude --resume <sid>` runs under
whatever account the vendor default is, because the session map that named one is gone --
resume through `claude-<name> --resume <sid>`, or let Herdr's restore replay the launcher it
recorded. And a codex worker spawned with no `--profile` and no inherited `CODEX_HOME` runs
the base `~/.codex` account rather than one chosen from usage: the launchers carry no
judgement, and picking the account to spend is now something a person does by typing a
name.

`assets/` stops being a byte-for-byte copy of `~/dotfiles` at the same time. The ops scripts
are jello's files to change -- `agent-teammate.py` already carried three ruled deltas -- so
the manifest of source digests and the parity gate go, and the asset tests check what an
install actually needs: every link target exists, and every script is executable.
