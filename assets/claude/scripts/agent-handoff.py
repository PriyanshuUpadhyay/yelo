#!/usr/bin/env python3

"""Durable result artifacts for visible-pane agent workflows.

Deliverable state and occupant state are independent. A valid artifact bound to a
dispatch this run actually made completes a seat by itself; herdr lifecycle state only
decides how quickly the chair wakes, whether a seat needs intervention, and when a pane
can be reused. Terminal text is never consulted for completion — it hard-wraps, scrolls
out of the retained window, and echoes markers before the work exists.

Platform contract: POSIX only. Publication needs a local, hard-link-capable filesystem
shared by the chair and every pane, and the collector needs an `AF_UNIX` herdr socket.
Remote panes and the Windows named-pipe transport are out of scope; both fail loudly.

Subcommands: `publish` (worker side), `dispatch` / `verify` / `collect` / `prompt` /
`pending` / `digest` (chair side).
"""

# Workers run `publish` with whatever `python3` their pane shell resolves — /usr/bin/python3
# is 3.9 on macOS — so this module stays 3.9-compatible and defers annotations.
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import errno
import fcntl
import hashlib
import itertools
import json
import os
import re
import select
import shlex
import socket
import sys
import threading
import time


SCHEMA_VERSION = 1
ENVELOPE_FIELDS = (
    "schema_version",
    "run_id",
    "workflow",
    "seat_id",
    "round",
    "attempt",
    "outcome",
    "input_digest",
    "payload",
    "payload_digest",
)
OUTCOMES = ("ok", "blocked", "failed", "recovered")
# Kinds whose panes run sandboxed: a Codex `workspace-write` seat may write only its cwd,
# /tmp, $TMPDIR and declared roots, so a home-state run dir fails at `publish` — after the
# worker already spent its whole turn (ORCH-01).
SANDBOXED_KINDS = ("codex",)
ARTIFACT_DIR = "artifacts"
CHECKPOINT_DIR = "checkpoints"
# Breadcrumbs for seats this run left live, under the bus root rather than a run
# dir: the stop gate asks about the session, not about one run it would have to find.
PENDING_DIR = "pending"
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HELPER = os.path.abspath(__file__)
HERDR_BUS = os.path.join(os.path.dirname(HELPER), "herdr-bus.py")
PUBLISH_COMMAND = os.environ.get("AGENT_HANDOFF_COMMAND", "agent-handoff")
BUS_COMMAND = os.environ.get("HERDR_BUS_COMMAND", "herdr-bus")

COMPLETED = "completed"
DECLARED_BLOCKED = "declared_blocked"
DECLARED_FAILED = "declared_failed"
BLOCKED = "blocked"
UNKNOWN = "unknown"
EXITED = "exited"
REPLACED = "replaced"
READY_WITHOUT_ARTIFACT = "ready_without_artifact"
PROMPT_STALLED = "prompt_stalled"
RETRY_EXHAUSTED = "retry_exhausted"
OWNED_ELSEWHERE = "owned_elsewhere"
CHECKPOINT_CONFLICT = "checkpoint_conflict"
DISPATCHED = "dispatched"
BUSY = "busy"

CONNECT_POLL_SECONDS = 0.025
ARTIFACT_POLL_SECONDS = 0.25


class CheckpointError(Exception):
    """A checkpoint that cannot be trusted; the seat fails closed instead of dispatching."""

STATE_BY_OUTCOME = {
    "ok": COMPLETED,
    "recovered": COMPLETED,
    "blocked": DECLARED_BLOCKED,
    "failed": DECLARED_FAILED,
}

SETTLE_STATES = ("idle", "done", "blocked")
UPTAKE_STATES = ("working",)
LIVE_STATES = ("idle", "done")
UPTAKE_TIMEOUT_MS = 6_000
MAX_STALLED_RETRY_MS = 10_000
STALLED_RETRY_INITIAL_MS = 1_000
STALLED_RETRY_CAP_MS = 4_000

SETTLED = "settled"
LANDED_WORKING = "landed_and_working"
LANDED_UNCONFIRMED = "landed_unconfirmed"
NEVER_LANDED = "never_landed"
SEAT_UNSETTLED = "seat_unsettled"

# The exit code answers exactly one question: was the text delivered to this seat?
# `blocked` and `landed_unconfirmed` are deliveries — the seat still needs attention, and
# the caller reads `outcome` for that, but neither may read as "safe to send again".
LANDED_OUTCOMES = (SETTLED, LANDED_WORKING, LANDED_UNCONFIRMED, BLOCKED)
UNSENT_OUTCOMES = (SEAT_UNSETTLED, NEVER_LANDED)

# herdr's two distinct answers to `agent prompt --wait`. They look alike to a chair
# reading exit codes and mean opposite things about whether the text reached a turn.
WAIT_TIMEOUT_CODE = "timeout"
PROMPT_STALLED_CODE = "agent_prompt_stalled"

# A codex TUI can report a first prompt delivered while its input never took the text:
# the uptake wait sees no turn and the seat still shows its pre-prompt settled state.
UPTAKE_UNOBSERVED_CODE = "uptake_unobserved_preexisting_settle"

# Precedence when signals about one seat disagree. Applies to the collector and the
# ad-hoc prompt path alike; both implement these three steps in this order.
#   1. A delivery fact herdr answered OUR OWN call with — `agent_prompt_stalled` — decides
#      the delivery axis and is sticky. It says our text reached no turn, so nothing that
#      happens afterwards can turn it into a misdelivery; churn is recorded as identity
#      metadata beside it, never promoted over it.
#   2. Otherwise validate identity first. A status read describes whoever holds the target
#      NOW, so it says nothing about our seat until the occupant is confirmed unchanged.
#   3. Only then apply status evidence, including a `blocked` that any earlier read caught.
# The asymmetry between 1 and 2 is the point: a prompt reply is evidence about our call,
# while a status read is evidence about a pane, and only the latter needs scoping.
POST_PROMPT_STALLED = "stalled"
POST_PROMPT_INDETERMINATE = "indeterminate"
POST_PROMPT_REPLACED = "replaced"
POST_PROMPT_UNVERIFIABLE = "unverifiable"
POST_PROMPT_BLOCKED = "blocked"
POST_PROMPT_OPEN = "open"

OCCUPANT_SAME = "same"
OCCUPANT_CHANGED = "changed"
OCCUPANT_UNPINNED = "unpinned"
OCCUPANT_UNVERIFIABLE = "unverifiable"
OCCUPANT_UPGRADED = "upgraded"

_TEMP_SEQUENCE = itertools.count()


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_identity(label, value):
    if not isinstance(value, str) or not IDENTITY_PATTERN.match(value):
        raise ValueError(f"{label} {value!r} is not a safe identity")
    return value


def check_digest(label, value):
    if not isinstance(value, str) or not DIGEST_PATTERN.match(value):
        raise ValueError(f"{label} {value!r} is not a sha256 digest")
    return value


@dataclasses.dataclass(frozen=True)
class Expectation:
    """The dispatch an artifact must be bound to before it can be accepted.

    `attempts` is the set of attempt ids this run actually dispatched (or the single
    already-accepted attempt). `None` means "unconstrained" and is only for human
    diagnosis — the collector always passes a concrete set, so an artifact nobody
    dispatched can never complete a seat.
    """

    run_id: str
    workflow: str
    seat_id: str
    round_id: int
    input_digest: str
    attempts: frozenset | None = None


@dataclasses.dataclass(frozen=True)
class Validation:
    ok: bool
    reason: str | None = None
    document: dict | None = None


def envelope(*, run_id, workflow, seat_id, round_id, attempt, outcome, input_digest, payload):
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "workflow": workflow,
        "seat_id": seat_id,
        "round": round_id,
        "attempt": attempt,
        "outcome": outcome,
        "input_digest": input_digest,
        "payload": payload,
        "payload_digest": digest(payload),
    }


def artifact_name(seat_id, round_id, attempt):
    return f"{seat_id}.r{round_id}.a{attempt}.json"


def artifact_dir(run_dir):
    return os.path.join(run_dir, ARTIFACT_DIR)


def _write_durably(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path):
    # Best effort: the artifact is already published by the time this runs, and some
    # filesystems refuse a directory fsync.
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish(run_dir, document):
    """Write `document` to its per-attempt path atomically, never clobbering an attempt.

    Raises FileExistsError when the attempt was already published, so a retry that races
    a late original publication cannot destroy the original.
    """
    check_identity("seat id", document["seat_id"])
    directory = artifact_dir(run_dir)
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(
        directory, artifact_name(document["seat_id"], document["round"], document["attempt"])
    )
    temp = os.path.join(
        directory,
        f".{os.path.basename(final)}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp",
    )
    try:
        _write_durably(temp, json.dumps(document, indent=2, sort_keys=True) + "\n")
        # link() publishes atomically AND fails on an existing attempt; rename() would
        # silently overwrite accepted work.
        os.link(temp, final)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
    _fsync_directory(directory)
    return final


def validate(raw, expect):
    """Pure artifact check: no filesystem, no clock, no network."""
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return Validation(False, "unparsable")
    if not isinstance(document, dict):
        return Validation(False, "not_an_object")
    missing = [field for field in ENVELOPE_FIELDS if field not in document]
    if missing:
        return Validation(False, "missing:" + ",".join(missing))
    if document["schema_version"] != SCHEMA_VERSION:
        return Validation(False, f"schema_version:{document['schema_version']}")
    for field in ("run_id", "workflow", "seat_id", "outcome", "input_digest",
                  "payload", "payload_digest"):
        if not isinstance(document[field], str):
            return Validation(False, f"type:{field}")
    for field in ("round", "attempt"):
        value = document[field]
        if not isinstance(value, int) or isinstance(value, bool):
            return Validation(False, f"type:{field}")
    if document["outcome"] not in OUTCOMES:
        return Validation(False, f"outcome:{document['outcome']}")
    if document["attempt"] < 1 or document["round"] < 0:
        return Validation(False, "identity_range")
    for field, expected in (
        ("run_id", expect.run_id),
        ("workflow", expect.workflow),
        ("seat_id", expect.seat_id),
        ("round", expect.round_id),
        ("input_digest", expect.input_digest),
    ):
        if document[field] != expected:
            return Validation(False, f"misattributed:{field}")
    if expect.attempts is not None and document["attempt"] not in expect.attempts:
        return Validation(False, f"undispatched:{document['attempt']}")
    if document["payload_digest"] != digest(document["payload"]):
        return Validation(False, "payload_digest")
    return Validation(True, None, document)


