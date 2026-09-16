#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import pathlib
import subprocess
import sys


SCRIPT_DIR = pathlib.Path(__file__).parent


def load_teammate():
    spec = importlib.util.spec_from_file_location(
        "swarm_spawn_role_agent_teammate", SCRIPT_DIR / "agent-teammate.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


teammate = load_teammate()


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
        or os.environ.get("AGENT_TEAMMATE_CHILD") == "1"
    )


def build_spawn(args):
    provider_args = list(args.provider_args)
    caller_args_count = len(provider_args)
    if args.kind == "agy":
        provider_args = teammate.prepare_agy_worker(provider_args)
    route, provider_args = teammate.resolve_role(args.role, args.kind, provider_args)
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
        teammate.check_provider_trust(args.kind, cwd)
    if args.kind == "claude":
        launch = argparse.Namespace(
            cwd=cwd, agent_args=provider_args, name=args.agent_id
        )
        teammate.prepare_claude_worker(launch)
        teammate.check_trust(launch.cwd)
        cwd, provider_args = launch.cwd, launch.agent_args

    command = [
        "swarm", "spawn", args.agent_id, args.role, "--",
        teammate.PROVIDERS[args.kind], *provider_args,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
