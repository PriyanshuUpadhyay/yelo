# jello

One CLI for the multi-account profiles of `claude`, `codex`, and `prime-agent`.

Each of those tools keeps one account per home directory — `~/.claude/.profiles/NAME`,
`~/.codex-NAME`, `~/.prime/agent-NAME`. jello is the one place that knows how to find
them, name them, pick between them, and launch into the right one. There is deliberately
no default profile: an unmatched or ambiguous name is an error, because silently landing
on the wrong account spends the wrong subscription.

## Command groups

| group | what it does |
|---|---|
| `jello profile` | `list`, `menu`, `resolve`, `pick`, `sessions`, `create` across the three CLIs |
| `jello resume` | keeps the session-to-profile map a Claude Code hook writes, so a replayed `--resume` lands on the account that started it |
| `jello shell-init` | prints the zsh wrappers for `claude`, `cc`, `codex`, and `prime-agent` |
| `jello setup` | writes the shell cache, the Claude hook group, and the profile root, idempotently |
| `jello doctor` | reports every setup step as `ok`, `missing`, or `owned-by-dotfiles` |

## Install

```sh
uv tool install --editable ~/jello
```

Then add one line to `~/.zshrc`:

```sh
command -v jello >/dev/null && eval "$(jello shell-init zsh)"
```

`jello setup` writes the rest — the shell-init cache under `~/.cache/jello`, the
`jello resume map-session` hook group in `~/.claude/settings.json`, and
`~/.claude/.profiles`. It never edits `~/.zshrc`, never overwrites a target that another
package owns, and a second run changes nothing. `jello doctor` re-derives every verdict
from the filesystem and exits 0 when no step is missing.

## Requirements

Python 3.10 or newer. The standard library only: jello runs from a shell hook and from a
wrapper on every prompt, so it must not pay for an import tree or break when an
environment is half-installed.

## Runtime state is machine-local

The account homes hold live credentials and machine state, and none of it belongs in this
repository or any other: `auth.json` and `.claude.json` (tokens), the macOS Keychain items
Claude Code writes, `~/.claude/.profiles/` itself, and the session map under
`~/.claude/.profiles/.session-map/`. jello reads and writes those paths in place; it never
copies them into the repository, and the tests never read them — every test that touches
the filesystem runs against a temporary `HOME`.

## Tests

```sh
uv run --with pytest pytest tests -q
```
