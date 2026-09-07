"""`profile create` for the Claude and Codex account homes.

Ported from the three zsh creators: `_cprofile_manage create` (.aliases:58-108),
`_codexprofile_manage create` (.zshrc:138-184). Messages, exit codes, directory modes, and the copied and linked
files are the shell's; only the order of the claude profile-root mkdir changed, so a
refusal or a cancelled prompt now creates nothing at all.

Every refusal is raised before the confirmation prompt, and the prompt runs before the
first write, so a create either happens whole or leaves the tree as it was.

The account's launcher is written last, so `claude profile create sid` leaves a `claude-sid`
command behind and nobody has to remember `yelo setup launchers` (yelo.launchers). Its
path is checked with the other refusals, before anything is made: the success line names
that command, so an account cannot be created into one somebody else owns.
"""

import os
import shutil
import sys

from . import core
from .. import launchers

# The label the messages carry, which is the command the user typed, not the --cli value.
LABELS = {"claude": "claude", "codex": "codex"}
DISPLAY = {"claude": "Claude", "codex": "Codex"}
USAGE = {
    "claude": "usage: yelo profile create --cli claude NAME [--email ADDR] [--yes]",
    "codex": "usage: yelo profile create --cli codex NAME [--yes]",
}
# Companions shared with the base Codex home. Missing sources are skipped.
CODEX_LINKS = ("hooks.json", "AGENTS.md")
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
    raise ValueError(f"unsupported provider: {cli}")


def profile_dir(cli, name, home):
    root = profile_root(cli, home)
    if cli == "claude":
        return os.path.join(root, name)
    if cli == "codex":
        return os.path.join(root, core.CODEX_PREFIX + name)
    raise ValueError(f"unsupported provider: {cli}")


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
    return f"Created profile '{name}'. Sign in with: claude-{name} auth login"


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
    return f"Created profile '{name}'. Sign in with: codex-{name} login"


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
    # Codex homes sit directly under HOME; only Claude has a shared profile root.
    if cli == "claude" and os.path.islink(root):
        raise CreateError(f"claude: profile root must not be a symlink: {root}", 1)
    directory = profile_dir(cli, name, home)
    check_free(cli, name, directory)
    # A free directory name is not a free command either. The success line below names
    # `<cli>-<name>`, so an account whose command already belongs to somebody else would be
    # a promise nobody can keep -- and undoing it would mean deleting the home by hand.
    taken = launchers.foreign(cli, name, home)
    if taken is not None:
        raise CreateError(
            f"{label}: '{name}' would need a command somebody else owns: {taken}", 1)
    if not yes:
        confirm(cli, name, directory)
    if cli == "claude":
        message = write_claude(name, directory, home, email)
    elif cli == "codex":
        message = write_codex(name, directory, home)
    # The home exists from here on, so a launcher that cannot be written is reported as
    # exactly that rather than swallowed: `yelo setup launchers` finishes the job.
    try:
        path = launchers.write_one(cli, name, directory, home)
    except OSError as error:
        raise CreateError(
            f"{label}: created {directory}, but its launcher could not be written: "
            f"{error}. Run `yelo setup launchers` to finish.", 1) from None
    return f"{message}\nWrote {path}." if path else message


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
