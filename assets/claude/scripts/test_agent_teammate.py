#!/usr/bin/env python3

import argparse
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from io import StringIO


sys.dont_write_bytecode = True
SCRIPT_DIR = pathlib.Path(__file__).parent

ROLE_KEYS = (
    "HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD", "HERDR_PANE_ID", "HERDR_ENV",
    "HERDR_REGISTRY_ROOT",
    "HERDR_REGISTRY_CAPABILITY", "HERDR_REGISTRY_KEY", "HERDR_REGISTRY_RESULTS",
)
REGISTRY = {
    "HERDR_REGISTRY_ROOT": "/tmp/herdr-registry/run-abc",
    "HERDR_REGISTRY_CAPABILITY": "cap-0123456789abcdef:" + "a" * 64,
}


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


teammate = load("agent_teammate", "agent-teammate.py")


@contextmanager
def role(**environment):
    """Exactly the given role variables, so a test inherits nothing from the pane it runs
    in — this suite is itself normally executed inside a worker."""
    saved = {key: os.environ.get(key) for key in ROLE_KEYS}
    for key in ROLE_KEYS:
        os.environ.pop(key, None)
    os.environ.update(environment)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@contextmanager
def recorded_host():
    """Replaces the helper's only side-effecting primitive, so a spawn can be inspected
    as the argv it would have produced."""
    commands = []

    def run(command):
        commands.append(command)
        if "split" in command:
            return Result(json.dumps({"result": {"pane_id": "w1:p9"}}))
        if command[1:3] == ["agent", "get"]:
            return Result(json.dumps({"result": {
                "agent_status": "idle",
                "agent_session": {
                    "source": "herdr:codex",
                    "agent": "codex",
                    "kind": "id",
                    "value": "11111111-1111-4111-8111-111111111111",
                },
            }}))
        if "new-split" in command:
            return Result(json.dumps({"surface_ref": "surface:9"}))
        if command[1:3] == ["pane", "edges"]:
            return Result(returncode=1)
        return Result()

    original = teammate.run
    teammate.run = run
    try:
        yield commands
    finally:
        teammate.run = original


def spawn_args(provider="claude", env=None):
    return argparse.Namespace(
        provider=provider, name="w1", cwd="/tmp", direction="down",
        env=list(env or []), agent_args=["--model", "x"],
    )


class ProviderTrustTest(unittest.TestCase):
    def test_runs_provider_preflight_with_the_requested_cwd(self):
        commands = []

        def run(command):
            commands.append(command)
            return Result(stdout='{"managed": false}')

        original = teammate.run
        teammate.run = run
        try:
            teammate.check_provider_trust("codex", "/private/tmp/councils/run")
        finally:
            teammate.run = original

        self.assertEqual(commands, [[
            sys.executable, teammate.PROVIDER_TRUST_SCRIPT,
            "--provider", "codex", "--cwd", "/private/tmp/councils/run",
        ]])

    def test_a_failed_provider_preflight_stops_the_launch(self):
        original = teammate.run
        teammate.run = lambda command: Result(returncode=2, stderr="unsafe generated path")
        try:
            with self.assertRaisesRegex(RuntimeError, "unsafe generated path"):
                teammate.check_provider_trust("agy", "/private/tmp/councils/run")
        finally:
            teammate.run = original


class WorkerSessionTest(unittest.TestCase):
    def test_either_marker_marks_a_worker(self):
        for marker in ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD"):
            with self.subTest(marker=marker), role(**{marker: "1"}):
                self.assertTrue(teammate.worker_session())

    def test_an_unmarked_session_without_a_pane_is_an_orchestrator(self):
        with role():
            self.assertFalse(teammate.worker_session())

    def test_a_registry_root_is_an_orchestrator_despite_the_markers(self):
        with role(HERDR_AGENT_PANE="1", **REGISTRY):
            self.assertFalse(teammate.worker_session())

    def test_a_registry_leaf_stays_a_worker(self):
        """`claim_and_spawn` gives every child a `grants=[]` capability plus a work-item
        key; treating the capability alone as a grant promotes the leaves the drop box
        exists to contain."""
        with role(HERDR_AGENT_PANE="1", HERDR_REGISTRY_KEY="review-auth-1", **REGISTRY):
            self.assertTrue(teammate.worker_session())

    def test_a_half_configured_registry_does_not_exempt(self):
        with role(HERDR_AGENT_PANE="1", HERDR_REGISTRY_ROOT=REGISTRY["HERDR_REGISTRY_ROOT"]):
            self.assertTrue(teammate.worker_session())

    def test_the_gate_refuses_before_touching_the_host(self):
        stderr = StringIO()
        with role(HERDR_AGENT_PANE="1", HERDR_ENV="1"), recorded_host() as commands:
            argv, sys.argv = sys.argv, ["agent-teammate.py", "claude", "w1", "--cwd", "/tmp"]
            err, sys.stderr = sys.stderr, stderr
            try:
                status = teammate.main()
            finally:
                sys.argv, sys.stderr = argv, err
        self.assertEqual(status, 3)
        self.assertIn("worker sessions are leaves", stderr.getvalue())
        self.assertEqual(commands, [])



