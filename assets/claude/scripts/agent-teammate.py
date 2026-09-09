#!/usr/bin/env python3

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid

MIN_WORKER_WIDTH = 80
ORCHESTRATOR_SHARE = 0.5
ORCHESTRATOR_SHARE_TIGHT = 0.3
AGENT_START_RETRY_SECONDS = 10.0
AGENT_START_RETRY_INTERVAL_SECONDS = 0.1
PANE_BUSY_MARKERS = ("agent_pane_busy", "not an available shell")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SEAT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TRUST_SCRIPT = "/Users/priyanshu/.claude/scripts/ensure-cwd-trust.sh"
PROVIDER_TRUST_SCRIPT = "/Users/priyanshu/.claude/scripts/ensure-agent-cwd-trust.py"
ROUTING_SCRIPT = "/Users/priyanshu/.claude/scripts/agent-routing.mjs"
CLAUDE_PROFILES = (".claude", ".profiles")
CODEX_PREFIX = ".codex-"
# The providers whose shell wrapper used to own `--profile`, and whose account therefore
# has to be carried into the pane. Every other provider keeps its own argv and its own
# credentials: `agy` has a `--profile` that belongs to the AGY CLI, not to this helper.
ACCOUNT_PROVIDERS = ("claude", "cloud", "codex")
RESUME_FLAGS = ("-r", "--resume", "-c", "--continue", "--fork-session")
WORKER_POOL = (".herdr", "workers")
WORKER_SID_PREFIX = "aaaaaaaa"
WORKER_MARKERS = ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD")
RESERVED_ENV = (*WORKER_MARKERS, "HERDR_PARENT_PANE", "HERDR_BUS_DIR", "HERDR_SEAT")
CHILD_ENV = "AGENT_TEAMMATE_CHILD=1"
WORKER_REFUSAL = (
    "worker sessions are leaves and do not spawn descendants or visible panes. "
    "Complete the assigned task and report back to the orchestrator."
)

AGY_APP_DIR = "/Users/priyanshu/.gemini/antigravity-cli"
AGY_HEALTH_TIMEOUT_SECONDS = 45.0
AGY_HEALTH_POLL_SECONDS = 1.0
AGY_AUTH_MARKER = "OAuth: authenticated successfully"
AGY_SERVER_PID = re.compile(r"language server process with pid (\d+)")
AGY_LOG_NAME = re.compile(r"^cli-(\d{8}_\d{6})\.log$")
AGY_EXIT_MARKER = "CLI program exited"
AGY_LOG_INTEREST = re.compile(r"not logged|expired=|eligib|panic|fatal|exited|OAuth", re.IGNORECASE)
AGY_LOG_PREFIX = "ERROR: logging before google.Init: "
WORKER_HISTORY_CLASSIFIER = os.path.expanduser(
    "~/.claude/hooks/worker-history-classifier.py"
)
PROVIDERS = {
    "claude": "/Users/priyanshu/.local/bin/claude",
    "cloud": "/Users/priyanshu/.local/bin/claude",
    "codex": "/opt/homebrew/bin/codex",
    "agy": "/Users/priyanshu/.local/bin/agy",
}


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def registry_orchestrator():
    """A registry ROOT. `launch` provisions one through this helper with a spawn-granted
    capability, while `claim_and_spawn` gives every child a `grants=[]` capability plus a
    `HERDR_REGISTRY_KEY` work item — so the key present means leaf, absent means root."""
    return bool(
        os.environ.get("HERDR_REGISTRY_ROOT")
        and os.environ.get("HERDR_REGISTRY_CAPABILITY")
        and not os.environ.get("HERDR_REGISTRY_KEY")
    )


def worker_session():
    """Explicit launch markers define leaves. A registry root is the only exemption."""
    if registry_orchestrator():
        return False
    return any(os.environ.get(name) == "1" for name in WORKER_MARKERS)


def json_output(result, operation):
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"{operation} failed"
        raise RuntimeError(message)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{operation} returned invalid JSON") from error


def find_value(value, keys, prefix=None):
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and (prefix is None or candidate.startswith(prefix)):
                return candidate
        for child in value.values():
            candidate = find_value(child, keys, prefix)
            if candidate:
                return candidate
    if isinstance(value, list):
        for child in value:
            candidate = find_value(child, keys, prefix)
            if candidate:
                return candidate
    return None


