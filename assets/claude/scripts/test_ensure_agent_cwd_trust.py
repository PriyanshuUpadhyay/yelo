#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).parent
spec = importlib.util.spec_from_file_location(
    "ensure_agent_cwd_trust", HERE / "ensure-agent-cwd-trust.py"
)
trust = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trust)


class EnsureAgentCwdTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = pathlib.Path(self.temp.name)
        self.root = self.base / "generated"
        self.root.mkdir(mode=0o700)
        self.cwd = self.root / "run" / "worker"
        self.cwd.mkdir(parents=True, mode=0o700)
        self.codex = self.base / "codex.toml"
        self.codex.write_text('model = "test"\n\n[projects]\n')
        self.agy = self.base / "settings.json"
        self.agy.write_text(json.dumps({
            "enableTelemetry": True,
            "permissions": {"allow": ["command(existing)"]},
            "trustedWorkspaces": ["/existing"],
            "unrelated": "preserved",
        }))
        self.baseline = self.base / "baseline.json"
        self.baseline.write_text(json.dumps({
            "agentMode": "accept-edits",
            "enableTelemetry": False,
            "permissions": {"allow": ["command(required)"]},
            "toolPermission": "always-proceed",
        }))

    def test_matches_only_children_of_configured_roots(self):
        roots = (str(self.root),)
        self.assertEqual(trust.containing_root(str(self.cwd), roots), str(self.root))
        self.assertIsNone(trust.containing_root(str(self.root), roots))
        self.assertIsNone(trust.containing_root(str(self.base / "other"), roots))

    def test_rejects_a_writable_generated_path_component(self):
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "not group/world-writable"):
            trust.validate_generated_path(str(self.root), str(unsafe))

    def test_adds_an_exact_codex_target_once(self):
        target = str(self.cwd)
        self.assertTrue(trust.trust_codex(str(self.codex), target))
        self.assertFalse(trust.trust_codex(str(self.codex), target))
        self.assertIn(
            f'[projects."{target}"]\ntrust_level = "trusted"',
            self.codex.read_text(),
        )

    def test_codex_uses_the_generated_git_root(self):
        repo = self.root / "repo"
        repo.mkdir(mode=0o700)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        child = repo / "nested"
        child.mkdir(mode=0o700)
        self.assertEqual(
            trust.codex_trust_target(str(child), str(self.root)),
            os.path.realpath(repo),
        )

    def test_agy_merges_baseline_and_preserves_runtime_state(self):
        target = str(self.cwd)
        self.assertTrue(trust.trust_agy(str(self.agy), target, str(self.baseline)))
        self.assertFalse(trust.trust_agy(str(self.agy), target, str(self.baseline)))
        settings = json.loads(self.agy.read_text())
        self.assertEqual(settings["agentMode"], "accept-edits")
        self.assertFalse(settings["enableTelemetry"])
        self.assertEqual(
            settings["permissions"]["allow"],
            ["command(required)", "command(existing)"],
        )
        self.assertEqual(settings["trustedWorkspaces"], ["/existing", target])
        self.assertEqual(settings["unrelated"], "preserved")

    def test_agy_syncs_baseline_without_trusting_an_unmanaged_cwd(self):
        self.assertTrue(trust.trust_agy(str(self.agy), None, str(self.baseline)))
        settings = json.loads(self.agy.read_text())
        self.assertEqual(settings["trustedWorkspaces"], ["/existing"])
        self.assertEqual(settings["agentMode"], "accept-edits")


if __name__ == "__main__":
    unittest.main()
