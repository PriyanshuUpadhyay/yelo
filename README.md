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
| `jello doctor` | reports every setup step and HUD target as `ok`, `missing`, or `owned-by-dotfiles` |
| `jello usage` | `show [--json]`, `fetch`, `doctor` — the account usage meters the HUD reads |
| `jello hud` | `install`, `start`, `stop` — builds the Usage HUD app and runs it as a LaunchAgent |

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

## Usage meters

`jello usage show` prints one row per account and window — 5h, 7d, and the Fable weekly —
read from the cache files each account already keeps: the statusline caches
(`.usage-cache*.json`), the API caches (`.usage-api-cache*.json`), and the Codex rollout
files. It reads local files only: never the network, never the Keychain. `--json` prints
the row array the HUD app decodes.

`jello usage fetch` is the only writer of the API cache family. It reads each Claude
profile's OAuth token from the Keychain, asks the usage API, and rewrites
`.usage-api-cache.json`, `.usage-api-cache-fable.json`, and the Codex
`.usage-hud-api-cache.json` — one outcome line per profile, and no token in any output,
log, or cache file.

`jello usage doctor` audits the same ground read-only — row shapes, freshness, credential
presence, job liveness, and a token-leak scan over the HUD logs and every cache file — and
exits non-zero on any finding.

## The HUD app

The menu-bar app lives in this repository under `apps/UsageHUD`, a Swift package. It reads
its rows by running `jello usage show --json` and refreshes them with `jello usage fetch`.
launchd hands a job no PATH, so the LaunchAgent carries the absolute path of the launcher
in `JELLO_BIN`; `~/.local/bin/jello` is the fallback for a hand-started run.

```sh
jello hud install    # swift build -c release, ~/Applications/UsageHUD.app, the LaunchAgent
jello hud start      # bootstrap it into gui/<uid>
jello hud stop       # boot it out
```

`install` writes files and nothing else — it never calls `launchctl`, so starting the job
stays a separate, explicit action. A second `install` reports `plist unchanged`. The label,
the bundle identifier, and the ad-hoc signing identifier are all
`io.github.priyanshuupadhyay.jello-hud`, and the job logs to
`~/Library/Logs/jello-hud.out.log` and `jello-hud.err.log`.

### Dogfooding it beside the dotfiles HUD

The predecessor still ships from `~/dotfiles` under the label `work.example.usage-hud`. Both
can run at once — different labels, different bundles, different log files — so install the
jello one, start it, and compare the two windows before deciding:

```sh
jello hud install && jello hud start
jello doctor          # usage and hud read owned-by-dotfiles until the cutover
```

### The cutover

When the jello HUD has earned it, retire the dotfiles one, in this order:

```sh
launchctl bootout gui/$(id -u)/work.example.usage-hud
# in ~/dotfiles: git rm apps/usage-hud, home/.local/bin/usage-hud-*,
#                       home/Library/LaunchAgents/work.example.usage-hud.plist
stow -R home          # restow, dropping the retired links
```

`jello doctor` then reports `usage ok` and `hud ok`. Nothing else moves: the cache files,
`~/.local/state/usage-hud/history.json`, and the statusline writer in
`~/.claude/statusline-command.sh` stay exactly where they are.

## Requirements

Python 3.10 or newer. The standard library only: jello runs from a shell hook and from a
wrapper on every prompt, so it must not pay for an import tree or break when an
environment is half-installed.

The `hud` group additionally needs Swift 5.9 or newer and macOS 14 or newer, because it
compiles `apps/UsageHUD`. The `usage` group does not.

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
swift test --package-path apps/UsageHUD
```
