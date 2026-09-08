#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


HERDR_CONTEXT = """[agent-host: herdr]
This session is running inside Herdr. The top-level session is the orchestrator.
- Every worker must be a visible foreground pane split from HERDR_PANE_ID.
- Use `agent-teammate <claude|codex|agy> <unique-name> --role ROLE --cwd "$PWD" -- [extra agent flags]`; it resolves model/effort and calls `herdr pane split` plus `herdr agent start`.
- Do not use provider-native subagents, headless CLIs, detached processes, or background workers.
- For non-trivial pane lifecycle work, load `~/.claude/skills/herdr-ops/SKILL.md`.
- Workers are leaves: answer only, do not orchestrate, spawn descendants, or notify the user."""

HERDR_WORKER_CONTEXT = """[agent-host: herdr — worker]
This session is a worker pane, a child of the orchestrator session. Act only on the task you were assigned.
- Never spawn visible panes: `agent-teammate` and every `herdr` surface-creating command (`pane split`, `pane run`, `agent start`, `tab create`, `workspace create`, `worktree create`) are orchestrator-only and are refused for worker sessions.
- Remain a leaf: no provider-native subagents, workflow fan-out, headless one-shots, review rounds, or multi-agent pipelines.
- Report results to the orchestrator only; do not notify the user."""

# Codex-only delta: the native collab tools are a one-token affordance the shared contract
# does not name, and a task that asks for parallel agents reads as permission to use them.
CODEX_HERDR_DELEGATION = """- `spawn_agent`, `wait_agent` and the rest of the codex collaboration family are not a delegation path here: their workers run in-process and Herdr cannot show them. That holds however explicitly a task asks for sub-agents or parallel work — when you cannot create a visible pane, report that and stop; never substitute a hidden worker."""

# Probe delta, for a session with NO drop box only. A provisioned session already knows its
# spawn path, and telling it to probe would manufacture the denial HL-051 exists to remove.
CODEX_SOCKET_PROBE = """- The `agent-teammate` line above states the general contract; these two cases resolve it for you and supersede it. Settle your spawn path ONCE, by testing the control socket with a single `herdr status`, then stay on the answer:
  - `herdr status` answers → you are the unsandboxed driver. Spawn with `python3 ~/.claude/scripts/agent-teammate.py <family> <unique-name> --role ROLE --cwd "$PWD" -- [extra agent flags]`; the bare `agent-teammate` is a zsh function and does not exist in a non-interactive shell.
  - `herdr status` answers `PermissionDenied` → your sandbox denies that socket, so every direct `agent-teammate.py` or `herdr` call is denied for the same reason, and no drop box is provisioned for this session. You have no spawn path: report that and stop. Do not retry the call, and do not request an escalated sandbox to force one through — escalation spends a user approval on a path this session is not meant to use."""

# Registry delta: a sandboxed root obeys the clause above and then finds `agent-teammate`
# unusable, because the sandbox denies connect(2) to Herdr's control socket. Without a second
# path it either stalls or reaches back for the native tools, so the contract has to name the
# one primitive it still has — a file rename into the drop box.
CODEX_REGISTRY_DELEGATION = """- Your spawn path is settled and supersedes the `agent-teammate` line above: this session has a registry drop box. Do not probe for an alternative — no `herdr status`, no `agent-teammate.py`, no other `herdr` command. This sandbox denies Herdr's control socket by design, so those calls only produce a denial, and an escalated sandbox is never the remedy for one.
- The drop box is already provisioned at {root}, and your environment carries the capability it needs:
  `python3 ~/.claude/scripts/herdr-registry.py request <family> <worker-name> --key <idempotency-key> --cwd "$PWD" --wait 120`
  A host-side watcher outside your sandbox performs the real pane split, so the worker is a visible pane exactly as `agent-teammate` would have produced.
- Every placeholder above, with what it accepts — nothing here needs to be guessed:
  `<family>` is exactly one of `claude`, `codex`, `agy`.
  `<worker-name>` matches `[a-z0-9][a-z0-9_-]{{0,31}}`, for example `reviewer-1`.
  `<idempotency-key>` matches `[A-Za-z0-9][A-Za-z0-9._-]{{0,63}}` and identifies the unit of work, for example `review-auth-1`. Reuse the same key when you retry the same work: a repeat submission returns the existing pane instead of creating a second one.
  `--cwd` must sit inside the allowlist the host recorded; `"$PWD"` is the normal answer.
  `--wait` is seconds to block for a decision; 120 is a reasonable default.
- To pin a model, append ` -- --model <model-id>` — for example ` -- --model claude-opus-5` or ` -- --model gpt-5.6-sol`. Omit it entirely and the host picks the provider default. After `--`, only `--model` is accepted (plus `--profile` and `--permission-mode` for claude); anything else is refused rather than interpreted.
- Exit codes are the answer: 0 means the pane exists and its provenance checks out, 3 means the host refused the request and prints why, 4 means no decision inside the wait, 5 means a pane exists but its provenance is broken. Only 0 is success. On anything else, report the failure — never substitute an in-process agent, a background worker, or serial self-execution."""

