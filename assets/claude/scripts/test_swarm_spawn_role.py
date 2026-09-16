#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import stat
import tempfile
import unittest
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch


SCRIPT_DIR = pathlib.Path(__file__).parent
MARKERS = (
    "SWARM_SESSION_ID", "SWARM_AGENT_ID", "HERDR_AGENT_PANE",
    "AGENT_TEAMMATE_CHILD",
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
        self.assertEqual(spawn_role.teammate.prepare_agy_worker([]), [])
        fixed_uuid = uuid.UUID("11111111-1111-4111-8111-111111111111")
        cases = {
            "claude": [
                spawn_role.teammate.PROVIDERS["claude"], "--session-id",
                "aaaaaaaa-1111-4111-8111-111111111111", "-n", "seat-claude",
                "--model", "claude-test", "--effort", "high",
                "--permission-mode", "acceptEdits", "--add-dir", str(cwd),
            ],
            "codex": [
                spawn_role.teammate.PROVIDERS["codex"], "--model", "codex-test",
                "-c", 'model_reasoning_effort="medium"', "--sandbox",
                "workspace-write", "--ask-for-approval", "never", "-c",
                'sandbox_workspace_write.writable_roots=["/tmp/swarm-home/.swarm"]', "--search",
            ],
            "agy": [
                spawn_role.teammate.PROVIDERS["agy"], "--model", "agy-test",
                "--effort", "low", "--mode", "plan", "-i", "first prompt",
            ],
        }
        extras = {"claude": [], "codex": ["--search"], "agy": ["first prompt"]}
        with patch.object(spawn_role.teammate, "run", side_effect=self.route), \
                patch.object(spawn_role.teammate, "check_trust", side_effect=trusted.append), \
                patch.object(spawn_role.teammate, "check_provider_trust", side_effect=lambda kind, path: provider_trusted.append((kind, path))), \
                patch.object(spawn_role.teammate.uuid, "uuid4", return_value=fixed_uuid):
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
        with patch.object(spawn_role.teammate, "run", return_value=Result(json.dumps({
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
            {"SWARM_AGENT_ID": "orchestrator", "AGENT_TEAMMATE_CHILD": "1"},
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


if __name__ == "__main__":
    unittest.main()
