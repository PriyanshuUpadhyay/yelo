"""One tiny launcher per account, written once.

These optional shortcuts bypass the account menu. Each is a file under
`~/.local/bin`, three lines long:

    #!/bin/sh
    # written by jello setup launchers: account sid
    exec env AGENT_PROFILE_LABEL=sid CLAUDE_PROFILE_DIR=... claude "$@"

So `claude-sid` is a command, `codex-thine` is a command, and nothing of jello runs when
one starts. The vendor name is bare, so PATH answers at run time: a `claude` reinstalled
somewhere else needs no launcher rewritten.

Line 2 is the whole ownership rule, and it works in both directions. A regular file
carrying it is jello's: it is rewritten whenever the account's environment changes, and
removed when the account does. Anything else at the same path -- a file without the line, or
any symlink at all -- belongs to somebody else and is reported and left exactly as it is.

A codex home with no label gets no launcher: the name is the command, so there is
nothing to call it.
"""

import os
import shlex
import tempfile

from .profile import core

BIN_RELATIVE = (".local", "bin")
HEADER = "# written by jello setup launchers: account "
KEPT = "kept"
# (cli, the command name's prefix, the vendor binary). The prefix is the CLI's own name, so
LAUNCHERS = (("claude", "claude", "claude"),
             ("codex", "codex", "codex"))


def bin_dir(home):
    return os.path.join(home, *BIN_RELATIVE)


def environment(cli, name, directory, home):
    """Exactly what the removed wrappers exported before exec'ing the binary."""
    if cli == "claude":
        return [("AGENT_PROFILE_LABEL", name),
                ("CLAUDE_PROFILE_DIR", directory),
                ("CLAUDE_SECURESTORAGE_CONFIG_DIR", os.path.join(home, f".claude-{name}"))]
    if cli == "codex":
        return [("CODEX_HOME", directory),
                ("CODEX_CONFIG_PATH", os.path.join(directory, "config.toml"))]
    raise ValueError(f"unsupported provider: {cli}")


def text(cli, name, directory, home, binary):
    """The three lines. `shlex.quote` is what keeps a home directory with a space in it
    from becoming two arguments."""
    assignments = " ".join(f"{key}={shlex.quote(value)}"
                           for key, value in environment(cli, name, directory, home))
    return (f"#!/bin/sh\n{HEADER}{name}\n"
            f"exec env {assignments} {binary} \"$@\"\n")


def rows(home):
    """(path, wanted text) for every account that can have a launcher, in `jello profile`
    order. `core` is the one census: a row it does not report has no launcher here.

    `core` reads the layout of the HOME this process was started with, which is the `home`
    every caller passes, so the two cannot name different accounts.
    """
    found = []
    for cli, prefix, binary in LAUNCHERS:
        for row in core.rows_for(cli):
            name = row["name"]
            if not name or not core.valid_name(name):
                continue
            found.append((os.path.join(bin_dir(home), f"{prefix}-{name}"),
                          text(cli, name, row["dir"], home, binary)))
    return found


def path_for(cli, name, home):
    """Where one account's launcher lives. The command name is the account name, so this is
    the only place the two are joined."""
    prefix = next(prefix for candidate, prefix, _ in LAUNCHERS if candidate == cli)
    return os.path.join(bin_dir(home), f"{prefix}-{name}")


def ours(path):
    """Whether the file at `path` is a launcher jello wrote, by its second line alone.

    A symlink is never ours, whatever it points at. This module writes regular files, so a
    link at one of these names is somebody else's arrangement -- and reading line 2 through
    one would call a link to a real launcher `ours` and let it be replaced by a regular
    file, which orphans whatever it pointed at (R8-F2).

    Read as text and only the first two lines: a foreign file at this path may be a
    binary, and it is not this function's business to read all of it.
    """
    if os.path.islink(path):
        return False
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.readline()
            return handle.readline().startswith(HEADER)
    except OSError:
        return False