def tab_layout(binary, pane):
    """Pane tree of pane's tab, in real terminal cells.

    `herdr pane layout` reports a synthetic 80x24 window grid, so only `pane edges`
    yields widths that can be compared against a column threshold.
    """
    result = run([binary, "pane", "edges", "--pane", pane])
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    layout = find_layout(payload.get("result", payload))
    if not layout or not layout.get("panes") or not layout.get("area"):
        return None
    return layout


def find_layout(value):
    if isinstance(value, dict):
        if isinstance(value.get("panes"), list) and isinstance(value.get("area"), dict):
            return value
        for child in value.values():
            candidate = find_layout(child)
            if candidate:
                return candidate
    return None


def rightmost_pane(layout):
    return max(layout["panes"], key=lambda item: item["rect"]["x"])["pane_id"]


def rebalance_row(binary, anchor):
    layout = tab_layout(binary, anchor)
    if not layout:
        return
    area = layout["area"]
    panes = sorted(layout["panes"], key=lambda item: item["rect"]["x"])
    splits = layout.get("splits") or []
    ids = [item["pane_id"] for item in panes]
    if len(panes) < 2 or anchor not in ids:
        return
    if any(split["direction"] != "right" for split in splits):
        return
    if any(
        item["rect"]["y"] != area["y"] or item["rect"]["height"] != area["height"]
        for item in panes
    ):
        return
    workers = len(panes) - 1
    share = ORCHESTRATOR_SHARE
    if area["width"] * (1 - share) / workers < MIN_WORKER_WIDTH:
        share = ORCHESTRATOR_SHARE_TIGHT
    worker_width = area["width"] * (1 - share) / workers
    targets = {
        item["pane_id"]: area["width"] * share if item["pane_id"] == anchor else worker_width
        for item in panes
    }
    for split in splits:
        rect = split["rect"]
        inside = [
            item for item in panes
            if rect["x"] <= item["rect"]["x"] < rect["x"] + rect["width"]
        ]
        boundary = rect["x"] + split["ratio"] * rect["width"]
        left = [
            item for item in inside
            if item["rect"]["x"] + item["rect"]["width"] <= boundary + 1
        ]
        right = [item for item in inside if item not in left]
        total = sum(targets[item["pane_id"]] for item in inside)
        if not left or not right or total <= 0:
            continue
        delta = sum(targets[item["pane_id"]] for item in left) / total - split["ratio"]
        if abs(delta) < 0.005:
            continue
        # `resize` grows the named pane toward `direction`, so the boundary is moved by
        # whichever side of it should expand.
        mover, direction = (left[-1], "right") if delta > 0 else (right[0], "left")
        run([
            binary, "pane", "resize", "--pane", mover["pane_id"],
            "--direction", direction, "--amount", f"{abs(delta):.4f}",
        ])


def start_agent(command):
    """`herdr agent start` races the shell startup of a freshly split pane."""
    deadline = time.monotonic() + AGENT_START_RETRY_SECONDS
    while True:
        started = run(command)
        message = f"{started.stderr}{started.stdout}"
        if started.returncode == 0 or time.monotonic() >= deadline:
            return started
        if not any(marker in message for marker in PANE_BUSY_MARKERS):
            return started
        time.sleep(AGENT_START_RETRY_INTERVAL_SECONDS)


def agy_logs_since(log_dir, started_at):
    """agy opens one cli-YYYYMMDD_HHMMSS.log per CLI start, named in local time. Selecting
    by name, not mtime, keeps another live agy pane's log from being mistaken for ours."""
    found = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return found
    for name in names:
        match = AGY_LOG_NAME.match(name)
        if not match:
            continue
        try:
            stamp = time.mktime(time.strptime(match.group(1), "%Y%m%d_%H%M%S"))
        except (ValueError, OverflowError):
            continue
        if stamp >= started_at - 2:
            found.append((stamp, os.path.join(log_dir, name)))
    return [path for _, path in sorted(found)]


def process_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def log_excerpt(text, limit=3):
    lines = [line.replace(AGY_LOG_PREFIX, "", 1).strip() for line in text.splitlines()
             if AGY_LOG_INTEREST.search(line)]
    return " | ".join(line[:160] for line in lines[-limit:]) or "(no auth lines in the CLI log)"


