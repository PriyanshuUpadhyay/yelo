"""`profile create` for the claude, codex, and Prime Agent account homes.

Ported from the three zsh creators: `_cprofile_manage create` (.aliases:58-108),
`_codexprofile_manage create` (.zshrc:138-184), and `_prime_profile_manage create`
(.zshrc:357-392). Messages, exit codes, directory modes, and the copied and linked
files are the shell's; only the order of the claude profile-root mkdir changed, so a
refusal or a cancelled prompt now creates nothing at all.

Every refusal is raised before the confirmation prompt, and the prompt runs before the
first write, so a create either happens whole or leaves the tree as it was.
"""

import os
import shutil
import sys

from . import core

# The label the messages carry, which is the command the user typed, not the --cli value.
LABELS = {"claude": "claude", "codex": "codex", "prime": "prime-agent"}
DISPLAY = {"claude": "Claude", "codex": "Codex", "prime": "Prime Agent"}
USAGE = {
    "claude": "usage: claude profile create NAME [--email ADDR] [--yes]",
    "codex": "usage: codex profile create NAME [--yes]",
    "prime": "usage: prime-agent profile create NAME [--yes]",
}
# Companions the codex creator links from the base home, and the ones prime links from
# its base agent home. A missing source is skipped, never an error.
CODEX_LINKS = ("hooks.json", "AGENTS.md")
PRIME_LINKS = ("settings.json", "bin", "extensions", "skills", "harness")
CONFIRM_YES = ("y", "Y", "yes", "YES", "Yes")


class CreateError(Exception):
    """A refusal that carries the shell's exit code; the message is already prefixed."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


def profile_root(cli, home):
    if cli == "claude":
        return os.path.join(home, ".claude", ".profiles")
    if cli == "codex":
        return home
    return os.path.join(home, ".prime")


def profile_dir(cli, name, home):
    root = profile_root(cli, home)
    if cli == "claude":
        return os.path.join(root, name)
    if cli == "codex":
        return os.path.join(root, core.CODEX_PREFIX + name)
    return os.path.join(root, core.PRIME_PREFIX + name)


def mkdir_700(path):
    """`mkdir -m 700`: the mode is explicit, so the umask cannot widen it."""
    os.mkdir(path)
    os.chmod(path, 0o700)


def mkdir_p_700(path):
    """`mkdir -m 700 -p`: an existing directory keeps whatever mode it has."""
    if os.path.isdir(path):
        return
    os.makedirs(path, exist_ok=True)
    os.chmod(path, 0o700)


def write_600(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o600)


def link_target(path):
    """`[ -e ]` then `[ -L ] && readlink -f`: a broken link is skipped, a live one is
    followed, so the new home never depends on a link that may be re-pointed."""
    if not os.path.exists(path):
        return None
    return os.path.realpath(path) if os.path.islink(path) else path


def check_free(cli, name, directory):
    label = LABELS[cli]
    if os.path.lexists(directory):
        raise CreateError(f"{label}: profile already exists: {name}", 1)
    # A free directory name is not a free identity: a retired name still resolves through
    # the alias map, and taking it would silently redirect every launch that still uses it.
    try:
        core.resolve(cli, core.rows_for(cli), name)
    except core.ResolveError as error:
        if error.code == 2:
            raise CreateError(
                f"{label}: '{name}' matches several existing accounts", 1
            ) from None
        return
    raise CreateError(f"{label}: '{name}' already identifies an existing account", 1)


def confirm(cli, name, directory):
    label = LABELS[cli]
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise CreateError(
            f"{label}: confirmation required; rerun with --yes in a non-interactive shell", 2
        )
    sys.stderr.write(
        f"Create {DISPLAY[cli]} profile '{name}' at {directory}? [y/N] "
    )
    sys.stderr.flush()
    reply = sys.stdin.readline().rstrip("\n")
    if reply not in CONFIRM_YES:
        raise CreateError("Cancelled.", 1)


def write_claude(name, directory, home, email):
    mkdir_p_700(profile_root("claude", home))
    mkdir_700(directory)
    if email:
        with open(os.path.join(directory, "email"), "w", encoding="utf-8") as handle:
            handle.write(email + "\n")
    return f"Created profile '{name}'. Sign in with: claude --profile {name} auth login"


def write_codex(name, directory, home):
    base = os.path.join(home, core.CODEX_BASE)
    mkdir_700(directory)
    # sessions/ is the usage HUD's discovery marker for an extra Codex install.
    mkdir_700(os.path.join(directory, "sessions"))
    config = os.path.join(base, "config.toml")
    if os.path.isfile(config):
        shutil.copyfile(config, os.path.join(directory, "config.toml"))
        os.chmod(os.path.join(directory, "config.toml"), 0o600)
    for item in CODEX_LINKS:
        target = link_target(os.path.join(base, item))
        if target:
            os.symlink(target, os.path.join(directory, item))
    return f"Created profile '{name}'. Sign in with: codex --profile {name} login"


def write_prime(name, directory, home):
    root = profile_root("prime", home)
    base = os.path.join(root, core.PRIME_BASE)
    mkdir_p_700(root)
    mkdir_700(directory)
    try:
        write_600(os.path.join(directory, "auth.json"), "{}\n")
        for item in PRIME_LINKS:
            source = os.path.join(base, item)
            if os.path.exists(source):
                os.symlink(source, os.path.join(directory, item))
    except OSError as error:
        # A half-made home would resolve as a real account, so it is removed whole.
        shutil.rmtree(directory, ignore_errors=True)
        raise CreateError(f"prime-agent: could not create {directory}: {error}", 1) from None
    return (f"Created profile '{name}'. Start it with 'prime-agent --profile {name}', "
            "then run /login.")


def create(cli, name, home=None, email=None, yes=False):
    """Refusals, then the prompt, then the writes. Returns the success line."""
    home = core.HOME if home is None else home
    label = LABELS[cli]
    if name is None:
        raise CreateError(USAGE[cli], 2)
    if email is not None and cli != "claude":
        raise CreateError(USAGE[cli], 2)
    if email == "":
        raise CreateError(f"{label}: --email requires an address", 2)
    if not core.valid_name(name):
        raise CreateError(f"{label}: invalid profile name: {name}", 2)
    root = profile_root(cli, home)
    # Only the claude creator refuses a symlinked root today; codex writes beside $HOME
    # and prime creates its own root, so neither has the check to port.
    if cli == "claude" and os.path.islink(root):
        raise CreateError(f"claude: profile root must not be a symlink: {root}", 1)
    directory = profile_dir(cli, name, home)
    check_free(cli, name, directory)
    if not yes:
        confirm(cli, name, directory)
    if cli == "claude":
        return write_claude(name, directory, home, email)
    if cli == "codex":
        return write_codex(name, directory, home)
    return write_prime(name, directory, home)


def command_create(args):
    try:
        print(create(args.cli, args.name, email=args.email, yes=args.yes))
    except CreateError as error:
        print(str(error), file=sys.stderr)
        return error.code
    except OSError as error:
        print(f"{LABELS[args.cli]}: {error}", file=sys.stderr)
        return 1
    return 0
