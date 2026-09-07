---
status: accepted
date: 2026-09-02
deciders:
  - Priyanshu
informed-by:
  - docs/superpowers/plans/2026-09-02-repo-split-scope.md
---

# 0002. Use Python and the standard library only

In the context of porting about 8,500 debugged lines with about 7,900 lines of tests, facing a
choice between keeping Python and rewriting in Rust or Zig, we chose Python 3.10 or newer with
the standard library alone, installed by `uv tool install --editable`, and neglected the rewrite,
because the rewrite would discard every debugged line and its tests while Python starts in about
20 ms and `jello shell-init zsh` is cached under `~/.cache/jello` so a shell start runs no
Python at all, accepting that a hot path may later need a different answer — the `--json`
boundary between groups keeps that open one group at a time.

**Amended 2026-09-03:** the floor moves from Python 3.10 to 3.11. `jello herdr canary`
reads the Codex config through `tomllib`, which is standard library only from 3.11, so on
3.10 that command answers with one error line and seven of its tests skip. Raising the
floor keeps "the standard library alone" true of every command rather than of most of them;
no dependency is added and nothing else in this record changes.
