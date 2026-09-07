# jello

Claude and Codex account profiles, with a local usage HUD for macOS.

Use `claude` or `codex` normally. New sessions automatically select an account from
its available usage. Add `--profile` to choose from a menu, or `--profile NAME` to
select an account directly. Setup installs the selector separately from Jello.

## Install

Requirements: Python 3.11 or newer, `uv`, and the Claude or Codex CLI you use.
UsageHUD also requires macOS 14 or newer and Swift 5.9 or newer. A Homebrew install
also requires the Xcode Command Line Tools.

With Homebrew:

```sh
brew tap priyanshuupadhyay/tap
brew trust --formula priyanshuupadhyay/tap/jello   # Homebrew 6 or newer
brew install priyanshuupadhyay/tap/jello
jello setup
jello hud install && jello hud start
```

Homebrew builds the CLI and the HUD app on your Mac during install; `jello hud install`
copies the app into `~/Applications`. Update with `brew upgrade priyanshuupadhyay/tap/jello`.
`jello update` is for Git checkouts only. Use the full formula name: homebrew-core has an
unrelated formula also called `jello`.

From a clone of this repository:

```sh
uv tool install --editable .
uv tool update-shell
jello setup
```

Open a new terminal after setup to load the profile integration. Setup supports zsh
and adds its source line to `.zshrc`. Keep Python 3.11 or newer on PATH as `python3`.
Keep the checkout in place: the editable install and HUD source build use it.
Installing a wheel is not supported for the HUD.

## Add accounts

```sh
jello profile create --cli claude work
claude --profile work auth login
claude --profile work

jello profile create --cli codex personal
codex --profile personal login
codex --profile personal
```

Add `--yes` to `profile create` in a non-interactive terminal. Claude also accepts
`--email you@example.com` to label an account.

Existing accounts are discovered under `~/.claude/.profiles/NAME`, `~/.codex`, and
`~/.codex-NAME`. Run `jello setup launchers` to create or update their commands.
A base Codex account can have a name in `~/.codex/profile-label`.

```sh
jello profile list --cli claude
jello profile list --cli codex --usage
jello profile resolve --cli claude work
jello profile pick --cli codex
jello profile sessions --cli codex --all
jello doctor
```

## Select an account

```sh
claude                         # Automatically select an account
codex                          # Automatically select an account
claude --profile               # Open the account menu
codex --profile                # Open the account menu
claude --profile work          # Select a named account
codex --profile personal
claude profile list            # List accounts and usage
codex profile list
codex --profile personal resume # Resume within this account
```

Automatic selection prefers signed-in accounts with usable capacity that will reset
soon. An explicitly inherited account stays selected. Commands that manage credentials
or sessions ask for an account when none is supplied; they do not automatically switch
accounts. Menus require a terminal. An unknown or ambiguous name is an error, with no
fallback to another account.

The selection score is `fraction of quota remaining / fraction of time remaining`.
The time fraction has a 2% minimum. Each account uses its highest window score;
ties prefer more remaining capacity. Accounts with usable capacity take priority
over exhausted accounts. The normal windows are five hours and seven days.

For a Fable startup model, Claude also includes the Fable weekly window and excludes
accounts with known exhausted Fable usage, even when only one account is signed in.
If all are exhausted, it stops instead of starting Claude. The startup model comes
from `--model`, environment variables, and local user/project settings, including
`--settings` overrides. Explicit account selection still wins. Changing models
inside a running Claude session does not select another account.

Selection reads cached usage; it does not fetch from the network. Missing Fable
data is not treated as proof of exhaustion. Use the HUD's Refresh button to update
the samples before selecting from them.

In the shell integration, Codex's `--profile NAME` selects an **account**. Use
`codex --profile personal -p deep-review` to also select native configuration settings.
`command codex --profile deep-review` bypasses the account wrapper entirely.
The optional named shortcuts, such as `claude-work` and `codex-personal`, still work.

`~/.config/jello/shell.sh` and `profiles.pyz` contain the installed integration.
Profile selection, menus, and session launches keep working after `uv tool uninstall
jello`, provided Python and the vendor CLIs remain installed. The archive contains its
own copy of the selector code; it does not import the removed package or checkout.
Creating new accounts through `claude profile create` or `codex profile create` still
requires Jello. Existing accounts are discovered each time, without regenerating a list.

Run `jello setup` to update an older integration. It backs up a known generated shell
file and preserves `_codex_host_guard`. To load it in the current terminal:

```sh
source "$HOME/.config/jello/shell.sh"
```

Custom shell files and symlinks are preserved. If `.zshrc` is a symlink and does not
already source the integration, setup prints the line to add to its owner file.