RUNTIME_ADAPTERS = {
    "claude": "~/.claude/skills/orchestrate-claude/SKILL.md",
    "codex": "~/.agents/skills/orchestrate-codex/SKILL.md",
    "agy": "~/.gemini/config/skills/orchestrate-agy/SKILL.md",
}


WORKER_MARKERS = ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD")
WORKER_HISTORY = Path("~/.claude/hooks/worker-history-classifier.py").expanduser()


def registry_root():
    """The run-scoped registry root this session was launched with, if any.

    Presence of the environment variable IS the switch: `herdr-registry.py launch` sets it
    on the pane, so a root that has one was provisioned deliberately and a root that has
    none keeps exactly the contract it had before.
    """
    root = os.environ.get("HERDR_REGISTRY_ROOT")
    if not root or not os.environ.get("HERDR_REGISTRY_CAPABILITY"):
        return None
    return root


def registry_orchestrator():
    """A registry ROOT, as opposed to a registry-spawned leaf.

    `launch` provisions a root with a spawn-granted capability and nothing else, while
    `claim_and_spawn` mints every child a `grants=[]` capability and hands it
    `HERDR_REGISTRY_KEY` plus `HERDR_REGISTRY_RESULTS` for its own work item
    (herdr-registry.py:1621-1661). The grant set itself is MAC'd host-side metadata this
    process cannot read, so the work-item key is the leaf marker that IS readable — and the
    registry's own leafness rule (`capability_no_spawn`) is what it stands for.
    """
    return bool(registry_root()) and not os.environ.get("HERDR_REGISTRY_KEY")


def worker_session():
    """True in a pane an orchestrator spawned: `agent-teammate` plants both markers on the
    child's environment, so a session reads its own role from the env it was born with.

    Only a registry ROOT outranks the markers: its drop box is an explicit grant to spawn,
    and `launch` provisions it THROUGH `agent-teammate`, so it carries child markers while
    being the orchestrator of its own run. A registry leaf holds a no-grant capability and
    stays a worker.
    """
    if registry_orchestrator():
        return False
    return any(os.environ.get(name) == "1" for name in WORKER_MARKERS)


def active_context():
    worker = worker_session()
    if os.environ.get("HERDR_ENV") == "1" and os.environ.get("HERDR_PANE_ID"):
        return HERDR_WORKER_CONTEXT if worker else HERDR_CONTEXT
    return None


def session_start_output(context):
    """Codex SessionStart hook output, or `{}` when no visible host is active.

    `additionalContext` is the only field of this hook the model actually receives;
    `systemMessage` surfaces to the user instead. That is why codex root 019faa65 ran 19
    invisible `spawn_agent` workers while this hook was installed and trusted — the
    contract was emitted, just never into the conversation.
    """
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }


def bind_runtime(context, provider):
    if not context:
        return context
    adapter = RUNTIME_ADAPTERS[provider]
    return f"""{context}

[agent-runtime: {provider}]
Before executing any portable skill that requests workers, load `{adapter}`.
The runtime adapter translates workflow requirements; the active agent-host contract owns worker lifecycle and visibility."""


def contain_worker_history(provider, payload, runner=subprocess.run):
    if provider not in ("codex", "agy") or not worker_session():
        return
    try:
        runner(
            [str(WORKER_HISTORY), "--provider", provider],
            input=payload,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("claude", "codex", "agy"), required=True)
    args = parser.parse_args()
    payload = sys.stdin.read()
    contain_worker_history(args.provider, payload)
    context = active_context()
    if args.provider == "codex" and context == HERDR_CONTEXT:
        context = f"{context}\n{CODEX_HERDR_DELEGATION}"
        root = registry_root()
        # Exactly one spawn path is described, so there is nothing to probe for and no
        # contradiction to resolve.
        if root:
            context = f"{context}\n{CODEX_REGISTRY_DELEGATION.format(root=root)}"
        else:
            context = f"{context}\n{CODEX_SOCKET_PROBE}"
    context = bind_runtime(context, args.provider)
    if args.provider == "claude":
        if context:
            print(context)
    elif args.provider == "codex":
        print(json.dumps(session_start_output(context)))
    else:
        steps = [{"ephemeralMessage": context}] if context else []
        print(json.dumps({"injectSteps": steps}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
