---
status: accepted
date: 2026-09-16
deciders:
  - Priyanshu
supersedes-in-part:
  - docs/decisions/0005-per-account-launchers.md
informed-by:
  - Claude Code 2.1.273 environment variable strings
  - src/yelo/launchers.py
  - src/yelo/profile_shell.zsh
---

# 0014. Use a mirrored Claude config directory per account

## Context and Problem Statement

ADR 0005 made each Claude launcher export `CLAUDE_PROFILE_DIR` and a separate
`CLAUDE_SECURESTORAGE_CONFIG_DIR`. Claude Code 2.1.273 does not read
`CLAUDE_PROFILE_DIR`. The Keychain credential was separate, but all accounts still read and
wrote `~/.claude.json`, so the account shown by `/status` was the one that refreshed the shared
file most recently.

## Considered Options

- Keep the shared `~/.claude.json` and separate only Keychain credentials.
- Give each account a fully separate Claude config directory.
- Give each account its own `.claude.json` and link the remaining config entries to the shared home.

## Decision Outcome

We will export `CLAUDE_CONFIG_DIR` with the profile directory and keep
`CLAUDE_SECURESTORAGE_CONFIG_DIR` unchanged. Each profile directory will contain a real
`.claude.json` and relative links to the top-level entries in `~/.claude`. Yelo-owned account
files, caches, `.profiles`, and `.claude.json` are excluded from the links. `profile create`,
`profile sync`, and setup will make the mirror. They will report real entries as drift and will
not delete them.

### Consequences

- Good: each account has its own OAuth identity file without a new login, while settings,
  hooks, skills, plugins, projects, sessions, memory, history, and todos stay shared.
- Good: the initial seed removes `oauthAccount` from the base config and keeps its other keys.
- Bad: project trust and user-scope MCP servers can diverge after `.claude.json` is seeded.
- Bad: a new top-level entry that Claude creates in a profile directory stays there until
  `yelo profile sync --cli claude` reports it for a manual merge.
