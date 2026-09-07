"""Install the profile wrappers and their independent Python archive."""

import io
import os
from pathlib import Path
import shlex
import zipfile

from . import legacy, launchers

SHELL_HEADER = "# Installed by jello profile integration.\n"
ARCHIVE_HEADER = b"#!/usr/bin/env python3\n# Installed by jello profile integration.\n"
MODULES = ("profile/core.py", "usage/snapshot.py", "usage/fetch.py", "usage/codex.py", "profile_runtime.py")


def runtime_path(home):
    return str(Path(legacy.shell_path(home)).with_name("profiles.pyz"))


def archive():
    # Copy only standard-library modules. No link to this checkout or the tool's virtualenv.
    root = Path(__file__).parent
    buffer = io.BytesIO(ARCHIVE_HEADER)
    buffer.seek(0, 2)
    files = {"__main__.py": b"from profiles.profile_runtime import main\nraise SystemExit(main())\n"}
    files.update({f"profiles/{path}": (root / path).read_bytes() for path in MODULES})
    for package in ("profiles", "profiles/profile", "profiles/usage"):
        files[f"{package}/__init__.py"] = b""
    with zipfile.ZipFile(buffer, "w") as bundle:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)  # Fixed timestamp makes setup idempotent.
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, content)
    return buffer.getvalue()


def shell_text(home):
    template = Path(__file__).with_name("profile_shell.zsh").read_text()
    return template.replace("__RUNTIME_PATH__", shlex.quote(runtime_path(home)))


def owned(path, header):
    if os.path.islink(path):
        return False
    try:
        with open(path, "rb") as handle:
            return handle.read(len(header)) == header
    except OSError:
        return False


def shell_owned(path):
    if owned(path, SHELL_HEADER.encode()):
        return True
    if os.path.islink(path):
        return False
    try:
        return Path(path).read_text() in (legacy.old_shell(), legacy.STANDALONE_SHELL)
    except (OSError, UnicodeError):
        return False


def targets(home):
    return [(runtime_path(home), archive()), (legacy.shell_path(home), shell_text(home).encode())]


def source_line(home):
    path = legacy.shell_path(home)
    if path == os.path.join(home, ".config", "jello", "shell.sh"):
        return 'source "$HOME/.config/jello/shell.sh"'
    return "source " + shlex.quote(path)


def apply(home):
    from . import setup

    wanted = targets(home)
    for path, _ in wanted:
        ours = shell_owned(path) if path == legacy.shell_path(home) else owned(path, ARCHIVE_HEADER)
        if os.path.lexists(path) and not ours:
            raise setup.SetupError("profile integration target belongs to another owner", path)
    changed = []
    for path, body in wanted:
        current = Path(path).read_bytes() if os.path.exists(path) else None
        if current == body:
            continue
        if current is not None and path == legacy.shell_path(home):
            backup = path + ".before-profile-integration"
            if not os.path.lexists(backup):
                with open(backup, "xb") as handle:
                    handle.write(current)
        launchers.write(path, body)
        changed.append(path)
    rc = Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    line = source_line(home)
    body = rc.read_text() if rc.exists() else ""
    if line not in body.splitlines():
        if rc.is_symlink():
            return changed, f"add to {rc}: {line}"
        with rc.open("a") as handle:
            handle.write("\n" + line + "\n")
        changed.append(str(rc))
    return changed, "profile menu and automatic selection installed; open a new terminal"


def missing(home):
    result = []
    for path, body in targets(home):
        try:
            current = Path(path).read_bytes() if not os.path.islink(path) else None
        except OSError:
            current = None
        if current != body:
            result.append(path)
    rc = Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    try:
        if source_line(home) not in rc.read_text().splitlines():
            result.append(str(rc))
    except OSError:
        result.append(str(rc))
    return result
