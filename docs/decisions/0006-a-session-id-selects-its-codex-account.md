---
status: accepted
date: 2026-09-08
deciders:
  - Priyanshu
supersedes-in-part:
  - docs/decisions/0005-per-account-launchers.md
---

# 0006. A session id selects its own Codex account

In the context of the profile integration having brought back a `codex` wrapper -- a plain
launch picks the account about to waste the most usage, and every session-bound subcommand
showed the account menu -- facing `codex resume <id>` failing with "No saved session found"
whenever the account chosen in that menu was not the one whose home holds the session, we
chose to look the id up across every Codex home and run in the one that owns it, and
neglected both keeping the menu for every session-bound subcommand (the 0005 position that
a person names the account) and a session map written on every launch (the 0004 mechanism
that 0005 removed), to achieve a resume that needs nothing more than the id Codex itself
prints, accepting that the wrapper globs each home's rollouts on every `resume`, `fork`,
`archive`, `unarchive`, and `delete`, that a session *name* still asks, and that `--last`
now means the newest session in any account rather than in the vendor default.

The lookup is one new runtime subcommand, `owner --cli codex (ID | --last)`, in the same
`profiles.pyz` the wrapper already calls. Rollout filenames carry the id, so no file is
opened, and archived sessions are searched too so `unarchive` can find its target. Bare
`codex resume`, `login`, and `logout` keep the menu. An explicit `--profile` and a
caller-set `CODEX_HOME` still win.

0005 accepted "resume runs under whatever account the vendor default is" as a consequence
of having no wrapper at all. With the wrapper back, that consequence is what this record
reverses for Codex. Everything else 0005 settled -- one launcher per account, no jello code
in a hook or a spawn, the resolver in Python -- stands as written.