class ReservedMarkerTest(unittest.TestCase):
    def test_a_caller_cannot_supply_either_marker(self):
        for entry in ("HERDR_AGENT_PANE=0", "AGENT_TEAMMATE_CHILD=0"):
            with self.subTest(entry=entry):
                with self.assertRaises(RuntimeError) as raised:
                    teammate.check_env([entry])
                self.assertIn("reserved", str(raised.exception))

    def test_ordinary_env_still_passes(self):
        self.assertEqual(teammate.check_env(["FOO=bar"]), ["FOO=bar"])

    def test_malformed_env_is_still_rejected(self):
        with self.assertRaises(RuntimeError):
            teammate.check_env(["not-an-assignment"])

    def test_the_herdr_split_plants_the_markers_after_caller_env(self):
        with role(HERDR_ENV="1", HERDR_PANE_ID="w1:p1"), recorded_host() as commands:
            teammate.invoke_herdr(spawn_args(env=["FOO=bar"]))
        split = next(command for command in commands if "split" in command)
        planted = [split[index + 1] for index, token in enumerate(split) if token == "--env"]
        self.assertEqual(planted, ["FOO=bar", "HERDR_AGENT_PANE=1", "AGENT_TEAMMATE_CHILD=1"])

    def test_herdr_codex_workers_return_the_identity_archived_by_the_session_hook(self):
        with role(HERDR_ENV="1", HERDR_PANE_ID="w1:p1"), recorded_host() as commands:
            result = teammate.invoke_herdr(spawn_args(provider="codex"))
        session_id = "11111111-1111-4111-8111-111111111111"
        self.assertEqual(result["session_id"], session_id)
        self.assertNotIn([teammate.PROVIDERS["codex"], "archive", session_id], commands)

    def test_a_retained_pane_is_tagged_before_agent_start_fails(self):
        commands = []

        def run(command):
            commands.append(command)
            if "split" in command:
                return Result(json.dumps({"result": {"pane_id": "w1:p9"}}))
            if command[1:3] == ["agent", "start"]:
                return Result(returncode=1, stderr="agent_not_ready")
            return Result()

        original = teammate.run
        teammate.run = run
        try:
            with role(HERDR_ENV="1", HERDR_PANE_ID="w1:p1"):
                with self.assertRaisesRegex(RuntimeError, "pane retained for diagnosis: w1:p9"):
                    teammate.invoke_herdr(spawn_args(provider="codex"))
        finally:
            teammate.run = original

        tagged = next(i for i, command in enumerate(commands) if "report-metadata" in command)
        started = next(i for i, command in enumerate(commands) if command[1:3] == ["agent", "start"])
        self.assertLess(tagged, started)
        self.assertIn("tree=⠀↳", commands[tagged])

    def test_invalid_codex_agent_identity_is_never_archived(self):
        value = {"agent_session": {"agent": "codex", "kind": "id", "value": "not-a-uuid"}}
        self.assertIsNone(teammate.codex_session_id(value))


