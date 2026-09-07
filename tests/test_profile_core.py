#!/usr/bin/env python3
"""The resolver's own tests, ported from ~/.claude/scripts/test_agent_profiles.py.

Only two things changed: the module is imported as `yelo.profile.core` instead of being
loaded from a file path, and the subprocess runs `yelo profile ...` instead of the script.
Every assertion is the original one.
"""
import getpass
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from yelo.profile import core as agent_profiles
from yelo.usage import fetch as usage_fetch

YELO = [sys.executable, "-m", "yelo.cli", "profile"]
SECURITY_STUB = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
service = None
for index, item in enumerate(argv):
    if item == "-s" and index + 1 < len(argv):
        service = argv[index + 1]
with open(os.environ["FAKE_KEYCHAIN"], encoding="utf-8") as handle:
    items = json.load(handle)
blob = items.get(service)
if blob is None:
    sys.exit(1)
if "-w" in argv:
    sys.stdout.write(blob)
sys.exit(0)
"""


class ParseResetHours(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(agent_profiles.parse_reset_hours("3d23h"), 95)
        self.assertAlmostEqual(agent_profiles.parse_reset_hours("1h19m"), 1 + 19 / 60)
        self.assertAlmostEqual(agent_profiles.parse_reset_hours("4m"), 4 / 60)

    def test_unreadable(self):
        for text in ("now", "?", "", None, "5x"):
            self.assertIsNone(agent_profiles.parse_reset_hours(text))


class Urgency(unittest.TestCase):
    def test_fresh_full_window_scores_one(self):
        self.assertAlmostEqual(agent_profiles.urgency({"5h": (0.0, 5.0)}), 1.0)

    def test_expiring_capacity_scores_high(self):
        # 83% left with 1/5 of the window to run: about to waste most of it.
        self.assertAlmostEqual(agent_profiles.urgency({"5h": (17.0, 1.0)}), 0.83 / 0.2)

    def test_best_window_wins_across_lengths(self):
        windows = {"5h": (0.0, 5.0), "7d": (41.0, 40.0)}
        self.assertAlmostEqual(agent_profiles.urgency(windows), 0.59 / (40 / 168))

    def test_unknown_reset_counts_as_whole_span(self):
        self.assertAlmostEqual(agent_profiles.urgency({"7d": (30.0, None)}), 0.7)

    def test_time_fraction_floor_bounds_score(self):
        self.assertAlmostEqual(agent_profiles.urgency({"5h": (0.0, 0.001)}), 1 / 0.02)


class KeychainService(unittest.TestCase):
    """C12: one derivation of the Keychain item, shared by the sign-in probe and the token
    read. The rule is written out here rather than imported, so the code has to agree with
    it and not merely with itself."""

    def expected(self, name):
        identity = os.path.join(agent_profiles.HOME, f".claude-{name}")
        digest = hashlib.sha256(identity.encode()).hexdigest()[:8]
        return "Claude Code-credentials-" + digest

    def test_service_is_the_identity_path_digest(self):
        service, user = agent_profiles.keychain_service("pri")
        self.assertEqual(service, self.expected("pri"))
        self.assertEqual(user, os.environ.get("USER") or getpass.getuser())
        self.assertNotEqual(agent_profiles.keychain_service("work")[0], service)

    def test_keychain_service_shared(self):
        """A fake `security` that knows only the derived item satisfies both callers, so
        neither can be keying off a derivation of its own."""
        with tempfile.TemporaryDirectory() as tmp:
            items = pathlib.Path(tmp, "keychain.json")
            items.write_text(json.dumps({
                self.expected("pri"): json.dumps({"claudeAiOauth": {"accessToken": "t-1"}}),
            }))
            security = pathlib.Path(tmp, "security")
            security.write_text(SECURITY_STUB)
            security.chmod(security.stat().st_mode | stat.S_IXUSR)
            os.environ["AGENT_PROFILES_SECURITY_BIN"] = str(security)
            os.environ["FAKE_KEYCHAIN"] = str(items)
            try:
                self.assertTrue(agent_profiles.claude_signed_in("pri"))
                self.assertEqual(usage_fetch.read_keychain_token("pri"), "t-1")
                self.assertFalse(agent_profiles.claude_signed_in("work"))
                self.assertIsNone(usage_fetch.read_keychain_token("work"))
            finally:
                del os.environ["AGENT_PROFILES_SECURITY_BIN"]
                del os.environ["FAKE_KEYCHAIN"]


class Pick(unittest.TestCase):
    """The picker's own cases, unchanged in what they assert. The rows used to come from a
    fake usage-hud-data on PATH; they now come from the cache files the snapshot reads, so
    the reset texts are written as epochs that render back to the same durations."""

    def run_pick(self, caches, names):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "profiles")
            for name in names:
                root.joinpath(name).mkdir(parents=True)
            now = int(time.time())
            for name, windows in caches.items():
                document = {"ts": now, "fetched_at": now, "source": "api"}
                for key, (pct, offset) in windows.items():
                    document[key] = {"used_percentage": pct, "resets_at": now + offset}
                root.joinpath(name, ".usage-api-cache.json").write_text(json.dumps(document))
            env = os.environ | {
                "HOME": tmp,
                "AGENT_PROFILES_CLAUDE_ROOT": str(root),
                "AGENT_PROFILES_SECURITY_BIN": "/usr/bin/true",
            }
            result = subprocess.run(
                YELO + ["pick", "--cli", "claude", "--json"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_expiring_window_beats_fuller_account(self):
        picked = self.run_pick({
            "a": {"five_hour": (50, 1830)},        # 30m left
            "b": {"five_hour": (0, 17430)},        # 4h50m left
        }, ["a", "b"])
        self.assertEqual(picked["name"], "a")

    def test_exhausted_window_loses_to_usable_account(self):
        picked = self.run_pick({
            "a": {"five_hour": (20, 630), "seven_day": (100, 174630)},
            "b": {"five_hour": (40, 14430), "seven_day": (40, 432630)},
        }, ["a", "b"])
        self.assertEqual(picked["name"], "b")


class Sessions(unittest.TestCase):
    """Rollouts are laid out the way Codex writes them: one per account home, under
    sessions/YYYY/MM/DD, named with the local start time and the session id."""

    def rollout(self, root, home, started, session_id, cwd, meta_type="session_meta"):
        day = pathlib.Path(root, home, "sessions", *started[:10].split("-"))
        day.mkdir(parents=True, exist_ok=True)
        record = {"timestamp": started, "type": meta_type,
                  "payload": {"id": session_id, "cwd": cwd}}
        day.joinpath(f"rollout-{started}-{session_id}.jsonl").write_text(
            json.dumps(record) + "\n"
        )

    def run_sessions(self, root, cwd, *flags):
        env = os.environ | {"AGENT_PROFILES_CODEX_GLOB_ROOT": str(root)}
        result = subprocess.run(
            YELO + ["sessions", "--cli", "codex", "--json", *flags],
            capture_output=True, text=True, env=env, cwd=cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def build(self, tmp):
        root = pathlib.Path(tmp)
        root.joinpath(".codex").mkdir()
        root.joinpath(".codex", "profile-label").write_text("base\n")
        root.joinpath(".codex-other").mkdir()
        work = root / "work"
        work.mkdir()
        return root, str(work)

    def test_newest_first_across_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            self.rollout(root, ".codex", "2026-08-01T09-00-00",
                         "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3", work)
            self.rollout(root, ".codex-other", "2026-08-02T09-00-00",
                         "019e08eb-4c3f-7610-b2f9-c7b4a062e1ee", work)
            rows = self.run_sessions(root, work)
            self.assertEqual([row["profile"] for row in rows], ["other", "base"])

    def test_cwd_narrows_and_all_widens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            self.rollout(root, ".codex", "2026-08-01T09-00-00",
                         "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3", work)
            self.rollout(root, ".codex", "2026-08-03T09-00-00",
                         "019e08eb-4d19-7181-b8c8-ed0f8584a1ea", str(root / "elsewhere"))
            self.assertEqual(len(self.run_sessions(root, work)), 1)
            self.assertEqual(len(self.run_sessions(root, work, "--all")), 2)

    def test_limit_caps_the_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            for day, session_id in (
                ("01", "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3"),
                ("02", "019e08eb-4c3f-7610-b2f9-c7b4a062e1ee"),
                ("03", "019e08eb-4d19-7181-b8c8-ed0f8584a1ea"),
            ):
                self.rollout(root, ".codex", f"2026-08-{day}T09-00-00", session_id, work)
            rows = self.run_sessions(root, work, "--limit", "2", "--all")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["started"], "2026-08-03T09-00-00")

    def test_non_meta_first_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            self.rollout(root, ".codex", "2026-08-01T09-00-00",
                         "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3", work,
                         meta_type="response_item")
            self.assertEqual(self.run_sessions(root, work, "--all"), [])


if __name__ == "__main__":
    unittest.main()
