"""`jello setup [step ...]`: install the three targets, idempotently.

Three steps, each one target:

  shell         the rendered init script under ~/.cache/jello, plus the one line to add
                to ~/.zshrc -- printed, never written; .zshrc stays the user's file.
  claude-hooks  one hook group tagged `jello` under SessionStart and UserPromptSubmit in
                ~/.claude/settings.json, every other key and every other group untouched.
  profiles      ~/.claude/.profiles, mode 700.

Every step answers two questions with the same code: `apply(home)` makes the target so and
says whether it changed, `check(home)` re-derives the verdict from the filesystem alone and
never writes -- that is what `jello doctor` calls, and why doctor cannot be fooled by
setup's own bookkeeping.

A target owned by dotfiles is never overwritten. Ownership is a fact about the filesystem:
a symlink whose realpath lands inside ~/dotfiles, or a hook command that resolves in there
(today ~/.claude/hooks/session-profile-map.sh, the predecessor of `jello resume
map-session`). JELLO_DOTFILES_ROOT relocates that root for tests.

The settings.json merge is the pattern of ~/dotfiles/home/.claude/scripts/
sync-claude-settings.py: parse, mutate only what this tool owns, write by temp file and
rename. Every other key keeps its value and its position.
"""

import json
import os
import stat
import tempfile

from . import __version__, shell

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit")
HOOK_COMMAND = "jello resume map-session"
# The predecessor this step replaces; a settings.json still wired to it is dotfiles-owned.
DOTFILES_HOOK = "session-profile-map.sh"
SETTINGS_RELATIVE = os.path.join(".claude", "settings.json")
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


def settings_path(home):
    return os.path.join(home, SETTINGS_RELATIVE)


def profiles_path(home):
    return os.path.join(home, PROFILES_RELATIVE)


# --- shell -----------------------------------------------------------------------------

def zshrc_state(home):
    """Whether ~/.zshrc already carries the eval line. Read-only, always."""
    try:
        with open(os.path.join(home, ".zshrc"), encoding="utf-8", errors="replace") as handle:
            return shell.ZSHRC_LINE in handle.read()
    except OSError:
        return False


def shell_detail(home):
    if zshrc_state(home):
        return f"{shell.cache_path(home)}; ~/.zshrc already has: {shell.ZSHRC_LINE}"
    return f"{shell.cache_path(home)}; add to ~/.zshrc: {shell.ZSHRC_LINE}"


def apply_shell(home):
    if shell.read_cache(__version__, home) is not None:
        return UNCHANGED, shell_detail(home)
    try:
        shell.write_cache(shell.render_zsh(__version__), home)
    except OSError as error:
        raise SetupError(str(error), shell.cache_path(home)) from None
    return CHANGED, shell_detail(home)


def check_shell(home):
    if shell.read_cache(__version__, home) is None:
        return MISSING, f"no cache for jello {__version__}: {shell.cache_path(home)}"
    return OK, shell_detail(home)


# --- claude-hooks ----------------------------------------------------------------------

