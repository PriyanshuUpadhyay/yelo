"""C13 (`map-session`, every payload class of matrix M3) and C14 (`prune`), R5.

The classes are exercised against `resume.map_session` directly, because two of them --
a TTY on stdin, and a `mkdir` that fails -- cannot be arranged from the outside, and
because the byte, mode, and inode assertions are the point: a "touch only" that quietly
rewrote the file would still leave the right bytes behind. `test_map_session_is_silent`
then runs the same classes through the real command to prove the hook contract that holds
on every path: empty stdout, exit 0.
"""

import io
import json
import os
import shutil
import stat
import subprocess
import sys
import time

import pytest

from jello import resume

LABEL = "pri"
SID = "1234-abcd"
WANTED = b"pri\n"
DAY = 86400


class FakeStdin(io.StringIO):
    """A payload with a controllable isatty(), which StringIO reports as False."""

    def __init__(self, text="", tty=False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def home(tmp_path):
    directory = tmp_path / "home" / ".claude" / ".profiles" / ".session-map"
    directory.mkdir(parents=True)
    return tmp_path / "home"


def map_dir(home):
    return home / ".claude" / ".profiles" / ".session-map"


def entry(home, sid=SID):
    return map_dir(home) / sid


def names(home):
    directory = map_dir(home)
    return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []


def call(home, payload=None, label=LABEL, tty=False, text=None):
    body = text if text is not None else json.dumps(payload)
    env = {} if label is None else {"AGENT_PROFILE_LABEL": label}
    return resume.map_session(FakeStdin(body, tty), env, str(home))


def valid_payload(sid=SID, **extra):
    return {"session_id": sid, **extra}


def test_map_session_matrix(home):
    # --- class 1: valid payload -> write --------------------------------------------
    before = len(names(home))
    assert call(home, valid_payload()) == str(entry(home))
    assert entry(home).read_bytes() == WANTED
    assert stat.S_IMODE(entry(home).stat().st_mode) == 0o600
    # L1: at most one new file, and no temp file survives.
    assert names(home) == [SID]
    assert len(names(home)) <= before + 1

    # --- class 9: identical content -> touch only, never a rewrite -------------------
    old = time.time() - 10 * DAY
    os.utime(entry(home), (old, old))
    inode = entry(home).stat().st_ino
    assert call(home, valid_payload()) == str(entry(home))
    assert entry(home).stat().st_mtime > old
    assert entry(home).stat().st_ino == inode, "a touch must not replace the file"
    assert entry(home).read_bytes() == WANTED
    assert names(home) == [SID]

    # --- class 10: different content -> temp file and rename ------------------------
    entry(home).write_bytes(b"stale\nextra\n")
    inode = entry(home).stat().st_ino
    assert call(home, valid_payload()) == str(entry(home))
    assert entry(home).read_bytes() == WANTED
    assert entry(home).stat().st_ino != inode, "a rewrite must go through the rename"
    assert names(home) == [SID]

    # --- class 8: the existing entry is a symlink -> replaced, target untouched ------
    outside = home / "outside"
    outside.write_bytes(b"not the map\n")
    entry(home).unlink()
    entry(home).symlink_to(outside)
    assert call(home, valid_payload()) == str(entry(home))
    assert not entry(home).is_symlink()
    assert entry(home).read_bytes() == WANTED
    assert outside.read_bytes() == b"not the map\n"

    # --- class 2: agent_id present -> skip -------------------------------------------
    for agent_id in ("sub-1", "x"):
        assert call(home, valid_payload("nested-sid", agent_id=agent_id)) is None
        assert not entry(home, "nested-sid").exists()
    # Only a top-level *string* is an agent id, the way the hook's grep only matched one.
    assert call(home, valid_payload("null-agent", agent_id=None)) is not None
    assert entry(home, "null-agent").read_bytes() == WANTED

    # --- class 3: session_id empty or outside the charset -> skip --------------------
    for sid in ("", "has space", "slash/es", "dot.ted", "tab\there", "line\nfeed"):
        assert call(home, valid_payload(sid)) is None
    assert call(home, {"session_id": 17}) is None, "a non-string session_id is no id"
    assert call(home, {}) is None

    # --- class 4: stdin is a TTY, or empty -> skip ------------------------------------
    assert call(home, valid_payload("tty-sid"), tty=True) is None
    assert not entry(home, "tty-sid").exists()
    assert call(home, text="") is None

    # --- class 5: the payload is not JSON, or not an object -> skip -------------------
    for text in ("not json at all", "{", "[]", '"a string"', "null", "17"):
        assert call(home, text=text) is None

    # Board C1: only top-level keys are read, so a prompt that embeds JSON cannot shadow
    # the real session id.
    embedded = {"session_id": "real-sid", "prompt": json.dumps({"session_id": "evil"})}
    assert call(home, embedded) == str(entry(home, "real-sid"))
    assert entry(home, "real-sid").read_bytes() == WANTED
    assert not entry(home, "evil").exists()

    # --- class 6: the label is unset, empty, or outside the charset -> skip -----------
    for label in (None, "", "-leading", "_leading", ".leading", "has space", "sl/ash"):
        assert call(home, valid_payload("label-sid"), label=label) is None
        assert not entry(home, "label-sid").exists()

    # --- class 7: a symlinked profile root or map directory -> skip -------------------
    elsewhere = home / "elsewhere"
    (elsewhere / ".session-map").mkdir(parents=True)
    root = home / ".claude" / ".profiles"
    real_root = home / "real-profiles"
    root.rename(real_root)
    root.symlink_to(elsewhere)
    assert call(home, valid_payload("linked-root")) is None
    assert not (elsewhere / ".session-map" / "linked-root").exists()
    root.unlink()
    real_root.rename(root)

    linked_map = map_dir(home)
    real_map = home / ".claude" / ".profiles" / "real-map"
    linked_map.rename(real_map)
    linked_map.symlink_to(elsewhere / ".session-map")
    assert call(home, valid_payload("linked-map")) is None
    assert not (elsewhere / ".session-map" / "linked-map").exists()
    linked_map.unlink()
    real_map.rename(linked_map)

    # --- class 11: mkdir fails -> skip -----------------------------------------------
    blocked = home / "blocked"
    (blocked / ".claude").mkdir(parents=True)
    (blocked / ".claude" / ".profiles").write_text("a file where the root should be\n")
    assert call(blocked, valid_payload()) is None
    assert (blocked / ".claude" / ".profiles").read_text().startswith("a file")

    # Nothing above left a temp file behind.
    assert [name for name in names(home) if name.startswith(".tmp.")] == []


PAYLOAD_CLASSES = (
    ("valid", json.dumps({"session_id": SID}), LABEL),
    ("agent_id", json.dumps({"session_id": SID, "agent_id": "sub"}), LABEL),
    ("bad sid", json.dumps({"session_id": "no good"}), LABEL),
    ("no sid", json.dumps({"prompt": "hello"}), LABEL),
    ("not json", "definitely not json", LABEL),
    ("empty", "", LABEL),
    ("no label", json.dumps({"session_id": SID}), None),
    ("bad label", json.dumps({"session_id": SID}), "-nope"),
)


@pytest.mark.parametrize("name,payload,label", PAYLOAD_CLASSES, ids=[c[0] for c in PAYLOAD_CLASSES])
def test_map_session_is_silent(home, name, payload, label):
    """M3's last row, through the real command: hook stdout is injected into the session
    context and a non-zero hook fails the session, so neither may ever happen."""
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    if label is not None:
        env["AGENT_PROFILE_LABEL"] = label
    result = subprocess.run(
        [sys.executable, "-m", "jello.cli", "resume", "map-session"],
        input=payload, capture_output=True, text=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def age(path, days):
    when = time.time() - days * DAY
    os.utime(path, (when, when))


def test_prune(home):
    """A live session id is never pruned however old its entry is: herdr's restore may
    replay a session idle for weeks, and losing the entry means the wrapper's strict
    "--profile NAME is required" error."""
    for name, days in (("old-gone", 40), ("old-live", 40), ("young", 2), ("edge", 29)):
        path = entry(home, name)
        path.write_bytes(WANTED)
        age(path, days)

    session_json = home / ".config" / "herdr" / "session.json"
    session_json.parent.mkdir(parents=True)
    session_json.write_text(json.dumps({"sessions": [{"id": "old-live"}]}))

    assert resume.prune(str(home), str(session_json)) == 1
    assert names(home) == ["edge", "old-live", "young"]

    # A missing listing means nothing is live, so age alone decides.
    assert resume.prune(str(home), str(home / "no-such-file.json")) == 1
    assert names(home) == ["edge", "young"]

    # Nothing left is old enough, so a second run removes nothing.
    assert resume.prune(str(home), str(session_json)) == 0
    assert names(home) == ["edge", "young"]


def test_prune_leaves_a_symlinked_entry_and_a_symlinked_map(home):
    """`find -type f` reports a symlink as a link, so the housekeeping script never
    deletes one; nor does prune."""
    outside = home / "outside"
    outside.write_bytes(WANTED)
    link = entry(home, "linked")
    link.symlink_to(outside)
    age(outside, 40)
    assert resume.prune(str(home), str(home / "none.json")) == 0
    assert link.is_symlink()

    real_map = home / ".claude" / ".profiles" / "real-map"
    map_dir(home).rename(real_map)
    map_dir(home).symlink_to(real_map)
    assert resume.prune(str(home), str(home / "none.json")) == 0


@pytest.mark.parametrize("linked", ["profiles", "session-map"])
def test_prune_refuses_symlinked_root(home, tmp_path, linked):
    """Review finding F1: prune unlinks files, so it owes the same symlink refusal
    map_session makes. Following a symlinked root would delete outside this HOME."""
    outside = tmp_path / "outside"
    (outside / ".session-map").mkdir(parents=True)
    stranger = outside / ".session-map" / "old-gone"
    stranger.write_bytes(WANTED)
    age(stranger, 40)

    root = home / ".claude" / ".profiles"
    if linked == "profiles":
        shutil.rmtree(root)
        root.symlink_to(outside)
    else:
        shutil.rmtree(map_dir(home))
        map_dir(home).symlink_to(outside / ".session-map")

    assert resume.prune(str(home), str(home / "no-such-file.json")) == 0
    assert stranger.exists(), "prune followed a symlink out of the state tree"
    assert stranger.read_bytes() == WANTED

    # The command says the same thing an absent map says: nothing removed, exit 0.
    result = subprocess.run(
        [sys.executable, "-m", "jello.cli", "resume", "prune",
         "--session-json", str(home / "none.json")],
        capture_output=True, text=True,
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "resume prune: removed 0 entries\n"
    assert stranger.exists()


def test_prune_reports_through_the_cli(home):
    path = entry(home, "old-gone")
    path.write_bytes(WANTED)
    age(path, 40)
    result = subprocess.run(
        [sys.executable, "-m", "jello.cli", "resume", "prune",
         "--session-json", str(home / "none.json")],
        capture_output=True, text=True,
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "resume prune: removed 1 entry\n"
    assert names(home) == []
