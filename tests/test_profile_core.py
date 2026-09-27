#!/usr/bin/env python3
"""Profile selection and session tests run through the Rust CLI."""
import hashlib
import json
import os
import pathlib
import shlex
import subprocess
import tempfile
import time
import unittest

from conftest import fixture_env, repo_root, run_yelo

YELO = (shlex.split(os.environ["YELO_CMD"]) if os.environ.get("YELO_CMD") else
        [str(repo_root() / "target/debug/yelo")]) + ["profile"]

def test_keychain_service_shared(tmp_path):
    home = tmp_path / "home"
    for name in ("pri", "work"):
        (home / ".claude" / ".profiles" / name).mkdir(parents=True)
    identity = str(home / ".claude-pri")
    service = "Claude Code-credentials-" + hashlib.sha256(identity.encode()).hexdigest()[:8]
    security = tmp_path / "security"
    log = tmp_path / "security.log"
    security.write_text('''#!/usr/bin/env python3
import os, sys
with open(os.environ["FAKE_SECURITY_LOG"], "a") as handle:
    handle.write("\\t".join(sys.argv[1:]) + "\\n")
sys.exit(0 if sys.argv[sys.argv.index("-s") + 1] == os.environ["FAKE_SERVICE"] else 1)
''')
    security.chmod(0o755)
    env = fixture_env(home, security_bin=str(security))
    env.update(FAKE_SECURITY_LOG=str(log), FAKE_SERVICE=service)
    result = run_yelo(["profile", "list", "--cli", "claude", "--json"], env)
    assert result.returncode == 0, result.stderr
    rows = {row["name"]: row for row in json.loads(result.stdout)}
    assert rows["pri"]["signed_in"] is True
    assert rows["work"]["signed_in"] is False
    calls = [line.split("\t") for line in log.read_text().splitlines()]
    assert any(call[call.index("-s") + 1] == service for call in calls)
    assert all(call[call.index("-a") + 1] == env.get("USER", "") for call in calls)

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

    def run_owner(self, root, cwd, *flags, expect=0):
        env = os.environ | {"AGENT_PROFILES_CODEX_GLOB_ROOT": str(root)}
        result = subprocess.run(
            YELO + ["owner", "--cli", "codex", "--json", *flags],
            capture_output=True, text=True, env=env, cwd=cwd,
        )
        self.assertEqual(result.returncode, expect, result.stderr)
        return json.loads(result.stdout) if expect == 0 else result.stderr

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

    def test_owner_is_the_home_holding_the_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            self.rollout(root, ".codex", "2026-08-01T09-00-00",
                         "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3", work)
            self.rollout(root, ".codex-other", "2026-08-02T09-00-00",
                         "019e08eb-4c3f-7610-b2f9-c7b4a062e1ee", str(root / "elsewhere"))
            archived = root / ".codex-other" / "archived_sessions"
            archived.mkdir()
            archived.joinpath(
                "rollout-2026-07-01T09-00-00-019e08eb-4d19-7181-b8c8-ed0f8584a1ea.jsonl"
            ).write_text("")
            owner = self.run_owner(root, work, "019E08EB-4C3F-7610-B2F9-C7B4A062E1EE")
            self.assertEqual((owner["name"], owner["dir"]), ("other", str(root / ".codex-other")))
            self.assertEqual(self.run_owner(root, work, "019e08eb-4d19-7181-b8c8-ed0f8584a1ea")["name"],
                             "other")
            self.assertEqual(self.run_owner(root, work, "--last")["name"], "base")
            self.assertEqual(self.run_owner(root, work, "--last", "--all")["name"], "other")
            self.assertIn("is not in any account",
                          self.run_owner(root, work, "019e08eb-0000-7000-8000-000000000000", expect=1))
            self.assertIn("no sessions found",
                          self.run_owner(root, tmp, "--last", expect=1))

    def test_non_meta_first_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, work = self.build(tmp)
            self.rollout(root, ".codex", "2026-08-01T09-00-00",
                         "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3", work,
                         meta_type="response_item")
            self.assertEqual(self.run_sessions(root, work, "--all"), [])

if __name__ == "__main__":
    unittest.main()