Setup preserves foreign files at launcher paths and reports them. Repeating setup
changes only missing or outdated integration files and launchers. Account credentials stay in their
account homes or the macOS Keychain; they are not copied into this repository.

## UsageHUD

```sh
jello hud install
jello hud start
jello hud stop
```

The HUD opens at the top of the screen. Move the pointer over the notch to see your
accounts, or choose **Show usage** from its menu-bar menu. The menu action keeps the
panel open until you close it. Use **Refresh** or Command-R to run `jello usage fetch`
and display the updated usage. Escape closes the panel. Accounts are grouped by provider with shared usage-window
columns. Long account lists scroll while Refresh and Close stay visible.

The HUD reads local usage files every two minutes and when you open it. These automatic
reads do not use the network or read credentials. Clicking Refresh requests usage from
the providers and can require network or Keychain access. It does not start an agent,
control another app, or access your music library. A clock marks an older sample.
A missing reading is shown as **No sample**, never as zero usage.

Claude readings come from existing `.usage-cache*.json` and `.usage-api-cache*.json`
files. Codex readings come from `.usage-hud-api-cache.json` and recent session logs.
Jello does not install a Claude status-line writer. If no cache exists, use the
Refresh button or the terminal command below to create one. The HUD reads usage
amounts and reset times; it does not display session messages.

`hud install` builds `~/Applications/UsageHUD.app` and writes a LaunchAgent. It does
not start the app. The app label is `io.github.priyanshuupadhyay.jello-hud`.

## Online refresh

```sh
jello usage show
jello usage show --json
jello usage fetch
jello usage doctor
```

`usage show` reads local usage only. It does not read credentials or call the Keychain.

`usage fetch` is the online action used by the HUD's Refresh button. It can also run
directly in a terminal. It reads Claude credentials from the Keychain and requests
usage from Anthropic. It uses the installed Codex app-server
usage method for Codex accounts. These actions can need network or Keychain access;
they run from the HUD only when you request Refresh. Expired Claude credentials
return `auth-stale`.
Sign in through the named launcher, then retry. Fetch does not start a Claude agent
to renew credentials, run a prompt, or load its hooks and integrations.

`usage doctor` also checks credentials and job status. Run it from a terminal when
you need to diagnose a problem; the HUD does not run it.

## Update and remove

```sh
jello update --check
jello update
jello update --hud
```

`update` fast-forwards the installed Git checkout from its configured upstream,
reinstalls Jello with `uv`, then runs the updated setup. It stops if there are local
changes, no upstream, or a merge is needed. It does not switch branches or discard
work. `--check` checks local prerequisites; it does not fetch remote updates.
`--hud` also rebuilds an installed HUD and restarts it if it was running. A failed
build leaves the running app alone. Claude and Codex are updated with their own
installers.

After a manual source update, run `jello setup launchers`. For HUD changes, run
`jello hud install`, then stop and start the HUD. An extracted source archive has
no Git upstream. Install a newer archive with
`uv tool install --reinstall --editable PATH`, or use a Git clone for `jello update`.

To remove the HUD, stop it first, then remove `~/Applications/UsageHUD.app` and
`~/Library/LaunchAgents/io.github.priyanshuupadhyay.jello-hud.plist`. To remove the CLI,
run `uv tool uninstall jello`. Account homes and credentials remain in place.

Jello manages Claude and Codex profiles and UsageHUD only. It does not install Herdr
configuration, plugins, worker scripts, runtime skills, or Prime Agent extensions.
Existing installations of those tools are independent of Jello.

## Releases

```sh
jello release
jello release --tag v0.4.0
```

Run this from a clean, committed checkout. The command creates a versioned source
archive and SHA-256 file under `dist/`. It includes the Swift source and excludes
untracked and ignored files. Existing release files are not overwritten. Nothing
is published by this command. `--tag` also checks that the tag matches the project
version and points at the current commit.

The GitHub checks run Python tests on Linux and macOS and Swift tests on macOS.
Pushing a `v*` tag runs these checks, prepares the archive, and creates a draft
GitHub release with both files attached. Review the draft before publishing it.
Set matching versions in `pyproject.toml` and `src/jello/__init__.py` before tagging.

## Development

```sh
uv run --with pytest pytest -q
swift test --package-path apps/UsageHUD
```

Tests use temporary account homes and test credentials. The fetch tests use a local
HTTP server. Swift tests cover usage parsing, history, layout, and the HUD's local
read command. Source files under `docs/decisions` record earlier project decisions;
the scope above describes the current product.

## License

MIT. See `LICENSE`.
