---
status: accepted
date: 2026-08-23
deciders:
  - Priyanshu
informed-by:
  - home/.prime/agent/extensions/herdr-auto-resume.ts
  - home/.config/herdr/local-plugins/prime-agent-resume/
---

# 0012. Keep Prime pane auto-resume armed

In the context of Prime Agent chats inside Herdr, facing loss of the pane process after a cold
Herdr restart, we chose to keep the pane-to-session mapping armed until
`/herdr-auto-resume off` and neglected clearing the mapping when Prime Agent exits, to resume the
saved chat without manual work, accepting that an intentionally exited pane starts Prime Agent
again after the next cold restart unless the mapping is explicitly disabled.
<!-- Origin: ~/dotfiles docs/decisions/0012-keep-prime-pane-auto-resume-armed.md at 8e56172, copied unchanged. -->
