#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch


SCRIPT_DIR = pathlib.Path(__file__).parent
MARKERS = (
    "SWARM_SESSION_ID", "SWARM_AGENT_ID", "HERDR_AGENT_PANE",
    "SWARM_ADAPTER", "HERDR_PANE_ID", "HERDR_ACTIVE_PANE_ID",
)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


spawn_role = load("swarm_spawn_role", "swarm-spawn-role.py")


@contextmanager
def environment(**values):
    names = set(MARKERS) | set(values)
    saved = {name: os.environ.get(name) for name in names}
    for name in MARKERS:
        os.environ.pop(name, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for name in names:
            value = saved[name]
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class Result:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


class SpawnRoleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.record = self.root / "argv.json"
        fake = self.bin / "swarm"
        fake.write_text(
            "#!/bin/sh\n"
            "python3 - \"$@\" <<'PY'\n"
            "import json, os, sys\n"
            "with open(os.environ['SWARM_ARGV_RECORD'], 'w') as handle:\n"
            "    json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd()}, handle)\n"
            "PY\n"
            "printf 'pane:test\\n'\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        self.temp.cleanup()

    def route(self, command):
        provider = command[-1]
        payload = {
            "claude": {
                "provider": "claude", "model": "claude-test", "effort": "high",
                "permission": "acceptEdits",
            },
            "codex": {
                "provider": "codex", "model": "codex-test", "effort": "medium",
                "sandbox": "workspace-write", "approval": "never",
            },
            "agy": {
                "provider": "agy", "model": "agy-test", "effort": "low",
                "permission": "plan",
            },
        }[provider]
        return Result(json.dumps(payload))

    def invoke(self, *argv, **environment_values):
        output, errors = StringIO(), StringIO()
        path = f"{self.bin}{os.pathsep}{os.environ['PATH']}"
        base = {
            "SWARM_SESSION_ID": "7", "SWARM_AGENT_ID": "orchestrator",
            "SWARM_ARGV_RECORD": str(self.record), "PATH": path,
        }
        base.update(environment_values)
        with environment(**base), redirect_stdout(output), redirect_stderr(errors):
            status = spawn_role.main(list(argv))
        record = json.loads(self.record.read_text()) if self.record.exists() else None
        return status, output.getvalue(), errors.getvalue(), record

    def test_builds_one_routed_argv_for_each_provider(self):
        cwd = self.root / "work"
        cwd.mkdir()
        trusted = []
        provider_trusted = []
        self.assertEqual(spawn_role.prepare_agy_worker([]), [])
        fixed_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
        cases = {
            "claude": [
                spawn_role.COMMANDS["claude"], "--session-id",
                "aaaaaaaa-1111-4111-8111-111111111111", "-n", "seat-claude",
                "--model", "claude-test", "--effort", "high",
                "--permission-mode", "acceptEdits", "--add-dir", str(cwd),
            ],
            "codex": [
                spawn_role.COMMANDS["codex"], "--model", "codex-test",
                "-c", 'model_reasoning_effort="medium"', "--sandbox",
                "workspace-write", "--ask-for-approval", "never", "-c",
                'sandbox_workspace_write.writable_roots=["/tmp/swarm-home/.swarm"]', "--search",
            ],
            "agy": [
                spawn_role.COMMANDS["agy"], "--model", "agy-test",
                "--effort", "low", "--mode", "plan", "-i", "first prompt",
            ],
        }
        extras = {"claude": [], "codex": ["--search"], "agy": ["first prompt"]}
        with patch.object(spawn_role, "run", side_effect=self.route), \
                patch.object(spawn_role, "check_trust", side_effect=trusted.append), \
                patch.object(spawn_role, "check_provider_trust", side_effect=lambda kind, path: provider_trusted.append((kind, path))), \
                patch.object(spawn_role.uuid, "uuid4", return_value=fixed_uuid):
            for kind in cases:
                with self.subTest(kind=kind):
                    self.record.unlink(missing_ok=True)
                    status, output, errors, record = self.invoke(
                        kind, f"seat-{kind}", "--role", "code.routine",
                        "--cwd", str(cwd), "--", *extras[kind],
                        **({"SWARM_HOME": "/tmp/swarm-home"} if kind == "codex" else {}),
                    )
                    self.assertEqual((status, output, errors), (0, "pane:test\n", ""))
                    self.assertEqual(record["argv"], [
                        "spawn", f"seat-{kind}", "code.routine", "--", *cases[kind],
                    ])
                    expected_cwd = cwd / ".herdr" / "workers" if kind == "claude" else cwd
                    self.assertEqual(record["cwd"], os.path.realpath(expected_cwd))
        self.assertEqual(trusted, [str(cwd / ".herdr" / "workers")])
        self.assertEqual(provider_trusted, [("codex", str(cwd)), ("agy", str(cwd))])

    def test_codex_non_workspace_sandbox_has_no_writable_root(self):
        cwd = self.root / "work"
        cwd.mkdir()
        with patch.object(spawn_role, "run", return_value=Result(json.dumps({
                    "provider": "codex", "model": "codex-test", "effort": "medium",
                    "sandbox": "read-only", "approval": "never",
                }))):
            status, _, errors, record = self.invoke(
                "codex", "seat", "--role", "code.routine", "--cwd", str(cwd),
            )
        self.assertEqual((status, errors), (0, ""))
        self.assertNotIn("writable_roots", record["argv"][-1])

    def test_unset_session_is_refused(self):
        with environment(SWARM_AGENT_ID="orchestrator"), \
                redirect_stderr(errors := StringIO()):
            status = spawn_role.main(["codex", "seat", "--role", "code.routine"])
        self.assertEqual(status, 2)
        self.assertIn("SWARM_SESSION_ID is not set", errors.getvalue())
        self.assertFalse(self.record.exists())

    def test_worker_callers_are_refused_before_spawn(self):
        callers = (
            {"SWARM_AGENT_ID": "child"},
            {"SWARM_AGENT_ID": "orchestrator", "HERDR_AGENT_PANE": "1"},
        )
        for caller in callers:
            with self.subTest(caller=caller):
                self.record.unlink(missing_ok=True)
                status, _, errors, record = self.invoke(
                    "codex", "seat", "--role", "code.routine", **caller
                )
                self.assertEqual(status, 3)
                self.assertIn("worker sessions cannot spawn workers", errors)
                self.assertIsNone(record)


class ProviderTrustTest(unittest.TestCase):
    def test_runs_provider_preflight_with_the_requested_cwd(self):
        commands = []

        def run(command):
            commands.append(command)
            return Result(stdout='{"managed": false}')

        original = spawn_role.run
        spawn_role.run = run
        try:
            spawn_role.check_provider_trust("codex", "/private/tmp/councils/run")
        finally:
            spawn_role.run = original

        self.assertEqual(commands, [[
            sys.executable, spawn_role.PROVIDER_TRUST_SCRIPT,
            "--provider", "codex", "--cwd", "/private/tmp/councils/run",
        ]])

    def test_a_failed_provider_preflight_stops_the_launch(self):
        original = spawn_role.run
        spawn_role.run = lambda command: Result(returncode=2, stderr="unsafe generated path")
        try:
            with self.assertRaisesRegex(RuntimeError, "unsafe generated path"):
                spawn_role.check_provider_trust("agy", "/private/tmp/councils/run")
        finally:
            spawn_role.run = original


class RoleRoutingTest(unittest.TestCase):
    def route(self, payload, provider="claude", agent_args=None, commands=None,
              role="code.routine"):
        original = spawn_role.run

        def run(command):
            if commands is not None:
                commands.append(command)
            return Result(json.dumps(payload))

        spawn_role.run = run
        try:
            return spawn_role.resolve_role(role, provider, list(agent_args or []))
        finally:
            spawn_role.run = original

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
            "permission": "auto",
            "runnerId": "claude-sonnet-medium",
        })
        self.assertEqual(route["runnerId"], "claude-sonnet-medium")
        self.assertEqual(args, ["--model", "sonnet", "--effort", "medium", "--permission-mode", "auto"])

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
            "permission": "skip",
        }, provider="agy")
        self.assertEqual(args, [
            "--model", "gemini-3.6-flash-high", "--effort", "high",
            "--dangerously-skip-permissions",
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

    def test_role_rejects_permission_override(self):
        with self.assertRaisesRegex(RuntimeError, "remove the conflicting"):
            self.route({
                "provider": "claude", "model": "sonnet", "effort": "medium",
                "permission": "auto",
            }, agent_args=["--permission-mode", "plan"])

class RelayoutTest(unittest.TestCase):
    def relayout(self, **values):
        commands = []
        with environment(**values), patch.object(
            spawn_role, "run",
            side_effect=lambda command: commands.append(command) or Result(),
        ), redirect_stderr(StringIO()):
            spawn_role.relayout_herdr()
            active = os.environ.get("HERDR_ACTIVE_PANE_ID")
        return commands, active

    def test_a_herdr_spawn_reapplies_the_main_grid_on_the_orchestrator_tab(self):
        commands, active = self.relayout(
            SWARM_ADAPTER="herdr", HERDR_PANE_ID="pane:1"
        )
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][1:], [spawn_role.LAYOUT_SCRIPT, "main-grid"])
        self.assertEqual(active, "pane:1")

    def test_other_hosts_and_paneless_callers_keep_their_layout(self):
        for values in ({"SWARM_ADAPTER": "tmux", "HERDR_PANE_ID": "pane:1"},
                       {"SWARM_ADAPTER": "herdr"}):
            with self.subTest(values=values):
                self.assertEqual(self.relayout(**values)[0], [])


if __name__ == "__main__":
    unittest.main()
