#!/usr/bin/env python3

"""Spawn one routed provider role as a visible swarm pane.

`swarm spawn` types the command line into the new pane's interactive shell, so this
helper only has to resolve the route, prepare the provider's argv, and hand the line over.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid


ROUTING_SCRIPT = "/Users/priyanshu/.claude/scripts/agent-routing.mjs"
TRUST_SCRIPT = "/Users/priyanshu/.claude/scripts/ensure-cwd-trust.sh"
PROVIDER_TRUST_SCRIPT = "/Users/priyanshu/.claude/scripts/ensure-agent-cwd-trust.py"
LAYOUT_SCRIPT = "/Users/priyanshu/.config/herdr/bin/layout-shortcuts/action.mjs"
RESUME_FLAGS = ("-r", "--resume", "-c", "--continue", "--fork-session")
WORKER_POOL = (".herdr", "workers")
WORKER_SID_PREFIX = "aaaaaaaa"
# Command words, not paths: swarm types this line into a pane's interactive shell, where
# `claude` and `codex` are the yelo functions that pick the account by the launch model. A
# path skips them and lands the worker on whatever account the bare config dir holds.
COMMANDS = {"claude": "claude", "codex": "codex", "agy": "agy"}


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


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
        "claude": ("--model", "--effort", "--permission-mode", "--mode", "--dangerously-skip-permissions", "--yolo"),
        "codex": ("-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval"),
        "agy": ("--model", "--effort", "--permission-mode", "--mode", "--dangerously-skip-permissions", "--yolo"),
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
        if route.get("permission"):
            flags += ["--permission-mode", route["permission"]]
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
        if route.get("permission") == "skip":
            flags += ["--dangerously-skip-permissions"]
        elif route.get("permission"):
            flags += ["--mode", route["permission"]]
    return route, [*flags, *agent_args]


def prepare_agy_worker(agent_args):
    """Make an initial positional prompt explicit while preserving a prompt-free TUI."""
    if agent_args and not agent_args[0].startswith("-"):
        return ["-i", *agent_args]
    return agent_args


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
    """An untrusted cwd makes the worker boot into the folder-trust dialog and idle
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


def parse_command_line(argv):
    parser = argparse.ArgumentParser(
        description="Spawn one routed provider role through swarm."
    )
    parser.add_argument("kind", choices=("claude", "codex", "agy"))
    parser.add_argument("agent_id")
    parser.add_argument("--role", required=True)
    parser.add_argument("--cwd", default=os.getcwd())
    if "--" in argv:
        boundary = argv.index("--")
        launcher_args, provider_args = argv[:boundary], argv[boundary + 1:]
    else:
        launcher_args, provider_args = argv, []
    args = parser.parse_args(launcher_args)
    args.provider_args = provider_args
    return args


def worker_session():
    swarm_agent = os.environ.get("SWARM_AGENT_ID")
    return (
        (swarm_agent is not None and swarm_agent != "orchestrator")
        or os.environ.get("HERDR_AGENT_PANE") == "1"
    )


def build_spawn(args):
    provider_args = list(args.provider_args)
    caller_args_count = len(provider_args)
    if args.kind == "agy":
        provider_args = prepare_agy_worker(provider_args)
    route, provider_args = resolve_role(args.role, args.kind, provider_args)
    if args.kind == "codex" and route.get("sandbox") == "workspace-write":
        store_dir = os.path.join(
            os.environ.get("SWARM_HOME", os.path.expanduser("~")), ".swarm"
        )
        route_args_end = len(provider_args) - caller_args_count
        provider_args[route_args_end:route_args_end] = [
            "-c", f"sandbox_workspace_write.writable_roots={json.dumps([store_dir])}"
        ]

    cwd = args.cwd
    if args.kind in ("codex", "agy"):
        check_provider_trust(args.kind, cwd)
    if args.kind == "claude":
        launch = argparse.Namespace(
            cwd=cwd, agent_args=provider_args, name=args.agent_id
        )
        prepare_claude_worker(launch)
        check_trust(launch.cwd)
        cwd, provider_args = launch.cwd, launch.agent_args

    command = [
        "swarm", "spawn", args.agent_id, args.role, "--",
        COMMANDS[args.kind], *provider_args,
    ]
    return command, cwd


def run_spawn(command, cwd):
    result = subprocess.run(
        command, cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "swarm spawn failed"
        raise RuntimeError(message)
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)


def relayout_herdr():
    """Herdr only: re-apply the main-grid layout so a new child pane joins the right
    grid instead of shrinking the orchestrator. Every spawn re-applies it, because no
    spawn knows it is the last one; the layout is idempotent. Cosmetic, so a failure
    only warns and the spawn still counts as done.
    """
    pane = os.environ.get("HERDR_PANE_ID")
    node = shutil.which("node")
    if os.environ.get("SWARM_ADAPTER") != "herdr" or not pane or not node:
        return
    os.environ["HERDR_ACTIVE_PANE_ID"] = pane
    try:
        result = run([node, LAYOUT_SCRIPT, "main-grid"])
    except OSError as error:
        result = argparse.Namespace(returncode=1, stderr=str(error), stdout="")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "layout failed"
        sys.stderr.write(f"swarm-spawn-role: main-grid layout skipped: {detail}\n")


def main(argv=None):
    if worker_session():
        print(
            "swarm-spawn-role: worker sessions cannot spawn workers",
            file=sys.stderr,
        )
        return 3
    if "SWARM_SESSION_ID" not in os.environ:
        print("swarm-spawn-role: SWARM_SESSION_ID is not set", file=sys.stderr)
        return 2
    try:
        args = parse_command_line(sys.argv[1:] if argv is None else argv)
        command, cwd = build_spawn(args)
        run_spawn(command, cwd)
    except (OSError, RuntimeError) as error:
        print(f"swarm-spawn-role: {error}", file=sys.stderr)
        return 2
    relayout_herdr()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