def scan(run_dir, expect):
    """Highest-attempt valid artifact for a seat, plus why every other candidate lost."""
    directory = artifact_dir(run_dir)
    prefix = f"{expect.seat_id}.r"
    accepted = None
    rejected = []
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return None, rejected
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as error:
            rejected.append((path, f"unreadable:{error.errno}"))
            continue
        checked = validate(raw, expect)
        if not checked.ok:
            rejected.append((path, checked.reason))
            continue
        if accepted is None or checked.document["attempt"] > accepted[1]["attempt"]:
            accepted = (path, checked.document)
    return accepted, rejected


class Checkpoint:
    """Durable per-seat run state, so a resumed chair knows what it already did.

    Records every dispatched attempt with the occupant it was pinned to, and the
    acceptance once one is made. An acceptance is sticky: later attempts published by a
    seat that already had work accepted never move the accepted attempt.
    """

    def __init__(self, run_dir, run_id, workflow, seat_id, round_id, input_digest):
        self.directory = os.path.join(run_dir, CHECKPOINT_DIR)
        self.path = os.path.join(self.directory, f"{seat_id}.r{round_id}.json")
        self.lock_path = f"{self.path}.lock"
        self.identity = {
            "run_id": run_id,
            "workflow": workflow,
            "seat_id": seat_id,
            "round": round_id,
            "input_digest": input_digest,
        }
        self._lock_fd = None

    def acquire(self):
        """Exclusive dispatch ownership of this seat, across processes.

        Two collectors on one run dir would otherwise both read an empty checkpoint,
        both pick attempt 1, and both prompt the same pane. Whoever loses the lock may
        still read and accept artifacts; it must not dispatch.
        """
        os.makedirs(self.directory, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def release(self):
        if self._lock_fd is None:
            return
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(self._lock_fd)
            self._lock_fd = None

    def load(self):
        """Fail closed: a checkpoint we cannot trust never becomes a fresh dispatch."""
        try:
            with open(self.path, encoding="utf-8") as handle:
                raw = handle.read()
        except FileNotFoundError:
            state = dict(self.identity)
            state.update({"dispatched": [], "accepted": None, "state": None})
            return state
        except OSError as error:
            raise CheckpointError(f"unreadable:{error.errno}")
        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            raise CheckpointError("unparsable")
        if not isinstance(state, dict):
            raise CheckpointError("not_an_object")
        for key, value in self.identity.items():
            if state.get(key) != value:
                raise CheckpointError(f"identity_mismatch:{key}")
        if not isinstance(state.get("dispatched"), list):
            raise CheckpointError("dispatched")
        attempts = []
        for entry in state["dispatched"]:
            if not isinstance(entry, dict):
                raise CheckpointError("dispatched_entry")
            attempt = entry.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise CheckpointError("dispatched_attempt")
            if attempt in attempts:
                raise CheckpointError(f"dispatched_duplicate:{attempt}")
            attempts.append(attempt)
        accepted = state.get("accepted")
        if accepted is not None:
            if not isinstance(accepted, dict):
                raise CheckpointError("accepted")
            attempt = accepted.get("attempt")
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
                raise CheckpointError("accepted_attempt")
            if not isinstance(accepted.get("path"), str) or not accepted["path"]:
                raise CheckpointError("accepted_path")
            if not DIGEST_PATTERN.match(str(accepted.get("payload_digest"))):
                raise CheckpointError("accepted_payload_digest")
            if accepted.get("outcome") not in OUTCOMES:
                raise CheckpointError("accepted_outcome")
            # Structural validity is not enough: an accepted entry whose attempt was never
            # dispatched (or dispatched twice) would let a hand-written checkpoint turn a
            # stray artifact into accepted work, which is the hole the dispatch record
            # exists to close.
            if attempts.count(attempt) != 1:
                raise CheckpointError(f"accepted_attempt_undispatched:{attempt}")
        state.setdefault("state", None)
        return state

    def save(self, state):
        os.makedirs(self.directory, exist_ok=True)
        temp = f"{self.path}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp"
        try:
            _write_durably(temp, json.dumps(state, indent=2, sort_keys=True) + "\n")
            os.replace(temp, self.path)
        except OSError:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass
            raise
        _fsync_directory(self.directory)
        return state

    def record_dispatch(self, state, attempt, occupant):
        state["dispatched"].append({
            "attempt": attempt,
            "occupant": occupant,
            "dispatched_at": time.time(),
        })
        return self.save(state)

    def record_acceptance(self, state, document, path):
        state["accepted"] = {
            "attempt": document["attempt"],
            "path": path,
            "outcome": document["outcome"],
            "payload_digest": document["payload_digest"],
            "accepted_at": time.time(),
        }
        state["state"] = STATE_BY_OUTCOME[document["outcome"]]
        return self.save(state)

    def record_state(self, state, seat_state):
        state["state"] = seat_state
        return self.save(state)


@dataclasses.dataclass(frozen=True)
class Seat:
    seat_id: str
    target: str
    prompt: str
    input_digest: str
    max_attempts: int = 1
    # No kind's `idle` proves a turn ended — integrations supply session identity, while
    # status still comes from screen manifests and any known-agent screen can fall back
    # to idle. A workflow that accepts the duplicate-turn risk opts in per seat.
    allow_idle_retry: bool = False


@dataclasses.dataclass(frozen=True)
class Reply:
    """Transport-normalized answer, so the collector never parses herdr envelopes."""

    ok: bool
    code: str | None = None
    status: str | None = None
    occupant: str | None = None
    kind: str | None = None
    ready: bool | None = None


class SocketTransport:
    """Herdr Socket API client. One connection per call, all connections cancellable."""

    def __init__(self, path=None, budget=None):
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("agent-handoff requires an AF_UNIX herdr socket (POSIX only)")
        self.path = path or os.environ.get("HERDR_SOCKET_PATH")
        if not self.path:
            raise RuntimeError("HERDR_SOCKET_PATH is missing; cannot reach the herdr server")
        # Seconds left in the caller's global deadline, or None for "no deadline". Every
        # blocking step — including connect — is bounded by it.
        self.budget = budget
        self._lock = threading.Lock()
        self._open = set()
        self._cancelled = False
        self._sequence = 0

    def prompt(self, target, text, until, timeout_ms):
        wait = {"until": list(until), "timeout_ms": timeout_ms}
        return _reply(*self._call("agent.prompt",
                                  {"target": target, "text": text, "wait": wait}, timeout_ms))

    def wait(self, target, until, timeout_ms):
        return _reply(*self._call(
            "agent.wait", {"target": target, "until": list(until), "timeout_ms": timeout_ms},
            timeout_ms,
        ))

    def get(self, target):
        code, result = self._call("agent.get", {"target": target}, self._budget_ms(5000))
        reply = _reply(code, result)
        agent = _find_agent(result) if reply.ok else None
        if agent and reply.occupant is None:
            reply = dataclasses.replace(reply, occupant=self._sessionless_occupant(target, agent))
        return reply

    def process_group(self, pane_id):
        """The pane's foreground process group, or None when it cannot be read."""
        code, result = self._call("pane.process_info", {"pane_id": pane_id},
                                  self._budget_ms(5000))
        if code:
            return None
        return ((result or {}).get("process_info") or {}).get("foreground_process_group_id")

    def cancel_all(self):
        """Reap every owned wait — the chair, not a detached shell, owns these.

        The transport is single-use: later calls return `cancelled` immediately.
        """
        with self._lock:
            self._cancelled = True
            open_sockets = list(self._open)
        for sock in open_sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _sessionless_occupant(self, target, agent):
        """Identity for kinds herdr has no session reference for.

        `terminal_id` names the terminal, not the occupant, so a replacement in the same
        pane would compare equal. The foreground process group is the real occupant.
        herdr has no call returning both facts at once, so the agent snapshot is re-read
        afterwards and the identity is discarded unless it did not move underneath us —
        otherwise one occupant's status could be paired with another's process group.
        Unidentifiable is reported as None, which the collector treats as "do not retry".
        """
        pane_id = agent.get("pane_id")
        if not pane_id:
            return None
        group = self.process_group(pane_id)
        if group is None:
            return None
        recheck_code, recheck = self._call("agent.get", {"target": target},
                                           self._budget_ms(5000))
        if recheck_code:
            return None
        after = _find_agent(recheck)
        if not after or not _same_snapshot(agent, after):
            return None
        return f"{pane_id}:pgid{group}"

    def _budget_ms(self, default_ms):
        remaining = self.budget() if self.budget else None
        if remaining is None:
            return default_ms
        return max(min(default_ms, int(remaining * 1000)), 1)

    def _connect(self, sock, budget_seconds):
        """Connect without ever outliving the deadline or ignoring a cancellation.

        A blocking connect cannot be interrupted by `shutdown` — a saturated AF_UNIX
        accept queue would hold the chair well past its global deadline — so the connect
        runs non-blocking and is polled against both the budget and the cancel flag.
        Returns an error code, or None once connected.
        """
        deadline = time.monotonic() + max(budget_seconds, 0.0)
        sock.setblocking(False)
        try:
            sock.connect(self.path)
        except BlockingIOError:
            pass
        except OSError as error:
            if error.errno not in (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN):
                return f"transport:{error}"
        while True:
            if self._cancelled:
                return "cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "connect_timeout"
            _, writable, _ = select.select([], [sock], [],
                                           min(remaining, CONNECT_POLL_SECONDS))
            if not writable:
                continue
            code = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if code in (errno.EINPROGRESS, errno.EALREADY, errno.EAGAIN):
                continue
            if code:
                return f"transport:{os.strerror(code)}"
            return None

    def _call(self, method, params, timeout_ms):
        """Returns (error code or None, raw result or None)."""
        with self._lock:
            if self._cancelled:
                return "cancelled", None
            self._sequence += 1
            request_id = f"handoff-{os.getpid()}-{self._sequence}"
            # Created AND registered under the same lock: a cancel_all between creation
            # and registration would otherwise leave this socket blocking past the
            # global deadline.
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._open.add(sock)
        try:
            connect_code = self._connect(sock, timeout_ms / 1000.0)
            if connect_code:
                return connect_code, None
            sock.settimeout(max(timeout_ms / 1000.0, 1.0) + 5.0)
            stream = sock.makefile("rwb")
            stream.write(
                (json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
                .encode("utf-8")
            )
            stream.flush()
            while True:
                line = stream.readline()
                if not line:
                    return ("cancelled" if self._cancelled else "closed"), None
                message = json.loads(line)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    return message["error"].get("code", "error"), None
                return None, message.get("result")
        except (OSError, json.JSONDecodeError) as error:
            return ("cancelled" if self._cancelled else f"transport:{error}"), None
        finally:
            with self._lock:
                self._open.discard(sock)
            sock.close()


def _find_agent(value):
    if isinstance(value, dict):
        if "agent_status" in value and "pane_id" in value:
            return value
        for child in value.values():
            found = _find_agent(child)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find_agent(child)
            if found:
                return found
    return None


def _occupant_identity(fingerprint):
    """Split an occupant fingerprint into the pane it names and the scheme that named it.

    `get` reports the herdr session where the integration supplies one and the pane's
    foreground process group otherwise, so the two forms are not comparable as strings.
    """
    marker = fingerprint.find(":session:")
    if marker != -1:
        return fingerprint[:marker], "session"
    marker = fingerprint.rfind(":pgid")
    if marker != -1:
        return fingerprint[:marker], "pgid"
    return fingerprint, "opaque"


def _classify_occupant(before, after):
    """Compare two occupant fingerprints without deciding the ambiguous cases.

    Never pinned an identity to begin with, so there is nothing to contradict, is
    UNPINNED. Losing an identity we did hold is UNVERIFIABLE — not proof of replacement,
    but not proof of survival either. A same-pane `pgid`->`session` flip is UPGRADED: it
    is what a freshly spawned pane looks like once its integration registers, and only a
    process-group check can tell that from a new occupant that registered a session.
    """
    if not before:
        return OCCUPANT_UNPINNED
    if not after:
        return OCCUPANT_UNVERIFIABLE
    if before == after:
        return OCCUPANT_SAME
    pane_before, scheme_before = _occupant_identity(before)
    pane_after, scheme_after = _occupant_identity(after)
    if pane_before != pane_after:
        return OCCUPANT_CHANGED
    if scheme_before == "pgid" and scheme_after == "session":
        return OCCUPANT_UPGRADED
    return OCCUPANT_CHANGED


def _occupant_verdict(transport, before, after):
    """Resolve `_classify_occupant`, paying for a process-group read only when needed.

    The scheme flip is forgiven ONLY while the pane's foreground process group still
    matches the one the original fingerprint named. Forgiving it unconditionally would
    let an occupant that died and was replaced by an agent which then registered a
    session read as delivery to the original seat.
    """
    verdict = _classify_occupant(before, after)
    if verdict != OCCUPANT_UPGRADED:
        return verdict
    pane, _ = _occupant_identity(after)
    group = transport.process_group(pane)
    if group is None:
        return OCCUPANT_UNVERIFIABLE
    return OCCUPANT_SAME if f"{pane}:pgid{group}" == before else OCCUPANT_CHANGED


def classify_post_prompt(reply, churn, *also_seen):
    """The precedence above, evaluated once for both dispatch paths.

    `reply` is what `agent.prompt` answered, `churn` the occupant verdict across that
    prompt, and `also_seen` any other read of the same seat whose status still counts.
    This decides ONLY which rule fires; each caller maps the answer onto its own
    vocabulary. Keeping the decision here rather than in both callers is deliberate — the
    two drifted apart twice while the rule lived in two places.
    """
    if reply.code == PROMPT_STALLED_CODE:
        return POST_PROMPT_STALLED
    if reply.code and reply.code != WAIT_TIMEOUT_CODE:
        # The call itself failed, so herdr never told us whether the text was submitted —
        # a closed socket can drop the response to a prompt it already delivered. That is
        # the absence of a delivery fact, and it shares tier 1 with `stalled` because no
        # identity finding can settle it: reporting `replaced` here would claim a
        # misdelivery of text that may never have been sent, and retrying could double it.
        return POST_PROMPT_INDETERMINATE
    if churn == OCCUPANT_CHANGED:
        return POST_PROMPT_REPLACED
    if churn == OCCUPANT_UNVERIFIABLE:
        return POST_PROMPT_UNVERIFIABLE
    if _blocked_seen(reply, *also_seen):
        return POST_PROMPT_BLOCKED
    return POST_PROMPT_OPEN


def _blocked_seen(*replies):
    """Did any of these reads catch the seat asking a question?

    herdr's `idle` is "nothing said otherwise", not "the turn finished", so a later
    screen-derived read can silently lose a `blocked` an earlier one caught. `blocked` is
    a positive signal and a fallback is the absence of one, so within a single decision
    the positive read wins no matter which call produced it. Every place that reads a
    status from more than one call — a wait reply then a get, a prompt reply then a get —
    has to ask this rather than trust the last read.
    """
    return any(reply is not None and reply.status == "blocked" for reply in replies)


def _same_snapshot(before, after):
    """Did the pane's occupant stay put between two `agent.get` reads?"""
    return all(
        before.get(field) == after.get(field)
        for field in ("pane_id", "terminal_id", "agent", "agent_status", "state_change_seq")
    )


def _reply(code, result):
    """`agent.wait` answers with either an agent info or a lifecycle event envelope."""
    if code:
        return Reply(False, code)
    agent = _find_agent(result)
    if not agent:
        return Reply(True)
    session = (agent.get("agent_session") or {}).get("value")
    return Reply(
        True,
        None,
        agent.get("agent_status"),
        f"{agent.get('pane_id')}:session:{session}" if session else None,
        agent.get("agent"),
        agent.get("interactive_ready"),
    )


def cross_sandbox_roots():
    """Realpaths every sandboxed pane can write to, whatever workspace it runs in."""
    roots = ["/tmp", "/private/tmp"]
    if os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    # bus_dirs() exits when no bus is configured, so ask only when one is.
    if os.environ.get("HERDR_BUS_DIR") or os.environ.get("HERDR_WORKSPACE_ID"):
        import importlib.util
        try:
            spec = importlib.util.spec_from_file_location("herdr_bus", HERDR_BUS)
            bus = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bus)
            roots.append(os.path.expanduser(bus.bus_dirs()["root"]))
        except (Exception, SystemExit):
            pass
    return tuple(sorted({os.path.realpath(os.path.expanduser(root)) for root in roots}))


def _inside(path, root):
    """Component containment: /tmp/herdr-bus2 is not inside /tmp/herdr-bus."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class Collector:
    """Chair-owned concurrent dispatch/collection under one global deadline.

    Seats are dispatched in parallel, every wait belongs to this process, and the
    artifact — not a lifecycle transition or a screen match — decides completion. A seat
    is never prompted while a turn is active, and only an attempt this run recorded as
    dispatched can be accepted.
    """

    def __init__(self, run_dir, run_id, workflow, round_id, transport, *,
                 deadline_seconds, grace_seconds=15.0):
        self.run_dir = run_dir
        self.run_id = check_identity("run id", run_id)
        self.workflow = check_identity("workflow", workflow)
        self.round_id = int(round_id)
        self.transport = transport
        self.deadline_seconds = deadline_seconds
        self.grace_seconds = grace_seconds
        self._deadline = None

    def collect(self, seats, *, dispatch_only=False):
        seen_seats = set()
        seen_targets = set()
        for seat in seats:
            check_identity("seat id", seat.seat_id)
            check_digest("input digest", seat.input_digest)
            # Two entries for one seat would share a checkpoint and prompt one pane twice;
            # two seats on one target would interleave turns on that pane.
            if seat.seat_id in seen_seats:
                raise ValueError(f"seat id {seat.seat_id!r} appears twice in this run")
            if seat.target in seen_targets:
                raise ValueError(f"target {seat.target!r} is claimed by two seats")
            seen_seats.add(seat.seat_id)
            seen_targets.add(seat.target)
        self._check_run_dir_reachable(seats)
        self._deadline = time.monotonic() + self.deadline_seconds
        records = {}
        with BusLease(f"collect-{self.run_id}"), \
                concurrent.futures.ThreadPoolExecutor(max_workers=max(len(seats), 1)) as pool:
            futures = {
                pool.submit(self._run_seat, seat, dispatch_only=dispatch_only): seat
                for seat in seats
            }
            concurrent.futures.wait(futures, timeout=self._remaining())
            self.transport.cancel_all()
            for future, seat in futures.items():
                records[seat.seat_id] = future.result()
        report = {
            "run_id": self.run_id,
            "workflow": self.workflow,
            "round": self.round_id,
            "run_dir": self.run_dir,
            "deadline_seconds": self.deadline_seconds,
            "mode": "dispatch" if dispatch_only else "collect",
            "complete": all(record["state"] == COMPLETED for record in records.values()),
            "published": all(record["published"] for record in records.values()),
            "seats": [records[seat.seat_id] for seat in seats],
        }
        if dispatch_only:
            report["uptake_complete"] = all(
                record["state"] in (DISPATCHED, COMPLETED, DECLARED_BLOCKED, DECLARED_FAILED)
                for record in records.values()
            )
        self._record_pending(report)
        return report

    def _record_pending(self, report):
        """Leave a breadcrumb for each seat still live, and clear each seat that is not.

        A `collect` that returns has stopped waiting, so a seat it did not finish is owed
        a doorbell exactly like a bare `dispatch` is — the two differ in how long they
        waited, never in what they leave behind (ORCH-02/ORCH-03).
        """
        for record in report["seats"]:
            if record["state"] in (COMPLETED, DECLARED_BLOCKED, DECLARED_FAILED):
                clear_pending(record["seat_id"])
            else:
                mark_pending(record["seat_id"], via=report["mode"], state=record["state"],
                             run_id=self.run_id, workflow=self.workflow,
                             round=self.round_id, run_dir=self.run_dir)

    def _check_run_dir_reachable(self, seats):
        """Refuse a run dir no sandboxed seat could publish into, before any side effect.

        A Codex `workspace-write` pane cannot write a home-state path, and the failure
        would otherwise surface only after that worker spent its whole turn (ORCH-01).
        """
        roots = cross_sandbox_roots()
        run_dir = os.path.realpath(self.run_dir)
        if any(_inside(run_dir, root) for root in roots):
            return
        for seat in seats:
            info = self.transport.get(seat.target)
            # A seat whose target does not answer is left to the per-seat path, which
            # already reports it as exited or unknown.
            if info.ok and info.kind in SANDBOXED_KINDS:
                raise ValueError(
                    f"run dir {self.run_dir!r} is not writable from a {info.kind} sandbox "
                    f"(seat {seat.seat_id!r}, target {seat.target!r}); use a run dir under "
                    f"one of: {', '.join(roots)}"
                )

    def _expect(self, seat, attempts):
        return Expectation(self.run_id, self.workflow, seat.seat_id, self.round_id,
                           seat.input_digest, frozenset(attempts))

    def remaining(self):
        """Seconds left in the global deadline, or None before collection starts."""
        if self._deadline is None:
            return None
        return max(self._deadline - time.monotonic(), 0.0)

    def _remaining(self):
        remaining = self.remaining()
        return 0.0 if remaining is None else remaining

    def _remaining_ms(self):
        # Never 0: herdr reads a missing/zero timeout as "wait indefinitely", which is
        # exactly the unbounded wait the global deadline exists to prevent.
        return max(int(self._remaining() * 1000), 1)

    def _accept(self, record, accepted):
        path, document = accepted
        record["state"] = STATE_BY_OUTCOME[document["outcome"]]
        record["published"] = True
        record["artifact"] = path
        record["outcome"] = document["outcome"]
        record["payload_digest"] = document["payload_digest"]
        record["accepted_attempt"] = document["attempt"]
        record["accepted_at"] = time.time()

    def _run_seat(self, seat, *, dispatch_only=False):
        record = {
            "seat_id": seat.seat_id,
            "target": seat.target,
            "state": UNKNOWN,
            "published": False,
            "artifact": None,
            "outcome": None,
            "payload_digest": None,
            "accepted_attempt": None,
            "attempts": [],
            "dispatched_at": None,
            "accepted_at": None,
            "occupant": None,
            "kind": None,
            "last_status": None,
            "resumed": False,
            "rejected": [],
        }
        checkpoint = Checkpoint(self.run_dir, self.run_id, self.workflow, seat.seat_id,
                                self.round_id, seat.input_digest)
        owned = False
        try:
            owned = checkpoint.acquire()
            record["owned"] = owned
            state = checkpoint.load()
            record["attempts"] = [entry["attempt"] for entry in state["dispatched"]]
            if self._resume(seat, record, checkpoint, state, owned):
                return record
            if not owned:
                # Another collector holds this seat. Reading its artifacts is safe;
                # dispatching behind its back is what produces duplicate turns.
                record["state"] = OWNED_ELSEWHERE
                return record
            if dispatch_only and record["attempts"]:
                record["state"] = state["state"] or UNKNOWN
                record["resumed"] = True
                return record
            self._dispatch(seat, record, checkpoint, state, dispatch_only=dispatch_only)
            checkpoint.record_state(state, record["state"])
        except CheckpointError as error:
            record["state"] = CHECKPOINT_CONFLICT
            record["checkpoint_conflict"] = str(error)
        except Exception as error:  # one seat's transport blowing up must not sink the run
            record["error"] = repr(error)
        finally:
            if owned:
                checkpoint.release()
        return record

    def _resume(self, seat, record, checkpoint, state, owned):
        """An acceptance already made stays made, content included."""
        if state["accepted"]:
            return self._resume_accepted(seat, record, state)
        accepted, rejected = scan(self.run_dir, self._expect(seat, record["attempts"]))
        record["rejected"] = [{"path": path, "reason": reason} for path, reason in rejected]
        if not accepted:
            return False
        self._accept(record, accepted)
        record["resumed"] = True
        if owned:
            checkpoint.record_acceptance(state, accepted[1], accepted[0])
        return True

    def _resume_accepted(self, seat, record, state):
        """Re-accept exactly the bytes that were accepted before, or fail closed.

        Binding only the attempt number would let the accepted path be unlinked and
        replaced with a different, internally valid artifact for the same attempt.
        """
        pinned = state["accepted"]
        accepted, rejected = scan(self.run_dir, self._expect(seat, [pinned["attempt"]]))
        record["rejected"] = [{"path": path, "reason": reason} for path, reason in rejected]
        mismatch = None
        if not accepted:
            mismatch = "accepted_artifact_missing"
        elif accepted[0] != pinned["path"]:
            mismatch = "accepted_path_changed"
        elif accepted[1]["payload_digest"] != pinned["payload_digest"]:
            mismatch = "accepted_payload_changed"
        elif accepted[1]["outcome"] != pinned["outcome"]:
            mismatch = "accepted_outcome_changed"
        if mismatch:
            record["state"] = CHECKPOINT_CONFLICT
            record["checkpoint_conflict"] = mismatch
            return True
        self._accept(record, accepted)
        record["resumed"] = True
        return True

    def _scan_accept(self, seat, record, checkpoint, state):
        accepted, rejected = scan(self.run_dir, self._expect(seat, record["attempts"]))
        record["rejected"] = [{"path": path, "reason": reason} for path, reason in rejected]
        if not accepted:
            return False
        self._accept(record, accepted)
        checkpoint.record_acceptance(state, accepted[1], accepted[0])
        return True

    def _dispatch(self, seat, record, checkpoint, state, *, dispatch_only=False):
        # A question one read caught, carried across iterations so herdr's idle fallback
        # cannot lose it. Applied only after the loop top revalidates identity (step 2),
        # so a question B asked is never attributed to A's seat.
        blocked_seen = False
        while self._remaining() > 0:
            if self._scan_accept(seat, record, checkpoint, state):
                return
            if record["state"] == PROMPT_STALLED:
                # Step 1 is sticky against EVERY later branch, not just identity. herdr
                # proved this text reached no turn; an unreachable seat, a question, or an
                # odd status observed afterwards are all weaker signals about that same
                # text, and none may overwrite the verdict with `exited`/`blocked`/
                # `unknown`. The artifact rescan above is the only thing that still can.
                # Recovery from here is inspect-first, never another prompt.
                record["retry_declined"] = "prompt_stalled_needs_inspection"
                return
            info = self.transport.get(seat.target)
            if not info.ok:
                record["state"] = EXITED if info.code == "agent_not_found" else UNKNOWN
                return
            record["last_status"] = info.status
            record["kind"] = info.kind
            pinned = _occupant_verdict(self.transport, record["occupant"], info.occupant)
            if pinned in (OCCUPANT_UNVERIFIABLE, OCCUPANT_CHANGED):
                record["occupant_verdict"] = pinned
                if record["state"] == PROMPT_STALLED:
                    # Step 1 again, on the next pass: the delivery axis is already decided
                    # by herdr's own answer, so churn stays metadata here too.
                    return
                # Identified last pass and unidentifiable now means something moved under
                # this seat; stopping beats dispatching a fresh attempt into that.
                record["state"] = REPLACED if pinned == OCCUPANT_CHANGED else UNKNOWN
                return
            record["occupant"] = info.occupant
            # Step 3: identity is confirmed above, so status evidence — this read's or a
            # sticky one from an earlier read of the same occupant — is now ours to apply.
            if blocked_seen or info.status == "blocked":
                record["last_status"] = "blocked"
                record["state"] = BLOCKED
                return
            if info.status == "working":
                if dispatch_only:
                    record["state"] = BUSY
                    record["retry_declined"] = "active_turn_not_owned"
                    return
                # A turn is active — ours or someone else's. Prompting now would stack a
                # second turn and `--wait` cannot tell the two apart, so wait it out.
                settled = self.transport.wait(seat.target, SETTLE_STATES,
                                              self._remaining_ms())
                # Remembered rather than acted on: the turn may have ended on a question,
                # but this occupant has not been revalidated since, and the loop top does
                # that before the flag is honoured.
                blocked_seen = _blocked_seen(settled)
                continue
            if info.status not in LIVE_STATES:
                record["state"] = UNKNOWN
                return
            if record["attempts"]:
                if not self._may_redispatch(seat, record, info):
                    return
                # The retry gate slept out the grace interval; the artifact may have
                # landed inside it, and a retry then would duplicate finished work.
                if self._scan_accept(seat, record, checkpoint, state):
                    return
            attempt = max(record["attempts"], default=0) + 1
            record["attempts"].append(attempt)
            record["dispatched_at"] = time.time()
            # Recorded BEFORE the prompt: a chair that dies mid-dispatch must still be
            # able to accept what this attempt publishes.
            checkpoint.record_dispatch(state, attempt, info.occupant)
            reply = self.transport.prompt(
                seat.target, self._dispatch_text(seat, attempt), UPTAKE_STATES,
                min(self._remaining_ms(), UPTAKE_TIMEOUT_MS),
            )
            stalled = reply.code == "agent_prompt_stalled"
            if stalled:
                record["state"] = PROMPT_STALLED
            landed = self.transport.get(seat.target)
            churn = _occupant_verdict(self.transport, info.occupant, landed.occupant) \
                if landed.ok else OCCUPANT_UNVERIFIABLE
            if churn != OCCUPANT_SAME:
                record["occupant_verdict"] = churn
            # Retained independently of the decision below: when identity is merely
            # unverifiable the classifier defers rather than credits, and a question this
            # reply carried must survive that deferral to be applied once identity holds.
            blocked_seen = blocked_seen or _blocked_seen(reply, landed)
            decision = classify_post_prompt(reply, churn, landed)
            if decision == POST_PROMPT_INDETERMINATE:
                record["transport_error"] = reply.code
            if self._scan_accept(seat, record, checkpoint, state):
                return
            if decision == POST_PROMPT_STALLED:
                # Churn is recorded above, never promoted into `replaced` plus a
                # misdelivered attempt that never left. The loop top's sticky guard ends
                # the seat on the next pass, after one more artifact rescan.
                continue
            if decision == POST_PROMPT_INDETERMINATE:
                # Not a retry: the call failed without saying whether the text was
                # submitted, so a second attempt could deliver the brief twice. But the
                # artifact outranks transport state (HL-043/050) — the response may have
                # been dropped by a socket that had already delivered the prompt, and the
                # worker may be publishing right now. Give the deliverable the rest of
                # the deadline before calling the seat unknown; terminate either way.
                if self._await_artifact(seat, record, checkpoint, state):
                    return
                record["state"] = UNKNOWN
                return
            if decision == POST_PROMPT_REPLACED:
                # herdr has no compare-and-swap on a prompt target; the best we can do is
                # notice immediately and say where the work went.
                record["state"] = REPLACED
                record["misdelivered_attempt"] = attempt
                return
            if decision == POST_PROMPT_BLOCKED:
                # Identity confirmed, so this question is ours. Only the reply carries it:
                # the next loop's get can fall back to idle, leaving the seat merely
                # artifact-less and eligible for a retry straight into the dialog.
                if not self._scan_accept(seat, record, checkpoint, state):
                    record["last_status"] = "blocked"
                    record["state"] = BLOCKED
                return
            if dispatch_only and decision == POST_PROMPT_OPEN \
                    and (reply.ok or landed.status == "working"):
                record["state"] = DISPATCHED
                record["uptake_status"] = reply.status or landed.status
                return
            # `unverifiable` and `open` both fall through. This is the ONE place the two
            # paths intentionally differ: a collector can loop, so it revalidates identity
            # at the loop top and lets the artifact keep deciding, where `dispatch_once`
            # must answer now and reports `unknown`.
        self._scan_accept(seat, record, checkpoint, state)

    def _await_artifact(self, seat, record, checkpoint, state):
        """Wait out the deadline on the artifact alone, accepting one if it appears.

        For the case where delivery is UNKNOWN: the brief may have been delivered and the
        turn may be running, and the artifact is the only thing that can settle that.
        Deliberately artifact-only — no prompt, no status call, no retry — because the
        same uncertainty that makes waiting worthwhile makes sending anything again a
        possible duplicate.
        """
        while self._remaining() > 0:
            if self._scan_accept(seat, record, checkpoint, state):
                return True
            time.sleep(min(ARTIFACT_POLL_SECONDS, self._remaining()))
        # One last look: the deliverable may have landed inside the final interval.
        return self._scan_accept(seat, record, checkpoint, state)

    def _may_redispatch(self, seat, record, info):
        """Gate on a settled prior attempt: retry only with proof no turn is running."""
        # A stalled seat never reaches this gate: the loop top ends it first, because the
        # trust dialog that ate the prompt would eat a retry too.
        record["state"] = READY_WITHOUT_ARTIFACT
        if len(record["attempts"]) >= seat.max_attempts:
            if len(record["attempts"]) > 1:
                # Only a seat that actually consumed retries is retry-exhausted; a
                # single-attempt seat keeps the state that explains why it failed.
                record["state"] = RETRY_EXHAUSTED
            return False
        if info.occupant is None:
            record["retry_declined"] = "unidentifiable_occupant"
            return False
        if not seat.allow_idle_retry:
            # No kind's idle is causal proof the dispatched turn ended: integrations
            # supply session identity, status still comes from screen manifests, and a
            # known-agent screen can fall back to idle mid-turn. `interactive_ready` is
            # input readiness, not turn completion.
            record["retry_declined"] = "idle_is_not_turn_completion"
            return False
        time.sleep(min(self.grace_seconds, self._remaining()))
        confirm = self.transport.get(seat.target)
        # Stricter than the replacement checks above on purpose: reporting a replacement
        # is cheap, but licensing a retry on an occupant nobody can name risks a second
        # turn, so an unidentifiable confirmation still declines.
        if not confirm.ok or confirm.occupant is None \
                or _occupant_verdict(self.transport, info.occupant,
                                     confirm.occupant) != OCCUPANT_SAME:
            record["retry_declined"] = "occupant_changed"
            return False
        if confirm.status not in LIVE_STATES or confirm.ready is not True:
            # The "idle" that licensed the retry was a lull, or the pane is not accepting
            # input; either way a second prompt would duplicate or vanish.
            record["last_status"] = confirm.status
            record["retry_declined"] = f"not_settled:{confirm.status}"
            return False
        return True

    def _dispatch_text(self, seat, attempt):
        return dispatch_text(
            seat_id=seat.seat_id, run_id=self.run_id, workflow=self.workflow,
            round_id=self.round_id, attempt=attempt,
            input_digest=seat.input_digest, run_dir=self.run_dir, prompt=seat.prompt,
        )


def _load_bus(consequence):
    """The herdr-bus module, or None when this session has no usable bus.

    No bus configured is normal: the artifact is the deliverable and the signal is an
    optimisation. But a bus root that IS set with no usable script is a misinstall
    (unstowed herdr-bus.py), and a silently missing doorbell is the exact failure this
    wiring prevents — so that case says what it cost.
    """
    import importlib.util
    try:
        spec = importlib.util.spec_from_file_location("herdr_bus", HERDR_BUS)
        bus = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bus)
        bus.bus_dirs()  # exits when neither HERDR_BUS_DIR nor a workspace is set
        return bus
    except (Exception, SystemExit) as err:
        if os.environ.get("HERDR_BUS_DIR") or os.environ.get("HERDR_WORKSPACE_ID"):
            print(f"agent-handoff: bus configured but {HERDR_BUS} unusable ({err}); "
                  f"{consequence}", file=sys.stderr)
        return None


def _pending_dir(bus):
    path = os.path.join(bus.bus_dirs()["root"], PENDING_DIR)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def mark_pending(seat_id, **fields):
    """Record that this process is returning while `seat_id` is still live.

    Every chair-side command that reaches a worker returns after uptake, not on
    completion, so at the moment it returns nothing in this process is waiting any
    more. The breadcrumb outlives the process and lets the stop gate ask the one
    question the arming rule turns on: is anything still listening for this seat
    (ORCH-02)? `verify` and a completing `collect` clear it.
    """
    bus = _load_bus("not recording a pending seat")
    if bus is None:
        return None
    try:
        path = os.path.join(_pending_dir(bus), bus.sanitize(seat_id) + ".json")
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"seat_id": seat_id, "recorded_at": time.time(), **fields}, handle,
                      sort_keys=True)
        os.rename(tmp, path)
        return path
    except (Exception, SystemExit) as err:
        print(f"agent-handoff: could not record pending seat {seat_id!r} ({err})",
              file=sys.stderr)
        return None


def clear_pending(seat_id):
    """Drop a seat's breadcrumb; never recorded and already cleared are the same answer."""
    bus = _load_bus("not clearing a pending seat")
    if bus is None:
        return
    try:
        os.unlink(os.path.join(_pending_dir(bus), bus.sanitize(seat_id) + ".json"))
    except (Exception, SystemExit):
        pass


def pending_seats():
    """Every recorded live seat, each marked with whether anything still owns its wake.

    An owner is any live bus lease: an armed `herdr-bus.py watch`, or a `collect` still
    blocking. Ownership is per session, not per seat, because that is what the arming
    rule requires — one doorbell covers every live worker. Two concurrent runs therefore
    cover each other, which under-reports rather than blocking a turn that is fine.
    """
    bus = _load_bus("cannot report pending seats")
    if bus is None:
        return []
    try:
        watch = bus.bus_dirs()["watch"]
        listeners = []
        for name in sorted(os.listdir(watch)):
            if not name.endswith(".lease"):
                continue
            try:
                with open(os.path.join(watch, name), encoding="utf-8") as handle:
                    owner = json.load(handle).get("pid")
            except (OSError, ValueError):
                continue
            # a SIGKILLed watcher never released its lease, so the file alone
            # proves nothing — only its owner still existing does
            if bus.pid_alive(owner):
                listeners.append(name[:-len(".lease")])
        entries = []
        for name in sorted(os.listdir(_pending_dir(bus))):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(_pending_dir(bus), name), encoding="utf-8") as handle:
                    body = json.load(handle)
            except (OSError, ValueError):
                continue  # a half-written or hand-edited breadcrumb proves nothing
            body["listeners"] = listeners
            body["owned"] = bool(listeners)
            entries.append(body)
        return entries
    except (Exception, SystemExit) as err:
        print(f"agent-handoff: could not read pending seats ({err})", file=sys.stderr)
        return []


