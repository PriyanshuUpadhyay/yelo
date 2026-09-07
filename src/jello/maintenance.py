"""Update a source installation and prepare source releases."""

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import tomllib

from . import hud


class MaintenanceError(Exception):
    pass


def checkout():
    root = Path(__file__).resolve().parents[2]
    if not (root / "pyproject.toml").is_file() or not (root / ".git").exists():
        raise MaintenanceError("this command needs an editable install from a Git checkout; "
                               "a Homebrew install updates with: brew upgrade jello")
    return root


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args],
                            capture_output=True, text=True)
    if result.returncode:
        raise MaintenanceError(result.stderr.strip() or "git failed")
    return result.stdout.strip()


def require_clean(root):
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise MaintenanceError("commit or save the checkout's changes before continuing")


def run_command(args):
    result = subprocess.run([str(arg) for arg in args])
    if result.returncode:
        raise MaintenanceError(f"{args[0]} failed with exit code {result.returncode}")


def update(root, *, check=False, with_hud=False):
    require_clean(root)
    # Do not guess a remote or switch a developer's branch.
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    uv = shutil.which("uv")
    if not uv:
        raise MaintenanceError("uv is required to update Jello")
    launcher = hud.launcher_path()
    home = os.path.expanduser("~")
    if with_hud and not os.path.isfile(hud.binary_path(home)):
        raise MaintenanceError("HUD is not installed; run jello hud install first")
    print(f"Source: {root}\nUpstream: {upstream}", flush=True)
    if check:
        print("Ready to fast-forward this branch, reinstall Jello, and apply setup.")
        if with_hud:
            print("The HUD will also be rebuilt. A running HUD will be restarted.")
        return
    was_running = with_hud and hud.job_state()[1] is not None
    run_command(["git", "-C", root, "pull", "--ff-only"])
    run_command([uv, "tool", "install", "--reinstall", "--editable", root])
    # Use the updated CLI for setup and HUD work; this process still has old modules loaded.
    run_command([launcher, "setup"])
    if with_hud:
        run_command([launcher, "hud", "install"])
        if was_running:
            run_command([launcher, "hud", "stop"])
            run_command([launcher, "hud", "start"])
    print("Jello updated.")


def release(root, output, tag=None):
    require_clean(root)
    metadata = tomllib.loads(git(root, "show", "HEAD:pyproject.toml"))
    version = metadata["project"]["version"]
    if metadata["project"]["name"] != "jello" or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise MaintenanceError("release requires a Jello version in MAJOR.MINOR.PATCH form")
    module = git(root, "show", "HEAD:src/jello/__init__.py")
    declared = re.search(r'''^__version__\s*=\s*(["'])([0-9.]+)\1\s*$''', module, re.MULTILINE)
    if not declared or declared[2] != version:
        raise MaintenanceError("pyproject.toml and src/jello/__init__.py versions must match")
    if tag is not None:
        if tag != f"v{version}" or git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}") != git(root, "rev-parse", "HEAD"):
            raise MaintenanceError("release tag must match the version and current commit")
    output = Path(output).resolve()
    name = f"jello-{version}"
    archive = output / f"{name}.tar.gz"
    checksum = output / f"{name}.sha256"
    if archive.exists() or checksum.exists():
        raise MaintenanceError("release files already exist; choose a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="jello-release-") as staged:
        staged_archive = Path(staged) / archive.name
        run_command(["git", "-C", root, "archive", "--format=tar.gz",
                     f"--prefix={name}/", f"--output={staged_archive}", "HEAD"])
        data = staged_archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        # Exclusive creation prevents a concurrent release from being overwritten.
        with archive.open("xb") as handle:
            handle.write(data)
        with checksum.open("x") as handle:
            handle.write(f"{digest}  {archive.name}\n")
    print(f"Source archive: {archive}\nSHA-256: {checksum}\nNothing was published.")


def run(args):
    from .cli import fail

    try:
        root = checkout()
        if args.command == "update":
            update(root, check=args.check, with_hud=args.hud)
        else:
            release(root, args.output, args.tag)
    except (MaintenanceError, OSError, KeyError, tomllib.TOMLDecodeError) as error:
        return fail(args.command, str(error))
    return 0


def register(subparsers):
    parser = subparsers.add_parser("update", help="update this Jello checkout and apply setup")
    parser.add_argument("--check", action="store_true", help="check local prerequisites without changing files or fetching updates")
    parser.add_argument("--hud", action="store_true", help="also rebuild the installed HUD and restart it if running")
    parser.set_defaults(run=run)
    parser = subparsers.add_parser("release", help="prepare a source archive and checksum; does not publish")
    parser.add_argument("--output", default="dist", help="output directory (default: dist)")
    parser.add_argument("--tag", help="require this version tag to point at HEAD")
    parser.set_defaults(run=run)