def hook_group():
    """The group setup owns. No `matcher` key, matching every neighbouring group in a live
    settings.json; the literal `jello` in the command is the tag that finds it again."""
    return {"hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def group_commands(group):
    if not isinstance(group, dict):
        return []
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return []
    return [
        entry["command"]
        for entry in hooks
        if isinstance(entry, dict) and isinstance(entry.get("command"), str)
    ]


def first_word(command):
    parts = command.split()
    return parts[0] if parts else ""


def is_jello_group(group):
    for command in group_commands(group):
        word = first_word(command)
        if word == "jello" or word.endswith("/jello"):
            return True
    return False


def is_dotfiles_group(group):
    """A group still wired to the predecessor hook. Only that command counts: the other
    dotfiles hooks in these events (housekeeping, guards) stay in dotfiles by design, and on
    this machine every one of them is a stow symlink into it."""
    for command in group_commands(group):
        word = os.path.expanduser(os.path.expandvars(first_word(command)))
        if os.path.basename(word) == DOTFILES_HOOK:
            return True
    return False


def settings_link_state(home):
    """The verdict for the settings target itself, before a byte of it is read.

    A symlink at ~/.claude/settings.json is somebody's arrangement: dotfiles' one is left
    alone, and any other is refused. Without this the atomic write would `os.replace` the
    link with a regular file and orphan whatever it pointed at, which is review finding F2.
    None means the path is not a symlink and the content decides.
    """
    path = settings_path(home)
    if not os.path.islink(path):
        return None
    return OWNED if into_dotfiles(path) else MISSING


def load_settings(home):
    """The parsed settings, or {} when the file is absent. Anything unparseable is a
    refusal, not an empty file: overwriting it would drop the user's whole configuration."""
    path = settings_path(home)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise SetupError(str(error), path) from None
    if not isinstance(data, dict):
        raise SetupError("settings.json is not a JSON object", path)
    return data


def write_settings(home, data):
    path = settings_path(home)
    # The invariant lives with the one call that could break it: os.replace over a symlink
    # turns the link into a file. Callers decide *which* verdict a symlink earns; this only
    # makes sure no path through the module can write over one.
    if os.path.islink(path):
        raise SetupError("settings.json is a symlink; refusing to replace it", path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".settings.json.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o644
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def dotfiles_owned_events(data):
    hooks = data.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    owned = []
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if isinstance(groups, list) and any(is_dotfiles_group(group) for group in groups):
            owned.append(event)
    return owned


def apply_claude_hooks(home):
    path = settings_path(home)
    link = settings_link_state(home)
    if link == OWNED:
        return OWNED, f"symlink into {dotfiles_root()}: {path}"
    if link == MISSING:
        raise SetupError("settings.json is a symlink outside ~/dotfiles", path)
    data = load_settings(home)
    owned = dotfiles_owned_events(data)
    if owned:
        return OWNED, f"{DOTFILES_HOOK} still owns {', '.join(owned)} in {path}"
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("settings.json 'hooks' is not a JSON object", path)
    wanted = hook_group()
    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise SetupError(f"settings.json 'hooks.{event}' is not a list", path)
        tagged = [index for index, group in enumerate(groups) if is_jello_group(group)]
        if not tagged:
            groups.append(wanted)
            changed = True
            continue
        if groups[tagged[0]] != wanted:
            groups[tagged[0]] = wanted
            changed = True
        # A second tagged group would fire the hook twice; only the first survives.
        for index in reversed(tagged[1:]):
            del groups[index]
            changed = True
    if changed:
        write_settings(home, data)
    return (CHANGED if changed else UNCHANGED), path


def check_claude_hooks(home):
    path = settings_path(home)
    link = settings_link_state(home)
    if link == OWNED:
        return OWNED, f"symlink into {dotfiles_root()}: {path}"
    if link == MISSING:
        return MISSING, f"symlink outside ~/dotfiles: {path}"
    try:
        data = load_settings(home)
    except SetupError as error:
        return MISSING, str(error)
    owned = dotfiles_owned_events(data)
    if owned:
        return OWNED, f"{DOTFILES_HOOK} still owns {', '.join(owned)} in {path}"
    hooks = data.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    absent = [
        event
        for event in HOOK_EVENTS
        if not any(is_jello_group(group) for group in hooks.get(event) or [])
    ]
    if absent:
        return MISSING, f"no jello hook group under {', '.join(absent)} in {path}"
    return OK, path


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
    Step("shell", apply_shell, check_shell),
    Step("claude-hooks", apply_claude_hooks, check_claude_hooks),
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
        help="install the shell init, the Claude hook group, and the profile root",
        description=__doc__.splitlines()[0],
    )
    # Validated in run(), not by argparse, so an unknown step gets the one error shape.
    parser.add_argument("steps", nargs="*", metavar="step", default=[],
                        help=f"one or more of: {', '.join(STEP_NAMES)} (default: all)")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(run=run)
    return parser