class BusLease:
    """A bus lease held while this process blocks on seats.

    Same file, name, and shape `herdr-bus.py watch` writes, because the stop gate asks
    one question — is any live process listening — and a blocking collect answers it
    exactly like an armed watcher does.
    """

    def __init__(self, name):
        self.name = name
        self.path = None

    def __enter__(self):
        bus = _load_bus("this collect registers no lease")
        if bus is not None:
            try:
                self.path = bus.write_lease(bus.bus_dirs(), bus.sanitize(self.name), None)
            except (Exception, SystemExit) as err:
                print(f"agent-handoff: could not register a collect lease ({err})",
                      file=sys.stderr)
        return self

    def __exit__(self, *_exc):
        if not self.path:
            return False
        try:
            # Only ours: a replacement may already hold the name, and unlinking its
            # lease would let a third listener arm alongside it.
            with open(self.path, encoding="utf-8") as handle:
                if json.load(handle).get("pid") != os.getpid():
                    return False
            os.unlink(self.path)
        except (OSError, ValueError):
            pass
        return False


def _bus_emit_paragraph(seat_id):
    """Bus-emit instruction for a dispatched seat, or "" when no bus is configured.

    The bus root is resolved chair-side and interpolated as a literal: a worker
    re-expanding $VARS in another workspace or worktree resolves a different
    default root and would signal the wrong bus.
    """
    bus = _load_bus("dispatching without an emit line")
    if bus is None:
        return ""
    try:
        # the worker runs this from its own cwd, so the root must already be
        # absolute — a relative or ~-rooted value would land on another tree
        root = os.path.abspath(os.path.expanduser(bus.bus_dirs()["root"]))
        seat = bus.sanitize(seat_id)
    except (Exception, SystemExit) as err:
        print(f"agent-handoff: bus configured but seat {seat_id!r} has no emit line "
              f"({err})", file=sys.stderr)
        return ""
    return (
        "\n\nAfter a successful publish, run exactly:\n"
        f"HERDR_BUS_DIR={shlex.quote(root)} {shlex.quote(BUS_COMMAND)} emit "
        f"--from {seat} --kind done --ref <artifact path>\n"
        "Use --kind blocked or --kind failed to match a blocked/failed publish outcome.\n"
        "Do not edit these values. This is a signal only; the artifact remains the "
        "deliverable."
    )


