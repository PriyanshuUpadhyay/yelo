---
status: accepted
date: 2026-08-21
deciders:
  - Priyanshu
informed-by:
  - home/.config/herdr/config.toml
  - home/.config/herdr/bin/go-launcher.sh
---

# 0010. Keep the Go launcher for worktree entry

In the context of `prefix+shift+g` navigation, facing a choice between a direct Worktrunk
key binding and the multi-mode Go launcher, we chose to keep the Go launcher and delegate
worktree switch and create operations to the Worktrunk plugin, and neglected replacing the
complete key binding with `worktrunk.open`, to preserve recent-directory and repository
navigation, accepting that the worktree action opens one additional picker.
<!-- Origin: ~/dotfiles docs/decisions/0010-keep-the-go-launcher-for-worktree-entry.md at 8e56172, copied unchanged. -->
