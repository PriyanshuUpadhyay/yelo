"""`jello resume`: the session-id to profile-label map, and its 30-day prune.

`map-session` is a Claude Code hook (SessionStart and UserPromptSubmit). Herdr's restore
replays a bare `claude --resume <sid>` carrying neither `--profile` nor the pane env, so the
map is the only thing that can name the account that session belongs to. UserPromptSubmit
runs it too: a session id survives resume and compact, so a long-lived pane may never fire
SessionStart again, and its entry would age out of the prune below.

Ported from ~/dotfiles/home/.claude/hooks/session-profile-map.sh (map-session) and
~/dotfiles/home/.claude/scripts/claude-housekeeping.sh:26-37 (prune). The one deliberate
change is board decision C1: the payload is parsed with json.loads and only its top-level
keys are read, which is at least as strict as the hook's first-occurrence grep. Everything
else -- the charset checks, the symlink refusals, the byte compare, the atomic rename, the
file bytes and mode -- is the hook's.

Two contracts hold on every path of map-session: stdout stays empty, because hook stdout is
injected into the session context, and the exit code is 0, because a hook that fails would
fail the session. An unexpected payload or a filesystem error skips the write and nothing else.
"""

import json
import os
import re
import sys
import tempfile
import time

from .profile import core

# The hook's `case "$sid" in *[!A-Za-z0-9-]*`, as one anchored pattern.
SID_RE = re.compile(r"[A-Za-z0-9-]+\Z")
PRUNE_AGE_SECONDS = 30 * 86400
SESSION_JSON = os.path.join(".config", "herdr", "session.json")


def profiles_root(home):
    return os.path.join(home, ".claude", ".profiles")


def map_dir(home):
    # The leading dot in .session-map is load-bearing: `profile list` and the Usage HUD
    # treat every directory under ~/.claude/.profiles as an account.
    return os.path.join(profiles_root(home), ".session-map")


def session_json_path(home):
    return os.path.join(home, SESSION_JSON)


def linked_state_root(home):
    """True when the profile root or the map directory is a symlink.

    Both commands refuse then, and for the same reason: a symlinked root is somebody
    else's arrangement, and following it would write -- or delete -- outside the tree this
    tool owns. `map_session` has always refused; `prune` checked only the map directory
    until review finding F1, so a symlinked `.profiles` let it unlink a file outside HOME.
    """
    return os.path.islink(profiles_root(home)) or os.path.islink(map_dir(home))


def payload_string(payload, key):
    """Only a top-level string counts, the way the hook's grep only matched a string
    value: `"agent_id": null` is not an agent id there and is not one here."""
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def read_payload(stdin):
    """The payload as a dict, or None for every class the hook skips: a TTY, an empty
    body, text that is not JSON, and JSON that is not an object."""
    try:
        if stdin.isatty():
            return None
        text = stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def map_session(stdin, env, home):
    """Write or refresh one map entry. Returns the entry path when it wrote or touched
    one, else None. Never raises, never prints."""
    payload = read_payload(stdin)
    if payload is None:
        return None
    # Subagent and nested `claude -p` sessions are never restored by herdr, which skips
    # them on the same field (herdr-agent-state.sh), so they are not mapped.
    if payload_string(payload, "agent_id"):
        return None
    sid = payload_string(payload, "session_id")
    if not SID_RE.match(sid):
        return None
    label = env.get("AGENT_PROFILE_LABEL", "")
    if not core.valid_name(label):
        return None
    directory = map_dir(home)
    if linked_state_root(home):
        return None
    previous_umask = os.umask(0o077)
    try:
        return write_entry(directory, sid, label)
    except OSError:
        return None
    finally:
        os.umask(previous_umask)


def write_entry(directory, sid, label):
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = os.path.join(directory, sid)
    wanted = (label + "\n").encode()
    # Hot path -- this runs on every prompt: an entry whose whole content is already
    # exactly the label only needs its mtime refreshed, which is also the liveness signal
    # the prune reads. A whole-file byte compare holds for a non-ASCII label and for
    # embedded NULs, which a shell read would truncate at.
    if os.path.isfile(path) and not os.path.islink(path):
        try:
            with open(path, "rb") as handle:
                if handle.read() == wanted:
                    os.utime(path, None)
                    return path
        except OSError:
            pass
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(wanted)
        # Replaces a stale entry, a wrong-content entry, and a symlinked entry alike,
        # and no reader ever sees a half-written label.
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def prune(home, session_json=None, now=None):
    """Delete entries older than 30 days that Herdr no longer lists.

    Herdr's restore may replay a session idle for weeks, and losing its entry means the
    wrapper's strict "--profile NAME is required" error, so a live session id is never
    pruned however old its entry is. The listing is read as text, the way the housekeeping
    script greps it: a missing or unreadable file means nothing is live.
    """
    directory = map_dir(home)
    if session_json is None:
        session_json = session_json_path(home)
    # The same refusal map_session makes, before anything is listed or unlinked (F1).
    if linked_state_root(home) or not os.path.isdir(directory):
        return 0
    try:
        with open(session_json, encoding="utf-8", errors="replace") as handle:
            live = handle.read()
    except OSError:
        live = ""
    cutoff = (time.time() if now is None else now) - PRUNE_AGE_SECONDS
    removed = 0
    for name in sorted(os.listdir(directory)):
        path = os.path.join(directory, name)
        try:
            # `find -type f` reports a symlink as a link, not a file, so it is left alone.
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if os.stat(path).st_mtime > cutoff:
                continue
        except OSError:
            continue
        if name in live:
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        removed += 1
    return removed


def run_map_session(args):
    try:
        map_session(sys.stdin, os.environ, os.path.expanduser("~"))
    except Exception:  # noqa: BLE001 - fail-open: a hook may never fail the session
        pass
    return 0


def run_prune(args):
    home = os.path.expanduser("~")
    try:
        removed = prune(home, args.session_json)
    except OSError as error:
        from .cli import fail

        return fail("resume prune", str(error), map_dir(home))
    print(f"resume prune: removed {removed} entr{'y' if removed == 1 else 'ies'}")
    return 0


def register(subparsers):
    parser = subparsers.add_parser(
        "resume",
        help="the session-id to profile map the claude wrapper reads",
        description=__doc__.splitlines()[0],
    )
    group = parser.add_subparsers(dest="resume_command", required=True)
    mapping = group.add_parser(
        "map-session", help="Claude hook: record this session's profile label"
    )
    mapping.set_defaults(handler=run_map_session)
    pruning = group.add_parser("prune", help="drop entries older than 30 days")
    pruning.add_argument("--session-json", default=None)
    pruning.set_defaults(handler=run_prune)
    parser.set_defaults(run=run)
    return parser


def run(args):
    return args.handler(args)