def dispatch_text(*, seat_id, run_id, workflow, round_id, attempt, input_digest,
                  run_dir, prompt):
    """The exact text a dispatched seat receives: prompt, publish command, bus emit."""
    command = shlex.join([
        PUBLISH_COMMAND, "publish",
        "--run-dir", run_dir,
        "--run-id", run_id,
        "--workflow", workflow,
        "--seat", seat_id,
        "--round", str(round_id),
        "--attempt", str(attempt),
        "--input-digest", input_digest,
        "--outcome", "ok",
    ])
    return (
        f"{prompt}\n\n"
        "Publish your result once your work is durably written: run exactly this "
        "command with --payload-file and your result file appended to it. The chair "
        "reads the artifact, never your terminal output, so no marker line is needed "
        "and a wrapped or scrolled screen costs nothing:\n"
        f"{command}\n"
        "Do not edit those values or recompute the paths. If you cannot finish, "
        "publish the same way with --outcome blocked or --outcome failed and a "
        "payload that explains what you need; that publishes the seat but does not "
        "report it as done."
        f"{_bus_emit_paragraph(seat_id)}"
    )


dispatch_text_for_test = dispatch_text  # name the wiring test binds to


def _prompt_result(outcome, next_action, *, prompted, reason=None, status=None,
                   occupant=None, occupant_verdict=None):
    # `outcome` is the delivery axis and `occupant_verdict` the identity axis. They are
    # independent: a prompt herdr never submitted can still be followed by churn, and
    # collapsing the two lets one axis overwrite the other's evidence.
    return {
        "outcome": outcome,
        "prompted": prompted,
        "reason": reason,
        "status": status,
        "occupant": occupant,
        "occupant_verdict": occupant_verdict,
        "next_action": next_action,
    }


