# Decision records

ADR format. Read this index before proposing changes. Record bodies are immutable —
reversals supersede, never edit. 0014 and 0006 reverse parts of 0005, which reverses part of 0004, which
reverses part of 0001, 0002, and 0003; every one of those bodies stands as written. 0010 and 0012 are copies of
the dotfiles records that came with the Herdr assets; their origin line names the source
commit.

| #    | Title                                              | Status   | Date       | Origin          |
|------|----------------------------------------------------|----------|------------|-----------------|
| 0014 | Use a mirrored Claude config directory per account | accepted | 2026-09-16 | jello           |
| 0013 | Push worker wakes to the parent pane, publish owns the wake | accepted | 2026-09-09 | jello           |
| 0012 | Keep Prime pane auto-resume armed                  | accepted | 2026-08-23 | dotfiles 8e56172|
| 0006 | A session id selects its own Codex account          | accepted | 2026-09-08 | yelo            |
| 0010 | Keep the Go launcher for worktree entry            | accepted | 2026-08-21 | dotfiles 8e56172|
| 0005 | One launcher per account, and no wrappers at all    | accepted | 2026-09-04 | jello           |
| 0004 | jello is a setup master, not a launch-path dependency | accepted | 2026-09-04 | jello        |
| 0003 | Cut over from dotfiles one group at a time         | accepted | 2026-09-02 | jello           |
| 0002 | Use Python and the standard library only           | accepted | 2026-09-02 | jello           |
| 0001 | Use one tool named jello                           | accepted | 2026-09-02 | jello           |