def agy_health(started_at, app_dir=None, timeout=None, poll=AGY_HEALTH_POLL_SECONDS,
               alive=process_alive):
    """None once the agy CLI started at `started_at` has authenticated and its language
    server is alive, else the reason. `herdr agent start` succeeds as soon as the TUI is
    drawn, which is before the keyring token resolves and before the language server has
    survived its first seconds; both faults left a pane that accepted prompts and answered
    nothing (2026-09-02: an expired token mid-refresh, then a CLI that exited 8 s after the
    1.1.24 update). The CLI log is the only place either shows. Files under crashes/ are
    NOT a death signal: agy writes an empty crash report while the server stays up and
    keeps answering, so they are ignored here."""
    app_dir = app_dir or os.environ.get("AGENT_TEAMMATE_AGY_APP_DIR", AGY_APP_DIR)
    if timeout is None:
        timeout = float(os.environ.get("AGENT_TEAMMATE_AGY_HEALTH_TIMEOUT",
                                       AGY_HEALTH_TIMEOUT_SECONDS))
    if timeout <= 0:
        return None
    log_dir = os.path.join(app_dir, "log")
    deadline = time.monotonic() + timeout
    text = ""
    while True:
        logs = agy_logs_since(log_dir, started_at)
        if logs:
            try:
                with open(logs[-1], encoding="utf-8", errors="replace") as handle:
                    text = handle.read()
            except OSError:
                text = ""
            if AGY_EXIT_MARKER in text:
                return f"agy CLI exited after start; {log_excerpt(text)}"
            match = AGY_SERVER_PID.search(text)
            if match and not alive(int(match.group(1))):
                return f"agy language server pid {match.group(1)} exited; {log_excerpt(text)}"
            if match and AGY_AUTH_MARKER in text:
                return None
        if time.monotonic() >= deadline:
            what = "no agy CLI log appeared" if not logs else "agy did not authenticate"
            return f"{what} within {timeout:g}s; {log_excerpt(text)}"
        time.sleep(poll)


def check_env(entries):
    for entry in entries:
        name, separator, _ = entry.partition("=")
        if not separator or not name or any(c in entry for c in "\0\n"):
            raise RuntimeError(f"--env {entry!r} is not KEY=VALUE")
        # A caller must not replace the worker role or its return route.
        if name in RESERVED_ENV:
            raise RuntimeError(f"--env {name} is reserved; the helper plants it itself")
        if not ENV_NAME.match(name):
            raise RuntimeError(f"--env {entry!r} is not KEY=VALUE: {name!r} is not a shell name")
    return entries


def herdr_bus_root():
    root = os.environ.get("HERDR_BUS_DIR")
    if not root:
        workspace = os.environ.get("HERDR_WORKSPACE_ID")
        if not workspace:
            raise RuntimeError("HERDR_WORKSPACE_ID is missing; cannot route worker results")
        root = os.path.join("/tmp", f"herdr-bus-{os.getuid()}", workspace)
    return os.path.realpath(os.path.abspath(os.path.expanduser(root)))


def account_flag(agent_args):
    """`--profile X` or `--profile=X`, taken out of the argv and returned.

    It was the shell wrapper's flag and no vendor binary has one, so it must not reach a
    binary — and `herdr-registry.py` still appends it to every claude request. Removing it
    here is what keeps that request spawnable.
    """
    for index, argument in enumerate(agent_args):
        if argument == "--profile":
            value = agent_args[index + 1] if index + 1 < len(agent_args) else ""
            del agent_args[index:index + 2]
            return value
        if argument.startswith("--profile="):
            value = argument.split("=", 1)[1]
            del agent_args[index]
            return value
    return None