def _unsettled_reason(info, *also_seen):
    """Why this seat must not be prompted, or None when prompting is safe.

    `blocked` is a settle state but not a live one, so it lands here as a status reason:
    a seat holding a question would queue the text behind that dialog. `also_seen` carries
    earlier reads of the same seat — a wait reply, say — because the question one of them
    caught is still unanswered even if `info` came back with herdr's idle fallback.
    """
    if not info.ok:
        return info.code
    if _blocked_seen(info, *also_seen):
        return "status:blocked"
    if info.status not in LIVE_STATES:
        return f"status:{info.status}"
    if info.ready is not True:
        return "not_interactive_ready"
    if info.occupant is None:
        # With no identity pinned there is no way to report afterwards that the text
        # reached THIS seat rather than a replacement. herdr fails to name an occupant
        # only when process evidence is unreadable or the pane moved mid-read, so this is
        # the exceptional path, not the normal one for screen-manifest kinds — and while
        # nothing has been sent yet, declining is the answer with no duplicate-turn risk.
        return "occupant_unpinned"
    return None


def dispatch_once(transport, target, text, *, settle_timeout_ms, wait_timeout_ms):
    """One ad-hoc prompt with the discipline a bare `agent prompt --wait` lacks.

    Two unlike failures hide behind that command's timeout. `agent_prompt_stalled` means
    herdr observed no state change, so the text never reached a turn; `timeout` means the
    prompt was submitted but the requested observation outlived the wait, and re-prompting
    can stack a second turn. The occupant is re-read after every outcome, so a pane
    replaced between the check and the prompt is reported rather than counted as delivery.

    Delivery and identity are separate axes and neither may overwrite the other: herdr's
    own "no turn started" answer decides delivery, while occupant churn is reported
    alongside it. Every identity window is guarded, including the settle wait — the
    occupant we agreed to wait for can be replaced before the wait returns.

    For a seat that publishes a result artifact, `collect` is the better path — it owns
    the same discipline plus checkpoints, retries and artifact acceptance.
    """
    info = transport.get(target)
    waited = None
    if info.ok and info.status == "working" and settle_timeout_ms > 0:
        # Waiting out a turn is its own identity window: the occupant we agreed to wait
        # for can exit and be replaced before the wait returns. Keep the pre-wait identity
        # and hold the post-wait one to it, or the replacement silently inherits the text.
        pinned = info.occupant
        if pinned is None:
            # No baseline to hold the wait to: whoever is settled afterwards would pass
            # unchallenged, exactly as an unpinned seat does on the direct path. Refuse
            # here rather than after the wait, since there is nothing to wait *for*.
            return _prompt_result(
                SEAT_UNSETTLED,
                "nothing was sent; this seat is mid-turn and its occupant cannot be "
                "identified, so a settle wait would prove nothing — inspect it",
                prompted=False, reason="occupant_unpinned", status=info.status,
                occupant=None, occupant_verdict=OCCUPANT_UNPINNED)
        waited = transport.wait(target, SETTLE_STATES, settle_timeout_ms)
        info = transport.get(target)
        settled_as = _occupant_verdict(transport, pinned, info.occupant)
        if settled_as in (OCCUPANT_CHANGED, OCCUPANT_UNVERIFIABLE):
            return _prompt_result(
                SEAT_UNSETTLED,
                "nothing was sent; the occupant you waited for is no longer the one "
                "holding this seat — re-check the target before prompting",
                prompted=False, reason=f"occupant_{settled_as}_while_settling",
                status=info.status, occupant=info.occupant, occupant_verdict=settled_as)
    unsettled = _unsettled_reason(info, waited)
    if unsettled:
        return _prompt_result(
            SEAT_UNSETTLED, "nothing was sent; settle or inspect the seat, then prompt",
            prompted=False, reason=unsettled, status=info.status, occupant=info.occupant)
    reply = transport.prompt(target, text, UPTAKE_STATES,
                             min(wait_timeout_ms, UPTAKE_TIMEOUT_MS))
    # Re-read after EVERY outcome, not just the timeout. A happy reply proves the requested
    # uptake, never that it was still our occupant's turn — and `prompt` replies carry
    # no process-group fallback, so only `get` can identify a screen-manifest kind.
    after = transport.get(target)
    # This read is evidence about IDENTITY only. Its failure means we have no identity
    # evidence — it is not evidence about delivery, and must not overwrite what the
    # prompt call itself already proved.
    verdict = _occupant_verdict(transport, info.occupant, after.occupant) \
        if after.ok else OCCUPANT_UNVERIFIABLE
    decision = classify_post_prompt(reply, verdict, after)
    if decision == POST_PROMPT_STALLED:
        # Decided before any identity branch, including an unreadable one: herdr saw no
        # state change, which is direct evidence on the DELIVERY axis — the text reached
        # no turn. Nothing observed afterwards can promote that to "the text went to the
        # new occupant" or demote it to indeterminate; either would discard a provably
        # undelivered prompt and cost the caller its exit 1.
        churn = "" if verdict == OCCUPANT_SAME else \
            f" Occupant identity afterwards: {verdict} — re-check the target too."
        return _prompt_result(
            NEVER_LANDED,
            "inspect first (agent explain, pane process-info, verify --diagnose); never "
            "blind re-prompt or send-keys." + churn,
            prompted=True, reason=reply.code, status=after.status,
            occupant=after.occupant, occupant_verdict=verdict)
    if decision == POST_PROMPT_INDETERMINATE:
        return _prompt_result(
            UNKNOWN,
            "the prompt call failed in transport, so whether the text was submitted is "
            "unknown; inspect the seat and do not send again until you know",
            prompted=True, reason=reply.code, status=after.status,
            occupant=after.occupant, occupant_verdict=verdict)
    if decision == POST_PROMPT_REPLACED:
        return _prompt_result(
            REPLACED, "the pane was replaced; the text went to the new occupant",
            prompted=True, status=after.status, occupant=after.occupant,
            occupant_verdict=verdict)
    if decision == POST_PROMPT_UNVERIFIABLE:
        # The one place the two paths intentionally differ: the collector loops and can
        # revalidate, so it defers. This call must answer now, and the text was sent
        # without proof of who received it, so indeterminate is the honest report.
        if not after.ok:
            return _prompt_result(
                UNKNOWN, "the seat went unreachable after prompting; inspect it",
                prompted=True, reason=after.code, occupant_verdict=verdict)
        return _prompt_result(
            UNKNOWN,
            "the text was sent but the occupant can no longer be identified; confirm "
            "against the artifact before treating this seat as dispatched",
            prompted=True, reason="occupant_unverifiable", status=after.status,
            occupant=after.occupant, occupant_verdict=verdict)
    if decision == POST_PROMPT_BLOCKED:
        # Ahead of the happy-reply branch: a prompt or the immediate identity read can catch
        # a question, and that must tell the chair to answer rather than wait for an artifact.
        return _prompt_result(BLOCKED, "the seat is asking a question; read the pane and answer it",
                              prompted=True, status="blocked", occupant=after.occupant,
                              occupant_verdict=verdict)
    # Everything below is a fallback: `reply.ok` only says the requested state was reached, so
    # it must stay below every branch that identifies WHICH state and what it needs.
    if reply.ok and reply.status == "working":
        return _prompt_result(
            LANDED_WORKING,
            "the prompt landed and the turn is running; wait for the artifact, do not re-prompt",
            prompted=True, status=reply.status, occupant=after.occupant,
            occupant_verdict=verdict)
    if reply.ok:
        return _prompt_result(SETTLED, "the turn ended; read the seat's artifact",
                              prompted=True, status=reply.status, occupant=after.occupant,
                              occupant_verdict=verdict)
    if after.status == "working":
        return _prompt_result(
            LANDED_WORKING,
            "the prompt landed and the turn is running; wait for the artifact, do not re-prompt",
            prompted=True, status=after.status, occupant=after.occupant,
            occupant_verdict=verdict)
    if info.status in SETTLE_STATES and after.ok and after.status == info.status:
        # The turn may also have run to completion inside the uptake window without a
        # poll catching "working" — only the artifact can tell the two apart, which is
        # why the recovery is check-artifact-first and the automatic retry is opt-in.
        return _prompt_result(
            LANDED_UNCONFIRMED,
            "the uptake wait saw no turn and the seat still shows its pre-prompt "
            "settled state — first-prompt swallow signature: check the artifact; if "
            "absent, one identical re-prompt is the documented recovery",
            prompted=True, reason=UPTAKE_UNOBSERVED_CODE, status=after.status,
            occupant=after.occupant, occupant_verdict=verdict)
    return _prompt_result(
        LANDED_UNCONFIRMED,
        "herdr saw the turn start but the seat is settled again; check the artifact "
        "before considering a re-prompt",
        prompted=True, status=after.status, occupant=after.occupant,
        occupant_verdict=verdict)