class AccountEnvTest(unittest.TestCase):
    """A herdr pane takes its environment from the herdr server, not from this process, so
    an account the caller holds only in its own environment never reaches the spawned
    binary. `herdr agent start --kind` is a closed vocabulary of agent kinds and their
    canonical executables, so a per-account launcher cannot be named to it; the pane gets
    the same assignments the launcher's `exec env` line carries instead.

    Nothing is resolved and nothing is refused (ADR 0005).
    """

    def account_env(self, provider, environment, *agent_args):
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update(dict(environment, HOME="/h"))
        args = list(agent_args)
        try:
            return teammate.account_env(provider, args), args
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_a_claude_pane_carries_the_callers_account(self):
        env, rest = self.account_env("claude", {"AGENT_PROFILE_LABEL": "sid"}, "--model", "x")
        self.assertEqual(env, ["AGENT_PROFILE_LABEL=sid",
                               "CLAUDE_PROFILE_DIR=/h/.claude/.profiles/sid",
                               "CLAUDE_SECURESTORAGE_CONFIG_DIR=/h/.claude-sid"])
        self.assertEqual(rest, ["--model", "x"], "only --profile is taken out")

    def test_an_explicit_profile_wins_and_leaves_the_argv(self):
        """`herdr-registry.py` appends `--profile <label>` to every claude request, and no
        vendor binary has that flag: it has to be consumed here or the spawn dies."""
        for argv in (["--profile", "work", "-p"], ["--profile=work", "-p"]):
            env, rest = self.account_env("claude", {"AGENT_PROFILE_LABEL": "sid"}, *argv)
            self.assertEqual(env[0], "AGENT_PROFILE_LABEL=work")
            self.assertEqual(rest, ["-p"])

    def test_a_claude_pane_with_no_account_carries_none(self):
        """Not a refusal: the pane starts the vendor default, which is the account a person
        gets by typing `claude`."""
        self.assertEqual(self.account_env("claude", {})[0], [])
        self.assertEqual(self.account_env("cloud", {})[0], [])

    def test_a_codex_pane_inherits_the_callers_home(self):
        env, _ = self.account_env("codex", {"CODEX_HOME": "/h/.codex-alt"})
        self.assertEqual(env, ["CODEX_HOME=/h/.codex-alt",
                               "CODEX_CONFIG_PATH=/h/.codex-alt/config.toml"])

    def test_a_codex_profile_names_its_home_without_resolving_anything(self):
        """`--profile alt` is the account `alt`, whose home is `~/.codex-alt`. No roster is
        read and no name is matched: the name IS the account."""
        env, rest = self.account_env("codex", {}, "--profile", "alt", "exec")
        self.assertEqual(env, ["CODEX_HOME=/h/.codex-alt",
                               "CODEX_CONFIG_PATH=/h/.codex-alt/config.toml"])
        self.assertEqual(rest, ["exec"])

    def test_the_claude_label_crosses_a_non_claude_pane(self):
        """So a claude spawn downstream of a codex worker inherits it."""
        env, _ = self.account_env("codex", {"AGENT_PROFILE_LABEL": "sid",
                                            "CODEX_HOME": "/h/.codex-alt"})
        self.assertEqual(env, ["AGENT_PROFILE_LABEL=sid", "CODEX_HOME=/h/.codex-alt",
                               "CODEX_CONFIG_PATH=/h/.codex-alt/config.toml"])
        self.assertEqual(self.account_env("agy", {"AGENT_PROFILE_LABEL": "sid"})[0],
                         ["AGENT_PROFILE_LABEL=sid"])

    def test_a_codex_pane_with_no_account_carries_none(self):
        self.assertEqual(self.account_env("codex", {})[0], [])
        self.assertEqual(self.account_env("codex", {"CODEX_HOME": ""})[0], [],
                         "set but empty is no account, and must not travel")

    def test_agy_keeps_its_own_credentials(self):
        self.assertEqual(self.account_env("agy", {"CODEX_HOME": "/h/.codex"})[0], [])

    def test_agy_argv_reaches_its_binary_exactly_as_written(self):
        """`--profile` belongs to the AGY CLI, so consuming it here would silently change
        the launch. Only claude and codex ever had a wrapper that owned that flag."""
        for spelling in (["--profile", "agy-profile", "--model", "x"],
                         ["--profile=agy-profile", "--model", "x"]):
            env, rest = self.account_env("agy", {}, *spelling)
            self.assertEqual(rest, spelling, spelling)
            self.assertEqual(env, [], spelling)

        # The label still crosses, and still takes nothing out of the argv.
        env, rest = self.account_env("agy", {"AGENT_PROFILE_LABEL": "sid"},
                                     "--profile", "agy-profile")
        self.assertEqual(rest, ["--profile", "agy-profile"])
        self.assertEqual(env, ["AGENT_PROFILE_LABEL=sid"])

    def test_a_spawn_with_no_account_still_reaches_the_host(self):
        """End to end: a caller with nothing to forward runs, carrying no account entry."""
        invoked = []
        stderr = StringIO()
        saved = dict(os.environ)
        os.environ.clear()
        os.environ.update({"HERDR_ENV": "1", "HERDR_PANE_ID": "w1:p1"})
        argv = sys.argv
        sys.argv = ["agent-teammate.py", "codex", "w1", "--role", "code.routine"]
        real_resolve, teammate.resolve_role = teammate.resolve_role, \
            lambda role, provider, agent_args: ({}, list(agent_args))
        real_trust, teammate.check_provider_trust = teammate.check_provider_trust, \
            lambda provider, cwd: None
        real_invoke, teammate.invoke_herdr = teammate.invoke_herdr, \
            lambda args: invoked.append(args) or {
                "host": "herdr", "provider": "codex", "name": args.name, "pane": "w1:p9"}
        real_stderr, sys.stderr = sys.stderr, stderr
        real_stdout, sys.stdout = sys.stdout, StringIO()
        try:
            status = teammate.main()
        finally:
            sys.stdout = real_stdout
            sys.stderr = real_stderr
            teammate.invoke_herdr = real_invoke
            teammate.check_provider_trust = real_trust
            teammate.resolve_role = real_resolve
            sys.argv = argv
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(status, 0, stderr.getvalue())
        self.assertEqual(invoked[0].env, [])
        self.assertNotIn("--profile NAME is required", stderr.getvalue())