def account_env(provider, agent_args):
    """The account the pane starts under, as the environment its vendor binary reads.

    These are the assignments `jello setup launchers` writes into a launcher's `exec env`
    line. A launcher cannot be named to `herdr agent start`, whose `--kind` is a closed
    vocabulary of agent kinds and their canonical executables — anything else is
    `unsupported_agent_kind` — so the account reaches the pane as environment instead,
    which is the same process environment by another road.

    Nothing is resolved and nothing is refused. An explicit `--profile` is taken exactly as
    written, the caller's own account is passed on when it has one, and a spawn with
    neither starts the vendor default. The claude label crosses every provider's pane, so a
    claude spawn downstream of a codex worker still inherits it.

    `--profile` is consumed only for the two CLIs whose shell wrapper used to read it.
    Every other provider's argv reaches its binary exactly as the caller wrote it: `agy`
    has a `--profile` of its own, and taking it out here would silently change the launch.
    """
    home = os.path.expanduser("~")
    label = os.environ.get("AGENT_PROFILE_LABEL", "")
    if provider not in ACCOUNT_PROVIDERS:
        return [f"AGENT_PROFILE_LABEL={label}"] if label else []
    named = account_flag(agent_args)
    if provider in ("claude", "cloud"):
        label = named or label
        if not label:
            return []
        return [
            f"AGENT_PROFILE_LABEL={label}",
            "CLAUDE_PROFILE_DIR=" + os.path.join(home, *CLAUDE_PROFILES, label),
            "CLAUDE_SECURESTORAGE_CONFIG_DIR=" + os.path.join(home, ".claude-" + label),
        ]
    carried = [f"AGENT_PROFILE_LABEL={label}"] if label else []
    directory = (os.path.join(home, CODEX_PREFIX + named) if named
                 else os.environ.get("CODEX_HOME", ""))
    if not directory:
        return carried
    return carried + [f"CODEX_HOME={directory}",
                      "CODEX_CONFIG_PATH=" + os.path.join(directory, "config.toml")]


def has_flag(agent_args, flags):
    for arg in agent_args:
        # everything past a bare `--` is a positional prompt, not a flag
        if arg == "--":
            return False
        if arg.split("=", 1)[0] in flags:
            return True
    return False


def resolve_role(role, provider, agent_args):
    owned_flags = {
        "claude": ("--model", "--effort"),
        "codex": ("-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval"),
        "agy": ("--model", "--effort"),
    }
    canonical_provider = "claude" if provider == "cloud" else provider
    if has_flag(agent_args, owned_flags[canonical_provider]):
        raise RuntimeError(
            f"--role owns {canonical_provider} model, effort, sandbox, and approval flags; "
            "remove the conflicting provider flags"
        )
    result = run([ROUTING_SCRIPT, "get", role, "--provider", canonical_provider])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "role resolve failed"
        raise RuntimeError(f"cannot resolve role {role!r}: {message}")
    try:
        route = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"role {role!r} resolve returned invalid JSON") from error
    if route.get("error"):
        raise RuntimeError(f"cannot resolve role {role!r}: {route['error']}")
    if route.get("provider") != canonical_provider:
        raise RuntimeError(
            f"role {role!r} routes to {route.get('provider') or '<missing>'}, "
            f"not requested provider {canonical_provider}"
        )

    model = route.get("model")
    effort = route.get("effort")
    # The router already refuses a Fable runner at validate time; this is the second,
    # independent check, so a config edited past the router still cannot reach a pane.
    if (model and re.search(r"fable", model, re.IGNORECASE)
            and not role.startswith(("review.", "council."))):
        raise RuntimeError(
            f"Fable is a child only for review.* and council.* (role {role} resolved to {model})"
        )
    flags = []
    if canonical_provider == "claude":
        if not model or not effort:
            raise RuntimeError(f"role {role!r} must resolve both model and effort for Claude")
        flags += ["--model", model, "--effort", effort]
    elif canonical_provider == "codex":
        if not model or not effort:
            raise RuntimeError(f"role {role!r} must resolve both model and effort for Codex")
        flags += ["--model", model, "-c", f'model_reasoning_effort="{effort}"']
        if route.get("sandbox"):
            flags += ["--sandbox", route["sandbox"]]
        if route.get("approval"):
            flags += ["--ask-for-approval", route["approval"]]
    else:
        if model and model != "default":
            flags += ["--model", model]
        if not effort:
            raise RuntimeError(f"role {role!r} must resolve effort for AGY")
        flags += ["--effort", effort]
    return route, [*flags, *agent_args]


def prepare_claude_worker(args):
    """Keep worker transcripts out of the caller's /resume picker.

    Claude derives its transcript directory from the session's cwd and offers no way to
    override it, so workers run from a pool dir under the caller's tree (`--add-dir`
    restores tool access to it) and get a session id in a reserved namespace. Herdr
    restores a pane by replaying `--resume` from the pane's cwd, which is the pool.
    Resuming callers keep their cwd: their transcript already lives under the original
    slug and moving cwd would orphan it.
    """
    resuming = has_flag(args.agent_args, RESUME_FLAGS)
    if not resuming:
        original = args.cwd
        args.cwd = os.path.join(original, *WORKER_POOL)
        os.makedirs(args.cwd, exist_ok=True)
        args.agent_args += ["--add-dir", original]
    if resuming or has_flag(args.agent_args, ("--session-id",)):
        return None
    session_id = WORKER_SID_PREFIX + str(uuid.uuid4())[len(WORKER_SID_PREFIX):]
    named = [] if has_flag(args.agent_args, ("-n", "--name")) else ["-n", args.name]
    args.agent_args = ["--session-id", session_id, *named, *args.agent_args]
    return session_id