def dispatch_with_stalled_backoff(
        transport, target, text, *, settle_timeout_ms, wait_timeout_ms,
        retry_stalled_for_ms=0, sleeper=time.sleep):
    """Retry only failures a re-prompt is known to cure, each gated to the kind that
    exhibits it.

    Two signatures qualify. An AGY ``agent_prompt_stalled`` is herdr's proof the text
    reached no turn, so re-prompting cannot stack a turn. A codex first-prompt swallow
    (``uptake_unobserved_preexisting_settle``) reports delivered while the TUI never
    took the text; one identical re-prompt is the documented recovery, capped at one
    because a second identical timeout means something else is wrong.

    The retry budget is delay time between attempts, not transport observation time.
    Every attempt still passes through ``dispatch_once``, which revalidates readiness
    and occupant identity and stops the loop on any other result.
    """
    if not 0 <= retry_stalled_for_ms <= MAX_STALLED_RETRY_MS:
        raise ValueError(
            f"retry stalled window must be between 0 and {MAX_STALLED_RETRY_MS}ms")
    if retry_stalled_for_ms == 0:
        return dispatch_once(
            transport, target, text, settle_timeout_ms=settle_timeout_ms,
            wait_timeout_ms=wait_timeout_ms)

    worker_history_marker = "[agent-worker-history]"
    if worker_history_marker not in text:
        text = f"{text.rstrip()}\n\n{worker_history_marker}"

    retry_count = 0
    retry_wait_ms = 0
    delay_ms = STALLED_RETRY_INITIAL_MS
    swallow_retries = 0

    def gate():
        info = transport.get(target)
        if info.ok and info.kind in ("agy", "codex"):
            return info.kind, None
        return None, _prompt_result(
            SEAT_UNSETTLED,
            "nothing was sent; prompt retry is restricted to a verified AGY or codex seat",
            prompted=False, reason="retry_requires_agy_or_codex", status=info.status,
            occupant=info.occupant)

    def wants_retry(res, kind):
        if res["outcome"] == NEVER_LANDED and res.get("reason") == PROMPT_STALLED_CODE:
            return kind == "agy"
        return (kind == "codex" and swallow_retries == 0
                and res["outcome"] == LANDED_UNCONFIRMED
                and res.get("reason") == UPTAKE_UNOBSERVED_CODE)

    kind, refusal = gate()
    result = refusal if refusal is not None else dispatch_once(
        transport, target, text, settle_timeout_ms=settle_timeout_ms,
        wait_timeout_ms=wait_timeout_ms)

    while (refusal is None and wants_retry(result, kind)
           and retry_wait_ms < retry_stalled_for_ms):
        if result["outcome"] == LANDED_UNCONFIRMED:
            swallow_retries += 1
        sleep_ms = min(delay_ms, retry_stalled_for_ms - retry_wait_ms)
        sleeper(sleep_ms / 1000)
        retry_wait_ms += sleep_ms
        delay_ms = min(delay_ms * 2, STALLED_RETRY_CAP_MS)

        kind, refusal = gate()
        if refusal is not None:
            result = refusal
            break
        retry_count += 1
        result = dispatch_once(
            transport, target, text, settle_timeout_ms=settle_timeout_ms,
            wait_timeout_ms=wait_timeout_ms)

    return {**result, "retry_count": retry_count, "retry_wait_ms": retry_wait_ms}


