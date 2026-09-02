#!/usr/bin/env python3
"""The resolver's own tests, ported from ~/.claude/scripts/test_agent_profiles.py.

Only two things changed: the module is imported as `jello.profile.core` instead of being
loaded from a file path, and the subprocess runs `jello profile ...` instead of the script.
Every assertion is the original one.
"""
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

from jello.profile import core as agent_profiles

JELLO = [sys.executable, "-m", "jello.cli", "profile"]


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


class Pick(unittest.TestCase):
    def run_pick(self, usage_rows, names):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "profiles")
            for name in names:
                root.joinpath(name).mkdir(parents=True)
            usage_data = pathlib.Path(tmp, "usage-data")
            usage_data.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % json.dumps(usage_rows))
            usage_data.chmod(usage_data.stat().st_mode | stat.S_IXUSR)
            env = os.environ | {
                "AGENT_PROFILES_CLAUDE_ROOT": str(root),
                "AGENT_PROFILES_USAGE_DATA": str(usage_data),
                "AGENT_PROFILES_SECURITY_BIN": "/usr/bin/true",
            }
            result = subprocess.run(
                JELLO + ["pick", "--cli", "claude", "--json"],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    @staticmethod
    def row(name, window, pct, reset):
        return {"label": f"cl·{name}", "provider": "claude", "window": window,
                "state": "ok", "pct": pct, "reset": reset}

    def test_expiring_window_beats_fuller_account(self):
        picked = self.run_pick([
            self.row("a", "5h", 50, "30m"),
            self.row("b", "5h", 0, "4h50m"),
        ], ["a", "b"])
        self.assertEqual(picked["name"], "a")

    def test_exhausted_window_loses_to_usable_account(self):
        picked = self.run_pick([
            self.row("a", "5h", 20, "10m"),
            self.row("a", "7d", 100, "2d0h"),
            self.row("b", "5h", 40, "4h0m"),
            self.row("b", "7d", 40, "5d0h"),
        ], ["a", "b"])
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
            JELLO + ["sessions", "--cli", "codex", "--json", *flags],
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