def check_trust(cwd):
    """An untrusted cwd makes the teammate boot into the folder-trust dialog and idle
    forever. Trust entries key on the canonical path (macOS /tmp vs /private/tmp).
    """
    real = os.path.realpath(cwd)
    trusted = False
    try:
        with open(os.path.expanduser("~/.claude.json")) as handle:
            projects = json.load(handle).get("projects", {})
        trusted = projects.get(real, {}).get("hasTrustDialogAccepted") is True
    except (OSError, ValueError, AttributeError):
        trusted = False
    if trusted:
        return
    result = run([TRUST_SCRIPT, real])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "ensure-cwd-trust.sh failed"
        raise RuntimeError(
            f"cwd is not trusted for claude and could not be pre-trusted: {real}; {message}. "
            f"Run `{TRUST_SCRIPT} {shlex.quote(real)}` from a session allowed to write "
            "~/.claude.json, then respawn"
        )


def check_provider_trust(provider, cwd):
    result = run([
        sys.executable, PROVIDER_TRUST_SCRIPT,
        "--provider", provider, "--cwd", cwd,
    ])
    if result.returncode != 0:
        message = (
            result.stderr.strip() or result.stdout.strip() or "provider trust preflight failed"
        )
        raise RuntimeError(
            f"could not preflight {provider} trust for {os.path.realpath(cwd)}: {message}"
        )


def agent_record(binary, name):
    result = run([binary, "agent", "get", name])
    if result.returncode != 0:
        return {}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    value = payload.get("result", payload)
    return value if isinstance(value, dict) else {}


def agent_session_id(value, provider):
    if isinstance(value, dict):
        session = value.get("agent_session") or value.get("agentSession")
        if isinstance(session, dict) and session.get("agent") == provider:
            candidate = session.get("value")
            try:
                return str(uuid.UUID(str(candidate)))
            except ValueError:
                return None
        for child in value.values():
            candidate = agent_session_id(child, provider)
            if candidate:
                return candidate
    if isinstance(value, list):
        for child in value:
            candidate = agent_session_id(child, provider)
            if candidate:
                return candidate
    return None


def hide_agy_worker(session_id):
    environment = dict(os.environ)
    environment["HERDR_AGENT_PANE"] = "1"
    result = subprocess.run(
        [WORKER_HISTORY_CLASSIFIER, "--provider", "agy"],
        input=json.dumps({"conversationId": session_id}),
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        env=environment,
    )
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "classifier failed")