def load_seats(spec):
    return [
        Seat(
            seat_id=entry["seat_id"],
            target=entry["target"],
            prompt=entry["prompt"],
            input_digest=entry["input_digest"],
            max_attempts=entry.get("max_attempts", 1),
            allow_idle_retry=entry.get("allow_idle_retry", False),
        )
        for entry in spec["seats"]
    ]


def read_payload(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def command_publish(args):
    document = envelope(
        run_id=check_identity("run id", args.run_id),
        workflow=check_identity("workflow", args.workflow),
        seat_id=check_identity("seat id", args.seat),
        round_id=args.round,
        attempt=args.attempt,
        outcome=args.outcome,
        input_digest=check_digest("input digest", args.input_digest),
        payload=read_payload(args.payload_file),
    )
    print(publish(args.run_dir, document))
    return 0


def payload_out_path(base, round_id, attempt, payload_digest):
    """Content-addressed sibling of `base`: one path per accepted version.

    A fixed per-seat path cannot serve round 2 — the round-1 bytes own it, and a
    non-clobbering write must then refuse the new payload. Keying the name on round,
    attempt and payload digest gives every accepted version its own immutable file, so
    iteration works and a stale copy can never masquerade as the current one.
    """
    directory, name = os.path.split(base)
    stem, extension = os.path.splitext(name)
    unique = f"{stem}.r{round_id}.a{attempt}.{payload_digest[:12]}{extension or '.md'}"
    return os.path.join(directory, unique)


def materialize(path, payload):
    """Publish the accepted payload to `path` without ever clobbering it.

    A plain write would let the copy a downstream seat reads drift from the digest it is
    bound to. The path is written once; a re-run with identical bytes is a no-op, and a
    path whose content has changed is an error. It is a point check, not a guarantee about
    later readers — they must take the content from `agent-handoff.py consume`.
    """
    expected = digest(payload)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            found = digest(handle.read())
        if found != expected:
            raise ValueError(
                f"payload-out {path} holds {found}, not the accepted payload {expected}"
            )
        return path
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory,
                        f".{os.path.basename(path)}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp")
    try:
        _write_durably(temp, payload)
        os.link(temp, path)
    except FileExistsError:
        # Another writer won the race; its content still has to match.
        with open(path, encoding="utf-8") as handle:
            if digest(handle.read()) != expected:
                raise ValueError(f"payload-out {path} was written with different content")
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
    _fsync_directory(directory)
    with open(path, encoding="utf-8") as handle:
        if digest(handle.read()) != expected:
            raise ValueError(f"payload-out {path} does not match the accepted payload")
    return path


def command_verify(args):
    attempts = args.attempt
    if not args.diagnose and not attempts:
        # Acceptance needs to know which attempts were actually dispatched; the durable
        # checkpoint is the only record of that, and guessing would re-open the
        # undispatched-artifact hole the collector closed.
        checkpoint = Checkpoint(args.run_dir, args.run_id, args.workflow, args.seat,
                                args.round, args.input_digest)
        state = checkpoint.load()
        attempts = [entry["attempt"] for entry in state["dispatched"]]
        if state["accepted"]:
            attempts = [state["accepted"]["attempt"]]
        if not attempts:
            raise ValueError(
                "no dispatched attempts recorded for this seat: pass --attempt N for a "
                "dispatch made outside collect, or --diagnose to inspect candidates"
            )
    expect = Expectation(args.run_id, args.workflow, args.seat, args.round,
                         args.input_digest,
                         None if args.diagnose else frozenset(attempts))
    accepted, rejected = scan(args.run_dir, expect)
    if args.diagnose:
        # Diagnosis never returns acceptance: it reports candidates and always exits 1.
        print(json.dumps({
            "mode": "diagnostic",
            "accepted": None,
            "candidates": [{"path": accepted[0], "artifact": accepted[1]}] if accepted else [],
            "rejected": [{"path": path, "reason": reason} for path, reason in rejected],
        }, indent=2))
        return 1
    payload_out = None
    if accepted and args.payload_out:
        payload_out = materialize(
            payload_out_path(args.payload_out, args.round, accepted[1]["attempt"],
                             accepted[1]["payload_digest"]),
            accepted[1]["payload"],
        )
    print(json.dumps({
        "accepted": {
            "path": accepted[0],
            "artifact": accepted[1],
            "payload_out": payload_out,
            "payload_digest": accepted[1]["payload_digest"],
        } if accepted else None,
        "attempts": sorted(attempts),
        "rejected": [{"path": path, "reason": reason} for path, reason in rejected],
    }, indent=2))
    if accepted:
        clear_pending(args.seat)
    return 0 if accepted else 1


