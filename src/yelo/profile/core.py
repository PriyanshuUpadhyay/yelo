"""One identity resolver for the claude and codex account profiles.

A row is one account: canonical name, home dir, email, plan (codex), aliases.
A query matches by exact name, alias, exact email, or a unique case-insensitive
substring of either. There is deliberately NO default and NO fallback profile —
an unmatched (exit 1) or ambiguous (exit 2) query is an error, because silently
landing on the wrong account spends the wrong subscription.

Subcommands (wired by yelo.profile.commands as `yelo profile ...`):
  list    --cli claude|codex [--usage] [--json]   all rows, aligned table or JSON
  menu    --cli claude|codex                      picker rows: index/name/dir/display (TAB)
  resolve --cli claude|codex QUERY [--json]       name<TAB>dir, or JSON
  pick    --cli claude|codex [--json]             the account about to waste the most usage
  sessions --cli codex [--all] [--limit N] [--json]     recent sessions, newest first, each
                                                        with the account that owns it

--usage joins the Usage HUD rows, which `yelo.profile.commands` reads in process
from `yelo.usage.snapshot` and passes in, falling back to each account's own usage
cache; it costs a pass over the cache files, so the resolve hot path never asks for it.

AGENT_PROFILES_CLAUDE_ROOT, AGENT_PROFILES_CODEX_GLOB_ROOT,
and AGENT_PROFILES_SECURITY_BIN relocate the roots and
the Keychain probe for tests only.
"""