def invoke_herdr(args):
    binary = os.environ.get("AGENT_HARNESS_HERDR_BIN", "herdr")
    anchor = os.environ.get("HERDR_PANE_ID")
    if not anchor:
        raise RuntimeError("HERDR_PANE_ID is missing; cannot anchor a visible teammate")
    if not SEAT_NAME.match(args.name):
        raise RuntimeError(f"agent name {args.name!r} cannot be used as HERDR_SEAT")
    target = anchor
    if args.direction == "right":
        layout = tab_layout(binary, anchor)
        if layout:
            target = rightmost_pane(layout)
    extra = []
    for entry in args.env:
        extra += ["--env", entry]
    split = json_output(run([
        binary, "pane", "split", "--pane", target,
        "--direction", args.direction, "--cwd", args.cwd, "--no-focus",
        *extra, "--env", f"HERDR_PARENT_PANE={anchor}",
        "--env", f"HERDR_BUS_DIR={herdr_bus_root()}",
        "--env", f"HERDR_SEAT={args.name}",
        # markers last: with duplicate assignments the last one wins, so they cannot be
        # overridden by caller env even if the reserved-name check is ever relaxed
        "--env", "HERDR_AGENT_PANE=1", "--env", CHILD_ENV,
    ]), "herdr pane split")
    pane = find_value(split.get("result", split), ("pane_id", "paneId"))
    if not pane:
        raise RuntimeError("herdr pane split did not return a pane id")
    # Display-only fake nesting (HL-082): the split establishes the lead→worker
    # relation even when agent startup stops at a recoverable prompt. Tag the retained
    # pane before startup so a later recovery does not leave an unmarked worker.
    # The parser needs the pane id before the options; the reverse order is rejected.
    run([
        binary, "pane", "report-metadata", pane,
        "--source", "agent-teammate", "--token", "tree=⠀↳",
    ])
    provider = "claude" if args.provider == "cloud" else args.provider
    started_at = time.time()
    started = start_agent([
        binary, "agent", "start", args.name,
        "--kind", provider, "--pane", pane, "--",
        *args.agent_args,
    ])
    if started.returncode != 0:
        message = started.stderr.strip() or started.stdout.strip() or "herdr agent start failed"
        raise RuntimeError(
            f"herdr agent start failed; pane retained for diagnosis: {pane}; {message}. "
            f"Inspect with `herdr agent explain {pane}`, "
            f"`herdr pane process-info --pane {pane}`, and `herdr pane read {pane}`; "
            f"close it after diagnosis with `herdr pane close {pane}`"
        )
    if provider == "agy":
        fault = agy_health(started_at)
        if fault:
            raise RuntimeError(
                f"agy is not ready; pane retained for diagnosis: {pane}; {fault}. "
                f"Inspect with `herdr pane read {pane}` and the newest {AGY_APP_DIR}/log/cli-*.log; "
                f"close it after diagnosis with `herdr pane close {pane}`"
            )
    if args.direction == "right":
        rebalance_row(binary, anchor)
    record = agent_record(binary, args.name)
    result = {
        "host": "herdr", "provider": provider, "name": args.name, "pane": pane,
        "status": find_value(record, ("agent_status",)) or "unknown",
    }
    if provider in ("codex", "agy"):
        session_id = agent_session_id(record, provider)
        if session_id and provider == "agy":
            try:
                hide_agy_worker(session_id)
            except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                raise RuntimeError(
                    f"could not hide agy worker {session_id}; pane retained for diagnosis: "
                    f"{pane}; {error}"
                ) from error
        if session_id:
            result["session_id"] = session_id
    return result


def parse_command_line(argv):
    parser = argparse.ArgumentParser(
        description="Launch one visible interactive teammate through the active pane host."
    )
    parser.add_argument("provider", choices=tuple(PROVIDERS))
    parser.add_argument("name")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--direction", choices=("right", "down"), default="right")
    parser.add_argument(
        "--role", required=True,
        help="route key (for example code.routine) whose runner model, effort, and "
             "permissions are resolved and enforced",
    )
    parser.add_argument(
        "--env", action="append", default=[], metavar="KEY=VALUE",
        help="extra environment for the teammate's pane; repeatable",
    )
    if "--" in argv:
        boundary = argv.index("--")
        launcher_args, agent_args = argv[:boundary], argv[boundary + 1:]
    else:
        launcher_args, agent_args = argv, []
    args = parser.parse_args(launcher_args)
    args.agent_args = agent_args
    return args


def main():
    if worker_session():
        print(f"agent-teammate: {WORKER_REFUSAL}", file=sys.stderr)
        return 3
    args = parse_command_line(sys.argv[1:])
    session_id = None
    try:
        check_env(args.env)
        route = None
        if args.role:
            route, args.agent_args = resolve_role(args.role, args.provider, args.agent_args)
        args.env += account_env(args.provider, args.agent_args)
        if args.provider in ("codex", "agy"):
            check_provider_trust(args.provider, args.cwd)
        if args.provider in ("claude", "cloud"):
            session_id = prepare_claude_worker(args)
            check_trust(args.cwd)
        if os.environ.get("HERDR_ENV") == "1":
            result = invoke_herdr(args)
        else:
            raise RuntimeError(
                "no visible agent host is active; continue serially or ask the user for a fallback grant"
            )
    except RuntimeError as error:
        print(f"agent-teammate: {error}", file=sys.stderr)
        return 2
    if args.provider in ("claude", "cloud"):
        result["cwd"] = args.cwd
        if session_id:
            result["session_id"] = session_id
    if route:
        result.update({
            "role": args.role,
            "runner_id": route.get("runnerId"),
            "model": route.get("model"),
            "effort": route.get("effort"),
        })
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