WORKER_SESSION_REFUSAL = (
    "agent-handoff: worker sessions do not chair collection loops (orchestrator-only). "
    "Publish your own result and report back to the orchestrator."
)


def _worker_session():
    """HL-064 markers agent-teammate.py plants in every child. A registry ROOT outranks
    them: its capability is an explicit grant to orchestrate (see bash-guard.sh)."""
    if (os.environ.get("HERDR_REGISTRY_ROOT")
            and os.environ.get("HERDR_REGISTRY_CAPABILITY")
            and not os.environ.get("HERDR_REGISTRY_KEY")):
        return False
    return any(os.environ.get(name) == "1"
               for name in ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD"))


def command_collect(args):
    if _worker_session():
        print(WORKER_SESSION_REFUSAL, file=sys.stderr)
        return 3
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    collector = Collector(
        spec["run_dir"], spec["run_id"], spec["workflow"], spec["round"],
        None,
        deadline_seconds=spec["deadline_seconds"],
        grace_seconds=spec.get("grace_seconds", 15.0),
    )
    # The transport shares the collector's deadline, so even a connect cannot outlive it.
    collector.transport = SocketTransport(budget=collector.remaining)
    report = collector.collect(load_seats(spec))
    print(json.dumps(report, indent=2))
    return 0 if report["complete"] else 1


def command_dispatch(args):
    if _worker_session():
        print(WORKER_SESSION_REFUSAL, file=sys.stderr)
        return 3
    with open(args.spec, encoding="utf-8") as handle:
        spec = json.load(handle)
    collector = Collector(
        spec["run_dir"], spec["run_id"], spec["workflow"], spec["round"],
        None,
        deadline_seconds=min(spec.get("dispatch_timeout_seconds", 15.0),
                             spec["deadline_seconds"]),
        grace_seconds=spec.get("grace_seconds", 15.0),
    )
    collector.transport = SocketTransport(budget=collector.remaining)
    report = collector.collect(load_seats(spec), dispatch_only=True)
    print(json.dumps(report, indent=2))
    return 0 if report["uptake_complete"] else 1


def command_prompt(args):
    # `prompt` carries no run context, so it cannot rebuild the publish command the way
    # dispatch_text does — but the doorbell needs only the seat. Without this a seat first
    # contacted through `prompt` publishes with no event at all (ORCH-02).
    result = dispatch_with_stalled_backoff(
        SocketTransport(), args.target, args.text + _bus_emit_paragraph(args.target),
        settle_timeout_ms=args.settle_timeout, wait_timeout_ms=args.wait_timeout,
        retry_stalled_for_ms=args.retry_stalled_for,
    )
    if result["outcome"] in LANDED_OUTCOMES:
        mark_pending(args.target, via="prompt", state=result["outcome"])
    print(json.dumps({"target": args.target, **result}, indent=2))
    if result["outcome"] in LANDED_OUTCOMES:
        return 0
    return 1 if result["outcome"] in UNSENT_OUTCOMES else 2


def command_pending(args):
    for seat in args.clear or ():
        clear_pending(seat)
    entries = pending_seats()
    unowned = [entry["seat_id"] for entry in entries if not entry["owned"]]
    print(json.dumps({"pending": entries, "unowned": unowned}, indent=2))
    return 1 if unowned else 0


def command_digest(args):
    print(digest(read_payload(args.file)))
    return 0


def command_consume(args):
    """Read once, verify that buffer, and emit exactly the bytes that were verified.

    A gate that hashes the file and exits still leaves the consumer to open the path
    again, so it proves nothing about what the consumer read — it only moves the
    check-to-use interval. This command closes it by making the verified buffer the
    deliverable: whoever runs it adjudicates this stdout and never reopens the file. On a
    mismatch it emits no payload at all, so there is nothing to adjudicate by accident.
    """
    expected = check_digest("expected digest", args.digest)
    with open(args.file, "rb") as handle:
        raw = handle.read()
    found = hashlib.sha256(raw).hexdigest()
    if found != expected:
        print(f"agent-handoff: {args.file} holds {found}, not the expected {expected}; "
              "no payload emitted", file=sys.stderr)
        return 2
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(raw.decode("utf-8"))
    else:
        stream.write(raw)
        stream.flush()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    publisher = commands.add_parser("publish", help="atomically publish one result artifact")
    publisher.add_argument("--run-dir", required=True)
    publisher.add_argument("--run-id", required=True)
    publisher.add_argument("--workflow", required=True)
    publisher.add_argument("--seat", required=True)
    publisher.add_argument("--round", type=int, required=True)
    publisher.add_argument("--attempt", type=int, required=True)
    publisher.add_argument("--input-digest", required=True)
    publisher.add_argument("--outcome", choices=OUTCOMES, default="ok")
    publisher.add_argument("--payload-file", required=True, help="result file, or - for stdin")
    publisher.set_defaults(handler=command_publish)

    verifier = commands.add_parser("verify", help="report the accepted artifact for a seat")
    verifier.add_argument("--run-dir", required=True)
    verifier.add_argument("--run-id", required=True)
    verifier.add_argument("--workflow", required=True)
    verifier.add_argument("--seat", required=True)
    verifier.add_argument("--round", type=int, required=True)
    verifier.add_argument("--input-digest", required=True)
    verifier.add_argument("--attempt", type=int, action="append",
                          help="attempts dispatched outside collect; repeatable. Without "
                               "it the seat's checkpoint supplies them")
    verifier.add_argument("--diagnose", action="store_true",
                          help="list candidates ignoring dispatch records; never accepts")
    verifier.add_argument("--payload-out",
                          help="materialize the accepted payload beside this path, named "
                               "per round/attempt/digest; the exact path is reported back")
    verifier.set_defaults(handler=command_verify)

    collect = commands.add_parser("collect", help="dispatch and collect seats concurrently")
    collect.add_argument("--spec", required=True, help="JSON run spec")
    collect.set_defaults(handler=command_collect)

    dispatcher = commands.add_parser(
        "dispatch", help="dispatch seats concurrently and return after worker uptake")
    dispatcher.add_argument("--spec", required=True, help="JSON run spec")
    dispatcher.description = (
        "Checkpoint every prompt, wait only until each worker turn starts, then return. "
        "Use the artifact bus plus verify for durable completion, or run collect when the "
        "chair intentionally owns the full result wait."
    )
    dispatcher.set_defaults(handler=command_dispatch)

    prompter = commands.add_parser(
        "prompt", help="prompt one settled seat and return after worker uptake")
    prompter.add_argument("--target", required=True)
    prompter.add_argument("--text", required=True,
                          help="one-line pointer to the brief; a multi-KB paste stalls the submit")
    prompter.add_argument("--settle-timeout", type=int, default=15000, metavar="MS",
                          help="wait this long for an active turn to end before prompting; "
                               "0 reports the busy seat instead of waiting")
    prompter.add_argument("--wait-timeout", type=int, default=30000, metavar="MS",
                          help="uptake wait budget; capped at 6000ms")
    prompter.add_argument(
        "--retry-stalled-for", type=int, default=0, metavar="MS",
        help="AGY: retry a provably unsubmitted (stalled) prompt with bounded backoff; "
             "codex: one identical re-prompt on the first-prompt swallow signature; "
             "total delay budget, maximum 10000ms (default: no retry)")
    prompter.description = (
        "Exit 0 the text reached the seat (settled, landed_and_working, "
        "landed_unconfirmed, blocked); 1 it provably did not (seat_unsettled, "
        "never_landed); 2 delivery is indeterminate or went elsewhere (unknown, "
        "replaced). Read `outcome` and `next_action` for what to do; only 1 leaves the "
        "seat with nothing sent."
    )
    prompter.set_defaults(handler=command_prompt)

    pending = commands.add_parser(
        "pending", help="report seats this session left live with nothing listening")
    pending.add_argument("--clear", action="append", metavar="SEAT",
                         help="drop this seat's breadcrumb first; repeatable")
    pending.description = (
        "Exit 0 nothing is owed — no seat is recorded live, or a bus lease is alive and "
        "covers them all; 1 at least one recorded seat has no listener, which is a worker "
        "that will publish into an empty room. Arm "
        "`herdr-bus.py watch --subscriber chair --ttl 900` as a harness background task, "
        "or re-run `collect`."
    )
    pending.set_defaults(handler=command_pending)

    digester = commands.add_parser("digest", help="sha256 of a dispatch input")
    digester.add_argument("--file", default="-")
    digester.set_defaults(handler=command_digest)

    consumer = commands.add_parser(
        "consume", help="read once, verify the digest, and emit the verified bytes")
    consumer.add_argument("--file", required=True)
    consumer.add_argument("--digest", required=True)
    consumer.set_defaults(handler=command_consume)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    # RuntimeError is the unreachable-transport case. It must not exit 1: for `prompt`
    # that code means "the text did not land", which would read as safe to re-send.
    except (OSError, ValueError, KeyError, RuntimeError, CheckpointError) as error:
        print(f"agent-handoff: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