def write(path, body):
    """Temp file and rename, so a launcher a shell is about to exec is never half-written."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path),
                                             prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body.encode("utf-8") if isinstance(body, str) else body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o755)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def state(path, body):
    """(verdict, detail) for one launcher, without writing: `unchanged`, `kept` for a file
    that is not ours, else `changed` -- which is also what an absent one earns."""
    from . import setup

    if not os.path.lexists(path):
        return setup.CHANGED, path
    if not ours(path):
        return KEPT, f"not a jello launcher: {path}"
    try:
        with open(path, encoding="utf-8") as handle:
            current = handle.read()
    except OSError:
        return setup.CHANGED, path
    if current != body or not os.access(path, os.X_OK):
        return setup.CHANGED, path
    return setup.UNCHANGED, path


def orphans(home, wanted):
    """Launchers jello wrote whose account is gone.

    The wanted set comes from the account census, so one of our files that is not in it
    names an account nobody has any more -- and it is still an executable command on PATH,
    which is exactly why it has to be found. `ours` is what keeps a symlink out of this
    list, so the only thing ever unlinked here is a regular file jello wrote.
    """
    directory = bin_dir(home)
    known = {path for path, _ in wanted}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(directory, name)
        supported = any(name.startswith(prefix + "-") for _, prefix, _ in LAUNCHERS)
        if supported and path not in known and ours(path):
            found.append(path)
    return found


def foreign(cli, name, home):
    """The launcher path when a file that is not ours already holds it, else None.

    `jello profile create` asks this before it makes the account: a success line that names
    a command belonging to somebody else is a lie, and the account would have to be deleted
    by hand to undo it. `write_one` is gated on the same rule through `state`, so the two
    cannot disagree about who owns a path.
    """
    path = path_for(cli, name, home)
    return path if os.path.lexists(path) and not ours(path) else None


def apply_launchers(home):
    """Both halves of the report name their files: how many were written, and every path
    that was left alone because somebody else owns it."""
    from . import integration, setup

    integration_changes, integration_detail = integration.apply(home)
    wanted = rows(home)
    written, kept = [], []
    for path, body in wanted:
        verdict, _ = state(path, body)
        if verdict == KEPT:
            kept.append(f"kept, not a jello launcher: {path}")
        elif verdict == setup.CHANGED:
            try:
                write(path, body)
            except OSError as error:
                raise setup.SetupError(str(error), path) from None
            written.append(path)
    # An account that is gone leaves a command that still runs, and it is jello's own file
    # to take back. Removing it is the only part of this step that deletes anything, which
    # is why it may only ever touch a file carrying the header.
    removed = []
    for path in orphans(home, wanted):
        try:
            os.unlink(path)
        except OSError as error:
            raise setup.SetupError(str(error), path) from None
        removed.append(f"removed, its account is gone: {path}")
    if not wanted:
        head = f"no account has a launcher to write: {bin_dir(home)}"
    else:
        head = (f"wrote {len(written)} of {len(wanted)} launchers under {bin_dir(home)}"
                if written else f"{len(wanted)} launchers under {bin_dir(home)}")
    detail = "; ".join([head, *removed, *kept, integration_detail])
    return (setup.CHANGED if written or removed or integration_changes else setup.UNCHANGED), detail


def check_launchers(home):
    """The `jello doctor` row, which names its faults rather than counting them.

    A stale launcher is `missing`, not `ok`. Two things earn that word, and both are files
    a shell would run: one carrying an account's old directory, and one whose account is
    gone entirely. A file that is not ours is not a fault -- it is somebody else's command
    at that name -- so it is reported beside the verdict and never counted against it.
    """
    from . import integration, setup

    wanted = rows(home)
    absent, stale, kept = [], [], []
    for path, body in wanted:
        verdict, _ = state(path, body)
        if verdict == KEPT:
            kept.append(f"kept, not a jello launcher: {path}")
        elif verdict == setup.CHANGED:
            (absent if not os.path.lexists(path) else stale).append(path)
    stale += orphans(home, wanted)
    stale += integration.missing(home)
    notes = "; ".join(kept)
    if absent or stale:
        named = "; ".join([*(f"absent: {path}" for path in absent),
                           *(f"stale: {path}" for path in sorted(stale))])
        return setup.MISSING, f"{named}; {notes}" if notes else named
    detail = f"{len(wanted) - len(kept)} launchers under {bin_dir(home)}"
    return setup.OK, f"{detail}; {notes}" if notes else detail


def write_one(cli, name, directory, home):
    """The launcher for one account, written by `jello profile create` right after its
    folder. A foreign file at that path is left alone, exactly as the step would."""
    from . import setup

    binary = next(binary for candidate, _, binary in LAUNCHERS if candidate == cli)
    path = path_for(cli, name, home)
    body = text(cli, name, directory, home, binary)
    if state(path, body)[0] != setup.CHANGED:
        return None
    write(path, body)
    return path
