---
status: accepted
date: 2026-09-27
deciders:
  - Priyanshu
supersedes:
  - 0002
informed-by:
  - tests/golden/cli
  - ~/.claude/reports/2026-09-27-public-repo-split.md
---

# 0018. Rewrite yelo in Rust as one binary

## Context and Problem Statement

yelo goes public next to swarm, which is one Rust binary. The Python yelo needed Python 3.11,
`uv`, and an editable checkout, and its shell integration ran a `profiles.pyz` archive on every
`claude` or `codex` start. A user who wants only the profiles had to keep a Python toolchain for
them. 0002 kept Python because a rewrite would discard debugged code and its tests.

## Considered Options

- Keep Python and ship a zipapp.
- Rewrite in Rust against the existing tests as a black-box contract.

## Decision Outcome

Chosen: rewrite in Rust. `tests/golden/cli` recorded 36 CLI cases from the Python yelo, and the
whole pytest suite ran the binary through `YELO_CMD`, so the rewrite was judged against the
old behavior, not against itself. The suite then moved to `cargo test` with the same cases. The shell integration calls the `yelo` on PATH; without it,
`claude` and `codex` run the plain vendor CLI. Usage fetch runs `curl` with the token on stdin,
so the crate needs no HTTP library. `yelo update` and `yelo release` are dropped; Homebrew,
`cargo install`, and a `git archive` step in the release workflow replace them. UsageHUD stays
Swift.

### Consequences

- Good: one binary, no interpreter at run time or in the tests, and the tests carried over
  unchanged in meaning.
- Bad: contributors need Rust to build and test.
