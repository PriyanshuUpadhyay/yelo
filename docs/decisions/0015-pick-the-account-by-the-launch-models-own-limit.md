---
status: accepted
date: 2026-09-17
deciders:
  - Priyanshu
related:
  - 0006
informed-by:
  - src/yelo/profile/core.py
  - codex app-server account/rateLimits/read (rateLimitsByLimitId)
---

# 0015. Pick the account by the launch model's own limit

In the context of automatic account selection, facing launches that spend a model with its own
limit (Claude Fable, GPT-5.3-Codex-Spark), we chose to take the model from the arguments, then
the default (Claude settings, each Codex home's `config.toml`), and rank accounts by that model's
limit, and neglected ranking by the best of all windows, to achieve picks that spend the limit
the launch will use, accepting that a 5h window near its reset no longer wins a Fable launch and
that a Codex limit is matched to a model by its name.
