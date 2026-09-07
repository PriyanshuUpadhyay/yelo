"""`jello setup [step ...]`: install profiles and shell integration, idempotently.

Two setup steps:

  launchers  standalone zsh integration and selector, plus one executable per account under ~/.local/bin -- `claude-sid`, `codex-thine`,
             each three lines that exec the vendor binary with that
             account's environment. See jello.launchers.
  profiles   ~/.claude/.profiles, mode 700.

Every step answers two questions with the same code: `apply(home)` makes the target so and
says whether it changed, `check(home)` re-derives the verdict from the filesystem alone and
never writes -- that is what `jello doctor` calls, and why doctor cannot be fooled by
setup's own bookkeeping.

A target owned by dotfiles is never overwritten. Ownership is a fact about the filesystem:
a symlink whose realpath lands inside ~/dotfiles. JELLO_DOTFILES_ROOT relocates that root
for tests.

"""

import json
import os

from . import launchers

PROFILES_RELATIVE = os.path.join(".claude", ".profiles")
OK, MISSING, OWNED = "ok", "missing", "owned-by-dotfiles"
CHANGED, UNCHANGED = "changed", "unchanged"


class SetupError(Exception):
    """A refusal that becomes the CLI error shape; `path` is the target it names."""

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


class Step:
    """One installable target. `apply` returns (changed|unchanged|owned-by-dotfiles,
    detail); `check` returns (ok|missing|owned-by-dotfiles, detail)."""

    def __init__(self, name, apply_function, check_function):
        self.name = name
        self._apply = apply_function
        self._check = check_function

    def apply(self, home):
        return self._apply(home)

    def check(self, home):
        return self._check(home)


def dotfiles_root():
    root = os.environ.get("JELLO_DOTFILES_ROOT") or os.path.join(
        os.path.expanduser("~"), "dotfiles"
    )
    return os.path.realpath(root)


def into_dotfiles(path):
    root = dotfiles_root()
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def profiles_path(home):
    return os.path.join(home, PROFILES_RELATIVE)


# --- profiles --------------------------------------------------------------------------

def apply_profiles(home):
    path = profiles_path(home)
    if os.path.islink(path):
        if into_dotfiles(path):
            return OWNED, f"symlink into {dotfiles_root()}: {path}"
        raise SetupError("profile root is a symlink outside ~/dotfiles", path)
    if os.path.isdir(path):
        return UNCHANGED, path
    if os.path.exists(path):
        raise SetupError("profile root exists and is not a directory", path)
    try:
        os.makedirs(path, exist_ok=True)
        os.chmod(path, 0o700)
    except OSError as error:
        raise SetupError(str(error), path) from None
    return CHANGED, path


def check_profiles(home):
    path = profiles_path(home)
    if os.path.islink(path):
        if into_dotfiles(path):
            return OWNED, f"symlink into {dotfiles_root()}: {path}"
        return MISSING, f"symlink outside ~/dotfiles: {path}"
    if os.path.isdir(path):
        return OK, path
    if os.path.exists(path):
        return MISSING, f"not a directory: {path}"
    return MISSING, f"absent: {path}"


STEPS = (
    Step("launchers", launchers.apply_launchers, launchers.check_launchers),
    Step("profiles", apply_profiles, check_profiles),
)
STEP_NAMES = tuple(step.name for step in STEPS)


def emit(rows, as_json):
    if as_json:
        print(json.dumps(
            [{"step": name, "result": result, "detail": detail} for name, result, detail in rows],
            indent=2,
        ))
        return
    for name, result, detail in rows:
        print(f"{name}\t{result}\t{detail}")


def run(args):
    from .cli import fail

    home = os.path.expanduser("~")
    selected = [step for step in STEPS if not args.steps or step.name in args.steps]
    unknown = sorted(set(args.steps) - set(STEP_NAMES))
    if unknown:
        return fail("setup", f"unknown step: {', '.join(unknown)}")
    rows = []
    for step in selected:
        try:
            result, detail = step.apply(home)
        except SetupError as error:
            return fail("setup", str(error), error.path)
        except OSError as error:
            return fail("setup", str(error))
        rows.append((step.name, result, detail))
    emit(rows, args.json)
    return 0


def register(subparsers):
    parser = subparsers.add_parser(
        "setup",
        help="install profile selection, account launchers, and the profile root",
        description=__doc__.splitlines()[0],
    )
    # Validated in run(), not by argparse, so an unknown step gets the one error shape.
    parser.add_argument("steps", nargs="*", metavar="step", default=[],
                        help=f"one or more of: {', '.join(STEP_NAMES)} (default: all)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(run=run)
    return parser