class RoleRoutingTest(unittest.TestCase):
    def route(self, payload, provider="claude", agent_args=None, commands=None,
              role="code.routine"):
        original = teammate.run

        def run(command):
            if commands is not None:
                commands.append(command)
            return Result(json.dumps(payload))

        teammate.run = run
        try:
            return teammate.resolve_role(role, provider, list(agent_args or []))
        finally:
            teammate.run = original

    def test_the_route_is_resolved_for_the_requested_provider(self):
        commands = []
        self.route({"provider": "agy", "model": "gemini-3.8-flash", "effort": "high"},
                   provider="agy", commands=commands)
        self.assertEqual(commands[0][1:], ["get", "code.routine", "--provider", "agy"])

    def test_a_cloud_spawn_asks_for_the_claude_provider(self):
        """`cloud` is this helper's own name for a claude pane; the router knows claude."""
        commands = []
        self.route({"provider": "claude", "model": "sonnet", "effort": "medium"},
                   provider="cloud", commands=commands)
        self.assertEqual(commands[0][1:], ["get", "code.routine", "--provider", "claude"])

    def test_a_fable_runner_is_refused_for_a_coding_role(self):
        """Second, independent check: the router refuses Fable at validate time, and a
        config edited past it must still not reach a pane for a coding role."""
        with self.assertRaisesRegex(RuntimeError, "Fable is a child only for"):
            self.route({"provider": "claude", "model": "claude-fable-5-1", "effort": "high"},
                       role="code.routine")

    def test_a_fable_runner_is_allowed_for_a_review_role(self):
        route, args = self.route(
            {"provider": "claude", "model": "claude-fable-5-1", "effort": "high"},
            role="review.deep")
        self.assertEqual(args, ["--model", "claude-fable-5-1", "--effort", "high"])

    def test_claude_role_applies_model_and_effort(self):
        route, args = self.route({
            "provider": "claude", "model": "sonnet", "effort": "medium",
            "runnerId": "claude-sonnet-medium",
        })
        self.assertEqual(route["runnerId"], "claude-sonnet-medium")
        self.assertEqual(args, ["--model", "sonnet", "--effort", "medium"])

    def test_codex_role_applies_model_effort_and_permissions(self):
        _, args = self.route({
            "provider": "codex", "model": "gpt-5.6-sol", "effort": "high",
            "sandbox": "workspace-write", "approval": "never",
        }, provider="codex")
        self.assertEqual(args, [
            "--model", "gpt-5.6-sol", "-c", 'model_reasoning_effort="high"',
            "--sandbox", "workspace-write", "--ask-for-approval", "never",
        ])

    def test_agy_role_applies_model_and_effort(self):
        _, args = self.route({
            "provider": "agy", "model": "gemini-3.6-flash-high", "effort": "high",
        }, provider="agy")
        self.assertEqual(args, [
            "--model", "gemini-3.6-flash-high", "--effort", "high",
        ])

    def test_role_rejects_provider_mismatch(self):
        with self.assertRaisesRegex(RuntimeError, "routes to codex"):
            self.route({
                "provider": "codex", "model": "gpt-5.6-sol", "effort": "high",
            })

    def test_role_rejects_conflicting_flags(self):
        with self.assertRaisesRegex(RuntimeError, "remove the conflicting"):
            self.route({
                "provider": "claude", "model": "sonnet", "effort": "medium",
            }, agent_args=["--model", "opus"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class AgyHealthTest(unittest.TestCase):
    """The launcher must not report an agy pane ready on a drawn TUI alone."""

    PID_LINE = f"I0902 server.go:1491] Starting language server process with pid {os.getpid()}\n"
    AUTH_LINE = "I0902 server_oauth.go:197] OAuth: authenticated successfully as x@y\n"
    EXPIRED_LINE = "I0902 keyring.go:81] keyringAuth: loaded token, expiry=2026-08-30 expired=true\n"

    def app_dir(self, lines, age_seconds=0, crash=False):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        os.makedirs(os.path.join(root, "log"))
        os.makedirs(os.path.join(root, "crashes"))
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(time.time() - age_seconds))
        with open(os.path.join(root, "log", f"cli-{stamp}.log"), "w") as handle:
            handle.writelines(lines)
        if crash:
            open(os.path.join(root, "crashes", "crash_1.log"), "w").close()
        return root

    def test_ready_when_authenticated_and_the_server_is_alive(self):
        root = self.app_dir([self.PID_LINE, self.AUTH_LINE])
        self.assertIsNone(teammate.agy_health(time.time() - 5, app_dir=root, timeout=1, poll=0.05))

    def test_an_unauthenticated_start_is_reported_with_the_log_line(self):
        root = self.app_dir([self.PID_LINE, self.EXPIRED_LINE])
        reason = teammate.agy_health(time.time() - 5, app_dir=root, timeout=0.3, poll=0.05)
        self.assertIn("did not authenticate", reason)
        self.assertIn("expired=true", reason)

    def test_a_dead_language_server_fails_fast(self):
        root = self.app_dir([self.PID_LINE, self.AUTH_LINE])
        started = time.monotonic()
        reason = teammate.agy_health(time.time() - 5, app_dir=root, timeout=30, poll=0.05,
                                     alive=lambda pid: False)
        self.assertIn("language server pid", reason)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_crash_report_alone_is_not_a_fault(self):
        # agy writes an empty crashes/ file while the server stays up and answers.
        root = self.app_dir([self.PID_LINE, self.AUTH_LINE], crash=True)
        self.assertIsNone(teammate.agy_health(time.time() - 5, app_dir=root, timeout=1, poll=0.05))

    def test_an_exited_cli_fails_fast(self):
        exited = "I0902 common.go:417] CLI program exited, shutting down\n"
        root = self.app_dir([self.PID_LINE, self.AUTH_LINE, exited])
        started = time.monotonic()
        reason = teammate.agy_health(time.time() - 5, app_dir=root, timeout=30, poll=0.05)
        self.assertIn("CLI exited", reason)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_log_from_an_earlier_session_is_not_ours(self):
        root = self.app_dir([self.PID_LINE, self.AUTH_LINE], age_seconds=3600)
        reason = teammate.agy_health(time.time() - 5, app_dir=root, timeout=0.3, poll=0.05)
        self.assertIn("no agy CLI log appeared", reason)

    def test_agy_spawn_reports_ready_only_after_the_health_gate(self):
        healthy = self.app_dir([self.PID_LINE, self.AUTH_LINE])
        sick = self.app_dir([self.PID_LINE, self.EXPIRED_LINE])
        saved = {k: os.environ.get(k) for k in ("AGENT_TEAMMATE_AGY_APP_DIR", "AGENT_TEAMMATE_AGY_HEALTH_TIMEOUT")}
        os.environ["AGENT_TEAMMATE_AGY_HEALTH_TIMEOUT"] = "0.3"
        try:
            os.environ["AGENT_TEAMMATE_AGY_APP_DIR"] = healthy
            with role(HERDR_ENV="1", HERDR_PANE_ID="w1:p1"), recorded_host():
                result = teammate.invoke_herdr(spawn_args(provider="agy"))
            self.assertEqual(result["pane"], "w1:p9")
            os.environ["AGENT_TEAMMATE_AGY_APP_DIR"] = sick
            with role(HERDR_ENV="1", HERDR_PANE_ID="w1:p1"), recorded_host():
                with self.assertRaisesRegex(RuntimeError, "pane retained for diagnosis: w1:p9"):
                    teammate.invoke_herdr(spawn_args(provider="agy"))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