import base64
import getpass
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
KEYCHAIN_TIMEOUT_SECONDS = 10
CODEX_BASE = ".codex"
CODEX_PREFIX = ".codex-"
# Same rule the shell creators enforce, so a name can never be a path fragment, an option, or
# carry the tab this tool's own output uses as a field separator.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
WINDOW_HOURS = {"5h": 5.0, "7d": 168.0, "fb": 168.0}
# Floor on the fraction of a window still to run, so a window seconds from reset cannot
# produce an unbounded urgency and drown every other signal.
MIN_TIME_FRACTION = 0.02
RESET_TEXT_RE = re.compile(r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?\Z")
# A rollout filename carries the local start time and the session id, so listing and ordering
# sessions costs no file reads at all.
ROLLOUT_RE = re.compile(
    r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-"
    r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl\Z",
    re.IGNORECASE,
)
SESSIONS_LIMIT = 20


def valid_name(name):
    return bool(name) and bool(NAME_RE.match(name))


def emittable(path):
    """A row is only usable if its dir survives the tab-separated protocol intact."""
    return "\t" not in path and "\n" not in path


def claude_root():
    return os.environ.get("AGENT_PROFILES_CLAUDE_ROOT") or os.path.join(HOME, ".claude", ".profiles")


def codex_root():
    return os.environ.get("AGENT_PROFILES_CODEX_GLOB_ROOT") or HOME


def first_line(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readline().strip() or None
    except OSError:
        return None


def read_json(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def alias_map(root):
    """`old=new` lines under the claude profile root, so names retired by a rename
    keep resolving from session maps and running panes."""
    aliases = {}
    try:
        with open(os.path.join(root, ".aliases"), encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return aliases
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        old, new = (part.strip() for part in line.split("=", 1))
        if old and new:
            aliases[old] = new
    return aliases


def claude_email(directory):
    email = first_line(os.path.join(directory, "email"))
    if email:
        return email
    config = read_json(os.path.join(directory, ".claude.json"))
    if isinstance(config, dict):
        account = config.get("oauthAccount")
        if isinstance(account, dict):
            return account.get("emailAddress") or None
    return None


def claude_rows(*, include_identity=True):
    root = claude_root()
    aliases = alias_map(root)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    rows = []
    for name in names:
        directory = os.path.join(root, name)
        if not valid_name(name) or not os.path.isdir(directory) or not emittable(directory):
            continue
        rows.append({
            "name": name,
            "dir": directory,
            "email": claude_email(directory) if include_identity else None,
            "aliases": sorted(old for old, new in aliases.items() if new == name),
        })
    return rows


def jwt_payload(token):
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def codex_identity(directory):
    """Email and plan come from the id_token's payload. No signature check: this is
    a local label for an account we already hold the tokens of, not an authz decision."""
    auth = read_json(os.path.join(directory, "auth.json"))
    if not isinstance(auth, dict):
        return None, None, False
    tokens = auth.get("tokens")
    api_key = auth.get("OPENAI_API_KEY")
    has_token = isinstance(tokens, dict) and any(
        isinstance(tokens.get(key), str) and bool(tokens.get(key))
        for key in ("access_token", "refresh_token")
    )
    signed_in = has_token or (isinstance(api_key, str) and bool(api_key))
    token = tokens.get("id_token") if isinstance(tokens, dict) else None
    payload = jwt_payload(token)
    if not isinstance(payload, dict):
        return None, None, signed_in
    claims = payload.get("https://api.openai.com/auth")
    plan = claims.get("chatgpt_plan_type") if isinstance(claims, dict) else None
    return payload.get("email") or None, plan or None, signed_in


def codex_row(directory, name, *, include_identity=True):
    email, plan, signed_in = codex_identity(directory) if include_identity else (None, None, None)
    return {
        "name": name if valid_name(name) else None,
        "dir": directory,
        "email": email,
        "plan": plan,
        "signed_in": signed_in,
        "aliases": [],
    }


def codex_rows(*, include_identity=True):
    root = codex_root()
    rows = []
    base = os.path.join(root, CODEX_BASE)
    # An unusable label leaves the base home nameless rather than hiding it: it stays
    # reachable by email, and by the picker.
    if os.path.isdir(base) and emittable(base):
        rows.append(codex_row(base, first_line(os.path.join(base, "profile-label")),
                              include_identity=include_identity))
    for directory in sorted(glob.glob(os.path.join(root, CODEX_PREFIX + "*"))):
        name = os.path.basename(directory)[len(CODEX_PREFIX):]
        if os.path.isdir(directory) and emittable(directory) and valid_name(name):
            rows.append(codex_row(directory, name, include_identity=include_identity))
    return rows


def rollout_files(directory):
    """(start time, session id, path) for one home's live rollouts. Archived sessions are left
    out: `codex resume` cannot reach one until it is unarchived."""
    pattern = os.path.join(directory, "sessions", "*", "*", "*", "rollout-*.jsonl")
    for path in glob.glob(pattern):
        match = ROLLOUT_RE.match(os.path.basename(path))
        if match:
            yield match.group(1), match.group(2).lower(), path


def session_meta(path):
    """The session_meta record every rollout opens with. Only the first line is read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            record = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def same_dir(left, right):
    """A symlinked worktree reaches the same directory by more than one path."""
    if left == right:
        return True
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except OSError:
        return False


def codex_session_rows(limit, everywhere, cwd):
    """Sessions across every codex home, newest first, each tagged with the account that owns it.

    Ordering comes from the filenames, so only the candidates that survive it are opened, and
    only their first line is parsed. Without `everywhere` the list is narrowed to `cwd`, the
    way the Codex picker narrows its own."""
    candidates = []
    for profile in codex_rows():
        for started, session_id, path in rollout_files(profile["dir"]):
            candidates.append((started, session_id, path, profile))
    # Fixed-width local timestamps, so lexicographic order is chronological.
    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    rows = []
    for started, session_id, path, profile in candidates:
        if len(rows) >= limit:
            break
        meta = session_meta(path)
        session_cwd = meta.get("cwd") if meta else None
        if not isinstance(session_cwd, str):
            continue
        if not everywhere and not same_dir(session_cwd, cwd):
            continue
        session = meta.get("id")
        rows.append({
            "profile": profile["name"],
            "dir": profile["dir"],
            "id": session if isinstance(session, str) and session else session_id,
            "path": path,
            "cwd": session_cwd,
            "started": started,
        })
    return rows


def rows_for(cli):
    if cli == "claude":
        return claude_rows()
    if cli == "codex":
        return codex_rows()
    raise ValueError(f"unsupported provider: {cli}")


class ResolveError(Exception):
    def __init__(self, message, code, candidates=()):
        super().__init__(message)
        self.code = code
        self.candidates = list(candidates)


def resolve(cli, rows, query):
    query = query.strip()
    if not query:
        raise ResolveError(f"{cli}: empty profile name", 2)

    def unique(matches, reason):
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise ResolveError(f"{cli}: {query!r} matches {reason}", 2, matches)
        return None

    match = unique([row for row in rows if row["name"] == query], "several profiles by name")
    if match:
        return match
    if cli == "claude":
        target = alias_map(claude_root()).get(query)
        if target:
            match = unique([row for row in rows if row["name"] == target], "several profiles by alias")
            if match:
                return match
    lowered = query.lower()
    match = unique(
        [row for row in rows if (row["email"] or "").lower() == lowered],
        "several profiles by email",
    )
    if match:
        return match
    match = unique(
        [row for row in rows
         if lowered in (row["name"] or "").lower() or lowered in (row["email"] or "").lower()],
        "several profiles",
    )
    if match:
        return match
    raise ResolveError(f"{cli}: no profile matches {query!r}", 1)


def security_bin():
    """The Keychain CLI, pinned by AGENT_PROFILES_SECURITY_BIN for tests only."""
    return os.environ.get("AGENT_PROFILES_SECURITY_BIN") or "security"


def keychain_service(name):
    """(service, account) for one claude profile's credential item — the ONE derivation.

    Claude Code stores each account's OAuth blob under a service that carries the first
    eight hex of sha256 of that account's identity path, so no account rides the bare
    service and a rename can never hand one account another's credentials. The sign-in
    probe here and the token read in yelo.usage.fetch both key off this, so the two can
    never drift apart.
    """
    identity = os.path.join(HOME, f".claude-{name}")
    service = "Claude Code-credentials-" + hashlib.sha256(identity.encode()).hexdigest()[:8]
    return service, os.environ.get("USER") or getpass.getuser()


def hud_label(cli, row):
    """The Usage HUD's label for one account: `cl·NAME`, `cx`, or `cx·NAME`. The codex base
    home names itself through profile-label, and reports as the anonymous `cx` until it has
    one."""
    if cli == "claude":
        return f"cl·{row['name']}"
    name = row.get("name")
    return f"cx·{name}" if name else "cx"


def claude_signed_in(name):
    """Does this account hold credentials? Claude keeps them in the Keychain, keyed by the
    identity path, so the directory says nothing. This is a METADATA probe — the item's
    existence, by return code. It never passes -w, so no secret is read, printed, or logged,
    and it does not raise the access prompt that reading the password would."""
    try:
        service, user = keychain_service(name)
    except OSError:
        return False
    command = [security_bin(), "find-generic-password", "-s", service, "-a", user]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=KEYCHAIN_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def add_signed_in(cli, rows):
    """Codex rows already know from auth.json; claude has to ask the Keychain."""
    if cli == "claude":
        for row in rows:
            row["signed_in"] = claude_signed_in(row["name"])
    return rows


def usage_data_labels(cli, row):
    if cli == "claude":
        name = row.get("name")
        return [f"cl·{name}"] if name else []
    name = row.get("name")
    directory = row.get("dir")
    labels = [f"cx·{name}"] if name else []
    if directory and os.path.basename(directory) == CODEX_BASE:
        labels.append("cx")
    return labels


def parse_reset_hours(text):
    """Hours from the usage data feed's humanized reset ("3d23h", "1h19m", "4m"); None when unreadable."""
    if not isinstance(text, str):
        return None
    match = RESET_TEXT_RE.match(text.strip())
    if not match or not any(match.groups()):
        return None
    days, hours, minutes = (int(part or 0) for part in match.groups())
    return days * 24 + hours + minutes / 60


def claude_model(args=()):
    """Read startup model choices without changing Claude settings or starting Claude.

    Covers local user/project settings and explicit startup overrides. Remote enterprise
    policy and models restored from a transcript remain Claude's responsibility.
    """
    options = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            break
        key, separator, value = arg.partition("=")
        if key in ("--model", "--settings", "--setting-sources"):
            if not separator:
                index += 1
                if index >= len(args):
                    raise ResolveError(f"claude: {key} requires a value", 2)
                value = args[index]
            options[key] = value
        index += 1
    cwd = Path.cwd()
    project = next((parent for parent in (cwd, *cwd.parents)
                    if (parent / ".git").exists()), cwd)
    directory = Path(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"))
    sources = options.get("--setting-sources", "user,project,local").split(",")
    settings = {}
    environment = {}
    for source, path in (("user", directory / "settings.json"),
                         ("project", project / ".claude/settings.json"),
                         ("local", project / ".claude/settings.local.json")):
        data = read_json(path) if source in sources else None
        if isinstance(data, dict):
            settings.update(data)
            if isinstance(data.get("env"), dict):
                environment.update(data["env"])
    if "--settings" in options:
        value = options["--settings"]
        try:
            data = json.loads(value) if value.lstrip().startswith("{") else read_json(value)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise ResolveError("claude: cannot read --settings for account selection", 2)
        settings.update(data)
        if isinstance(data.get("env"), dict):
            environment.update(data["env"])
    environment.update(os.environ)
    model = (options.get("--model") or environment.get("ANTHROPIC_MODEL") or
             settings.get("model") or environment.get("ANTHROPIC_DEFAULT_MODEL") or "default")
    if not isinstance(model, str):
        raise ResolveError("claude: model must be a string", 2)
    family = model.lower().split("[", 1)[0]
    if family in ("fable", "opus", "sonnet", "haiku"):
        model = environment.get(f"ANTHROPIC_DEFAULT_{family.upper()}_MODEL") or model
    return model


def fable_model(model):
    return isinstance(model, str) and ("fable" in model.lower() or model.lower().split("[", 1)[0] == "best")


def usage_data_windows(cli, row, usage_data, include_fable=False):
    """(used percent, hours to reset) per window, from the first usage data label that has rows."""
    for label in usage_data_labels(cli, row):
        matched = [
            entry for entry in usage_data
            if entry.get("provider") == cli and entry.get("label") == label
        ]
        if not matched:
            continue
        windows = {}
        for entry in matched:
            window = entry.get("window")
            pct = entry.get("pct")
            if window not in (("5h", "7d", "fb") if include_fable else ("5h", "7d")) or entry.get("state") == "offline":
                continue
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                continue
            # A window past its reset is full again, whatever percent it last reported,
            # and has its whole span ahead of it rather than zero time.
            if entry.get("reset") == "now":
                windows[window] = (0.0, WINDOW_HOURS[window])
            else:
                windows[window] = (float(pct), parse_reset_hours(entry.get("reset")))
        return windows
    return None


def used_percent(pct, resets_at):
    """None when the window carries no usable number; 0 once its reset has passed."""
    if isinstance(pct, bool) or not isinstance(pct, (int, float)):
        return None
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        if resets_at <= time.time():
            return 0.0
    return float(pct)


def hours_until(resets_at, window):
    """Hours to the reset epoch; a passed reset means a fresh window with its whole span left."""
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    left = resets_at - time.time()
    return WINDOW_HOURS[window] if left <= 0 else left / 3600


def cache_windows(cli, row, include_fable=False):
    """The account's own usage snapshot, for when the usage data feed has no row for it."""
    directory = row["dir"]
    if not directory:
        return None
    windows = {}
    if cli == "claude":
        cache = read_json(os.path.join(directory, ".usage-api-cache.json"))
        cache = cache if isinstance(cache, dict) else {}
        for window, key in (("5h", "five_hour"), ("7d", "seven_day")):
            entry = cache.get(key)
            if not isinstance(entry, dict):
                continue
            pct = used_percent(entry.get("used_percentage"), entry.get("resets_at"))
            if pct is not None:
                windows[window] = (pct, hours_until(entry.get("resets_at"), window))
        if include_fable:
            cache = read_json(os.path.join(directory, ".usage-api-cache-fable.json"))
            entry = cache.get("seven_day") if isinstance(cache, dict) else None
            if isinstance(entry, dict):
                pct = used_percent(entry.get("used_percentage"), entry.get("resets_at"))
                if pct is not None:
                    windows["fb"] = (pct, hours_until(entry.get("resets_at"), "fb"))
        return windows or None
    cache = read_json(os.path.join(directory, ".usage-hud-api-cache.json"))
    limits = cache.get("rate_limits") if isinstance(cache, dict) else None
    if not isinstance(limits, dict):
        return None
    for entry in limits.values():
        if not isinstance(entry, dict):
            continue
        pct = used_percent(entry.get("used_percent"), entry.get("resets_at"))
        if pct is None:
            continue
        minutes = entry.get("window_minutes")
        window = "5h" if isinstance(minutes, (int, float)) and minutes <= 300 else "7d"
        windows[window] = (pct, hours_until(entry.get("resets_at"), window))
    return windows or None

def urgency(windows):
    """Fraction left divided by the fraction of the window still to run, best window wins.
    Capacity whose reset is close scores high (it is about to be wasted); a window with no
    readable reset time counts as having its whole span left. Normalizing by window length
    lets the 5h and 7d windows compete on the same scale."""
    best = 0.0
    for window, (used, hours) in windows.items():
        left = (100.0 - used) / 100.0
        if hours is None:
            fraction = 1.0
        else:
            fraction = min(max(hours / WINDOW_HOURS[window], MIN_TIME_FRACTION), 1.0)
        best = max(best, left / fraction)
    return best


def add_usage(cli, rows, usage_rows, include_fable=False):
    """Adds `usage` (display text), `remaining` (min percent left, None = unknown), and
    `urgency` (see urgency(); None when remaining is unknown). With include_fable,
    adds a hard exclusion flag from the model-specific API window, not statusline data.

    `usage_rows` is the Usage HUD snapshot the caller took (yelo.profile.commands reads it
    from yelo.usage.snapshot), or None when no snapshot could be taken at all — this
    module never reaches for it itself, so core stays free of any yelo.usage import."""
    usage_data = usage_rows
    for row in rows:
        windows = usage_data_windows(cli, row, usage_data, include_fable) if usage_data is not None else None
        if not windows:
            windows = cache_windows(cli, row, include_fable)
        elif include_fable and "fb" not in windows:
            cached = cache_windows(cli, row, True) or {}
            if "fb" in cached:
                windows["fb"] = cached["fb"]
        if include_fable:
            row["fable_exhausted"] = bool(windows and "fb" in windows and windows["fb"][0] >= 100)
        if windows:
            row["usage"] = " · ".join(
                f"{window} {int(round(100 - windows[window][0]))}% left"
                for window in ("5h", "7d", "fb") if window in windows
            )
            row["remaining"] = int(round(min(100 - used for used, _ in windows.values())))
            row["urgency"] = round(urgency(windows), 3)
        elif usage_data is None:
            row["usage"] = "?"
            row["remaining"] = None
            row["urgency"] = None
        else:
            # Signed in, no usage rows yet: never used this window, so it is full and fresh.
            row["usage"] = "no data"
            row["remaining"] = 100
            row["urgency"] = 1.0
    return rows

def columns(cli, rows, usage):
    header = ["NAME", "EMAIL"]
    if cli == "codex":
        header.append("PLAN")
    if usage:
        header.append("USAGE")
    body = []
    for row in rows:
        line = [row["name"] or "-", row["email"] or "-"]
        if cli == "codex":
            line.append(row.get("plan") or "-")
        if usage:
            line.append("not signed in" if row.get("signed_in") is False
                        else (row.get("usage") or "?"))
        body.append(line)
    return header, body


def render(header, body):
    widths = [max(len(row[i]) for row in [header] + body) for i in range(len(header))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in [header] + body
    ]


def started_text(started):
    """`2026-09-01T12-09-04` (local, from the filename) -> `2026-09-01 12:09`."""
    return f"{started[:10]} {started[11:13]}:{started[14:16]}"


def home_relative(path):
    if path == HOME:
        return "~"
    return "~" + path[len(HOME):] if path.startswith(HOME + os.sep) else path


def session_columns(rows, everywhere):
    """CWD earns a column only when the rows are free to differ in it."""
    header = ["PROFILE", "WHEN", "ID"]
    body = [[row["profile"] or "-", started_text(row["started"]), row["id"]] for row in rows]
    if everywhere:
        header.insert(2, "CWD")
        for line, row in zip(body, rows):
            line.insert(2, home_relative(row["cwd"]))
    return header, body


def command_list(args, usage_rows=None):
    rows = add_signed_in(args.cli, rows_for(args.cli))
    if args.usage:
        add_usage(args.cli, rows, usage_rows)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        print(f"{args.cli}: no profiles found", file=sys.stderr)
        return 1
    for line in render(*columns(args.cli, rows, args.usage)):
        print(line)
    return 0


def command_sessions(args):
    """The Codex picker cannot span accounts, so this is how a session in another account is
    found. The id it prints is what `codex --profile PROFILE resume ID` takes."""
    if args.limit <= 0:
        print("codex: --limit must be a positive number", file=sys.stderr)
        return 2
    cwd = os.getcwd()
    rows = codex_session_rows(args.limit, args.all, cwd)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    if not rows:
        scope = "any account" if args.all else f"any account for {home_relative(cwd)}"
        print(f"codex: no sessions found in {scope}", file=sys.stderr)
        return 1
    for line in render(*session_columns(rows, args.all)):
        print(line)
    return 0


def command_menu(args, usage_rows=None):
    rows = rows_for(args.cli)
    if not rows:
        print(f"{args.cli}: no profiles found", file=sys.stderr)
        return 1
    model = getattr(args, "model", None)
    include_fable = args.cli == "claude" and fable_model(claude_model() if model is None else model)
    add_usage(args.cli, add_signed_in(args.cli, rows), usage_rows, include_fable=include_fable)
    _, body = columns(args.cli, rows, True)
    for index, (row, line) in enumerate(zip(rows, render([""] * len(body[0]), body)[1:]), start=1):
        print("\t".join([str(index), row["name"] or "", row["dir"], line]))
    return 0


def emit_row(row, as_json):
    if as_json:
        print(json.dumps(row, ensure_ascii=False))
    else:
        print(f"{row['name'] or ''}\t{row['dir']}")


def command_resolve(args):
    try:
        row = resolve(args.cli, rows_for(args.cli), args.query)
    except ResolveError as error:
        print(str(error), file=sys.stderr)
        for candidate in error.candidates:
            print(f"  {candidate['name'] or '-'}\t{candidate['email'] or '-'}", file=sys.stderr)
        return error.code
    emit_row(row, args.json)
    return 0


def pick(cli, usage_rows=None, model=None):
    """Highest urgency wins — the account whose window is closest to wasting the most
    capacity — with most-remaining as tiebreak; ties keep the first account in list order.
    An account with any window exhausted loses to every usable one. A signed-in account
    with no usage rows yet counts as full, but an account nothing can be read for is not
    guessed at — with no judgeable candidate this fails instead of naming a default.
    A Fable startup model excludes known Fable-exhausted accounts even if all are exhausted
    or only one is signed in. Missing model-specific data is not proof of exhaustion."""
    rows = [row for row in add_signed_in(cli, rows_for(cli)) if row.get("signed_in")]
    if not rows:
        raise ResolveError(f"{cli}: no signed-in account to pick from", 1)
    include_fable = cli == "claude" and fable_model(claude_model() if model is None else model)
    if include_fable:
        add_usage(cli, rows, usage_rows, include_fable=True)
        rows = [row for row in rows if not row["fable_exhausted"]]
        if not rows:
            raise ResolveError("claude: Fable usage is exhausted on every signed-in account; "
                               "wait for reset or choose another model with --model", 1)
    if len(rows) > 1:
        if not include_fable:
            add_usage(cli, rows, usage_rows)
        judged = [row for row in rows if row.get("remaining") is not None]
        if not judged:
            raise ResolveError(f"{cli}: no usage data to pick an account from", 1)
        usable = [row for row in judged if row["remaining"] > 0] or judged
        rows = [max(usable, key=lambda row: (row["urgency"], row["remaining"]))]
    return rows[0]


def command_pick(args, usage_rows=None):
    try:
        row = pick(args.cli, usage_rows, getattr(args, "model", None))
    except ResolveError as error:
        print(str(error), file=sys.stderr)
        return error.code
    emit_row(row, args.json)
    return 0
