#!/usr/bin/env python3

"""Sandbox-crossing visible-spawn path for herdr (HL-046).

A codex orchestrator root under herdr obeys the visibility contract but cannot execute
it: its sandbox denies connect(2) to herdr's control socket and codex 0.145 exposes no
TUI-reachable unix-socket allowlist, so `agent-teammate` is unreachable from inside. What
the sandbox still has is rename(2) inside its own writable cwd. That is the whole design
premise: the root publishes `requests/<key>.<pid>-<nonce>.json` by write plus rename, a watcher
running OUTSIDE any sandbox validates it and performs the real pane split and agent start,
and writes `panes/<key>.json` back. No socket, no sandbox weakening, no family knowledge —
any process holding a spawn capability can submit a request by renaming a file, and the host
decides what happens to it.

Validation is host-owned so a confused or buggy requester cannot turn a mistake into damage:
name and key shape, cwd resolved through symlinks and confined to an allowlist recorded at
`init`, argv restricted to a per-family flag allowlist with no positionals (a positional is
read as an initial prompt by every CLI herdr wraps). Request files are opened `O_NOFOLLOW`
relative to a directory fd and size capped. Where the host can re-derive a value it does,
rather than taking the request's word for it. See the threat model below for what this does
and does not defend.

## Requester capability contract

Stated precisely, because a contract that overstates itself is worse than none:

- **Successful publication mutates the filesystem with exactly two operations** — create and
  write a temp file in the drop directory, then `rename(2)` it into place. No other
  permission is required for it to succeed. In particular it needs neither `link(2)` nor
  `unlink(2)`: there is no success-path cleanup, because rename consumes the temp name and
  the host sweeps any stray a crashed writer leaves behind.
- **The client also reads**: existence checks on the claim and rejection records, and
  directory enumeration of the drop dir, before and after publishing.
- **The failure path may attempt one best-effort `unlink(2)`** of its own temp file. It is
  not required to succeed; a denial is swallowed and can never become the reported error, so
  a sandbox without unlink still behaves correctly — it merely leaves a temp for the host's
  sweep.

That narrowness is deliberate and load-bearing: the bridge exists because a sandboxed codex
root has rename and not much more, so anything successful publication depended on beyond it
would put the premise back at risk.

Collision safety comes from names, not from a create-if-absent primitive: every
submission is `requests/<key>.<pid>-<nonce>.json`, so two clients retrying the same key write different files and neither can
land on the other's. That is a guarantee, not a probability, for the case that matters:
concurrent clients have distinct pids by definition. The residual is a later client whose pid
was recycled from a dead one, which would additionally need its 64-bit nonce to repeat. The
host then picks one submission per key (lexicographically first, so every pass and every
watcher agrees) and records in the claim which file it actually read. Whether
`link(2)` is permitted inside codex's seatbelt was NOT established — eight permission-profile
config forms were tried against codex 0.145 and none produced a runnable sandboxed probe — so
the design does not assume it either way.
`test_successful_publication_needs_no_permission_beyond_write_and_rename` pins the first
bullet by running the real client with `os.link`, `os.unlink` and `os.remove` all raising
`EPERM`; `test_a_denied_failure_path_unlink_never_becomes_the_reported_error` pins the third.

Capabilities are host-minted and scoped to a per-`init` session nonce the host generates
(`root_id` is a human label two runs may reuse; the nonce is what stops a token from a
previous session being replayed in the next one). They carry a finite TTL — there is no
unlimited form.

NO CAPABILITY SECRET IS EVER WRITTEN TO DISK. A worker runs as the same uid as the watcher,
so file modes protect nothing from it and relocating a store cannot help. Instead `watch`
holds a master key in memory only — generated at start, or supplied once via stdin/env and
never persisted — derives each capability's secret from it (HKDF), and stores only
non-secret metadata plus a MAC keyed by the same master. A worker that reads the entire
store learns nothing forgeable, and one that writes to it cannot invent a grant, extend an
expiry or rebind a session. The trade is explicit: a fresh master key invalidates every
previously issued capability, so an operator who needs them to survive a restart re-supplies
the key rather than the registry quietly persisting it.

A request never carries a secret either: it presents `{cap_id, proof}`, where the proof is
an HMAC over the whole request minus the proof itself. The artifact left on disk therefore
authorizes exactly the one request it is, so a worker that reads its own
`requests/<key>.<pid>-<nonce>.json` back gains nothing — replaying it verbatim re-presents an
already-claimed key, and any edit breaks the proof. Workers are additionally spawned holding
a capability with NO grants, so a worker writing a fresh request is refused
`capability_no_spawn`. That is the leafness half of the visibility contract, enforced rather
than asked for.

Provenance is a host-attested chain, never temporal co-occurrence: the exact request bytes
are digested into an immutable `claims/<key>.json` BEFORE the spawn; `attestations/<key>.json`
then records the pane id, the occupant session and the foreground process group the host
itself observed at spawn time; only then is the pane record written. A spawn whose identity
cannot be observed does not become a pane record — the pane is closed and the request
refused, because a refusal is recoverable and a false success is not. `request --wait`
succeeds on an intact chain, not on the existence of a pane file.

Co-occurrence is never authority to DESTROY either. Restart adoption needs the attestation
to match, and an unattested claim — the crash-between-spawn-and-attestation case — is parked
as an orphan for a human rather than closing whichever agent happens to hold the predictable
name. Closing needs the same causal binding adopting does. A close that fails never settles
a key: the claim stays reconcilable and every pass tries again, because a terminal record
over a still-running pane strands it forever.

## Threat model

The requester is a CONTRACT-OBEYING agent that cannot reach herdr's control socket — a
capability gap, not an adversary. It may be buggy, confused, or wrong about what it is
asking for; it is not assumed to be attacking the host.

DEFENDED — mistakes, confusion, and the containment the visibility contract needs:
  - Leafness. A worker holds a capability with no grants, so a worker that writes a request
    is refused `capability_no_spawn` rather than quietly acquiring a pane of its own.
  - Capability integrity. Secrets are derived in memory and never written; stored metadata
    carries a MAC; a request presents a per-request proof, not a reusable bearer secret.
  - Quota. A host-only, MAC'd ledger is the authoritative count of outstanding work, so a
    confused agent cannot lose track of its own reservations and fan out without limit.
  - Accidental damage. Malformed or surprising input is refused, not interpreted: an argv
    flag allowlist with no positionals (every wrapped CLI reads one as an initial prompt),
    name and cwd shape checks, an allowlist confining cwd to configured trees, size caps,
    and `O_NOFOLLOW` reads so a stray symlink in the drop dir is never followed.
  - Provenance. Which request produced which pane is answerable under normal operation, and
    a spawn whose identity could not be observed is refused rather than published.
  - Non-destructive recovery. A pane that cannot be bound to a claim is never closed.

NOT DEFENDED — and deliberately not, since it is not the model:
  - A requester that deliberately manipulates the filesystem against the host: racing
    renames or symlinks on its own cwd between validation and use, or editing the in-tree
    `claims/`, `heartbeat/` and `panes/` mirrors to mislead the watcher. The cwd is
    sanity-checked once (resolved, must be a directory, must sit inside the allowlist) and
    then used; there is no attempt to win that race, because there is no racer in this
    model.

Adopting a hostile-writer posture later would need an fd-accepting herdr API — a split that
takes a directory handle rather than a path — plus authenticated in-tree records; the
filesystem drop-box cannot provide either from where it sits.

Failure is loud by construction. There is no native fallback: a request that is refused,
starved by the pane quota, or interrupted by a watcher restart gets a terminal
`rejected/<key>.json` carrying `no_native_fallback`, and `request --wait` exits non-zero.
A blocked request must surface as a failure, never degrade into an invisible worker.

Outstanding-work accounting lives in a HOST-ONLY ledger beside the capability metadata, MAC'd
the same way, and is the authoritative count. The `claims/` and `heartbeat/` files inside the
root are mirrors — convenient for humans and for the requester, but the host does not count
from them, so a stray deletion cannot release a slot whose pane is still running. A
reservation is released only on evidence: a terminal rejection, a published pane whose agent
is gone, or an explicit `release` by an operator.

Backpressure is on requests, not only panes: a global and per-capability outstanding cap, a
per-capability admission rate, and an inode/byte budget on the drop dir. Saturation is logged
once and writes nothing per key, so a runaway loop cannot turn its own mistake into unbounded
host-side storage; waiting clients fail on their own timeout. Refused requests' inputs are
reclaimed once they age past `--retention`.

Control-bus semantics, stated rather than papered over:
  - Delivery: `inbox/<key>/<seq>.json` relays one prompt through `herdr agent prompt
    --wait`, bounded by `--submit-timeout-ms`. It confirms UPTAKE (herdr requires an
    observed state change) or reports `stalled`; a timeout means the turn is still running
    and is recorded as `submitted`, not as failure. Completion is the artifact under
    `results/<key>/`, never the screen and never this result file.
  - Cancellation: `control/<key>/<seq>.json` with `cancel` closes the pane (herdr kills the
    process tree) or `interrupt` sends ctrl-c. There is no way to deliver an arbitrary
    signal to the worker process, and no exit status is observable.
  - Liveness: `heartbeat/<key>.json` is a poll at `--interval`, so state is stale by up to
    one interval and a worker whose entire lifetime falls inside one interval is invisible.

Subcommands: `init`, `serve-capability`, `watch`, `launch`, `release`, `status`, `chain`
(host side, outside any sandbox); `request` (client side, usable from a plain shell that has
only write and rename).

## Operator start

The master key is held in memory and never written, so it has to come from the operator.
One command creates the root, starts the watcher and issues the orchestrator's capability —
run it from a herdr pane, with fd 3 redirected so the bearer token never enters the log:

    export HERDR_REGISTRY_MASTER_KEY=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
    ~/.claude/scripts/herdr-registry.py watch \
        --root /tmp/herdr-registry/$CLAUDE_CODE_SESSION_ID \
        --init-root-id run1 --allow-cwd ~/example/repos \
        --master-key env --issue orchestrator --issue-fd 3 3>/tmp/registry-token

Then wire a codex root to it, in the same shell so it inherits the master key:

    ~/.claude/scripts/herdr-registry.py launch codex root-1 \
        --root /tmp/herdr-registry/$CLAUDE_CODE_SESSION_ID --cwd ~/dotfiles

`launch` mints that root's own spawn capability and passes it into the pane environment, so
the root's SessionStart contract can tell it to delegate through `request` and the capability
is already there when it does. The token is never printed; `/tmp/registry-token` from the
first command is only needed if something outside a launched pane has to submit requests.
"""

# A sandboxed root may run `request` with whatever python3 its shell resolves —
# /usr/bin/python3 is 3.9 on macOS — so this module stays 3.9-compatible.
from __future__ import annotations

import argparse
import dataclasses
import errno
import fcntl
import hashlib
import hmac
import itertools
import json
import math
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time


SCHEMA_VERSION = 1
HELPER = os.path.abspath(__file__)

MANIFEST = "registry.json"
WATCHER_LOCK = "watcher.lock"
REQUESTS = "requests"
CLAIMS = "claims"
ATTESTATIONS = "attestations"
PANES = "panes"
REJECTED = "rejected"
HEARTBEAT = "heartbeat"
INBOX = "inbox"
RESULTS = "results"
CONTROL = "control"
SUBDIRS = (REQUESTS, CLAIMS, ATTESTATIONS, PANES, REJECTED, HEARTBEAT, INBOX, RESULTS, CONTROL)

HKDF_SALT = b"herdr-registry/v1"
MASTER_KEY_ENV = "HERDR_REGISTRY_MASTER_KEY"

DEFAULT_STORE = os.path.expanduser("~/.claude/herdr-registry/capabilities")
DEFAULT_MAX_PANES = 4
DEFAULT_INTERVAL_SECONDS = 1.0
DEFAULT_QUEUE_TIMEOUT_SECONDS = 900.0
DEFAULT_SUBMIT_TIMEOUT_MS = 15000
DEFAULT_TTL_SECONDS = 86400.0
RESERVATIONS = "reservations"
DEFAULT_RETENTION_SECONDS = 3600.0
DEFAULT_TEMP_RETENTION_SECONDS = 300.0
DEFAULT_ROLE = "worker"
# The seat each family spawns with when the request names none. `agent-teammate` has
# required `--role` since dotfiles dd804cc, and the role IS the launch policy (ADR 0009):
# the host picks it, never the sandboxed requester, which is also why `--model` in a
# request cannot survive beside one. A family with no entry here has to be told a role.
FAMILY_ROLES = {"claude": "code.routine", "codex": "code.routine", "agy": "code.routine"}

# Storage and attention budgets: a requester that can rename into `requests/` can
# otherwise mint unbounded queue entries, directory entries and per-pass stat() work.
DEFAULT_MAX_OUTSTANDING = 32
DEFAULT_MAX_OUTSTANDING_PER_CAPABILITY = 8
DEFAULT_MAX_REQUEST_FILES = 256
DEFAULT_MAX_REQUEST_DIR_BYTES = 4 * 1024 * 1024
DEFAULT_RATE_LIMIT = 30
DEFAULT_RATE_WINDOW_SECONDS = 60.0

MAX_REQUEST_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 256 * 1024
MAX_ARGV_ITEMS = 16
AGENT_NAME_LIMIT = 32

KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CAP_ID_PATTERN = re.compile(r"^cap-[0-9a-f]{16}$")
SECRET_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_PATTERN = re.compile(r"^[0-9a-f]{16}$")
SEQ_PATTERN = re.compile(r"^[0-9]{1,9}$")
NONCE_PATTERN = re.compile(r"^[0-9a-f]{1,16}-[0-9a-f]{16}$")
TEMP_PATTERN = re.compile(r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,96}\.tmp$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

REQUEST_FIELDS = (
    "schema_version", "idempotency_key", "capability", "family", "name", "cwd", "argv", "role",
)
FAMILIES = ("claude", "codex", "agy")
SPAWN = "spawn"

# Values a sandboxed writer may choose. Everything absent is host policy on purpose:
# `--sandbox`, `--config`/`-c`, `--dangerously-*` and bare positionals are exactly the
# flags a confused requester is most likely to reach for; widening it is a host-side change.
FLAG_VALUES = {
    "--model": TOKEN_PATTERN,
    "--profile": TOKEN_PATTERN,
    "--permission-mode": frozenset({"default", "acceptEdits", "plan"}),
}
ARGV_POLICY = {
    "claude": ("--model", "--profile", "--permission-mode"),
    "codex": ("--model",),
    "agy": ("--model",),
}

QUEUED = "queued"
RUNNING = "running"
GONE = "gone"
ORPHANED = "orphaned"
CANCELLED = "cancelled"
REJECTED_STATE = "rejected"

ADMIT = "admit"
QUEUE = "queue"
REFUSE = "refuse"

SETTLED_STATES = ("idle", "done")
BLOCKED_STATUS = "blocked"

NO_FALLBACK_GUIDANCE = (
    "This spawn request was refused. Do NOT fall back to a provider-native in-process "
    "agent, a background worker, or serial self-execution: report the failure to the "
    "orchestrator and stop."
)

STALL_MARKER = "agent_prompt_stalled"
TIMEOUT_MARKERS = ("timeout", "timed out")

_TEMP_SEQUENCE = itertools.count()


class RegistryError(Exception):
    """Operator-facing failure: bad invocation, unusable root, lock already held."""


class RequestError(Exception):
    """A request the host refuses. `reason` is a stable token written into `rejected/`."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def byte_digest(raw):
    return hashlib.sha256(raw).hexdigest()


def run(command, env=None):
    return subprocess.run(command, capture_output=True, text=True, check=False, env=env)


def _write_durably(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path):
    # Best effort: the record is already published by the time this runs, and some
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


def encode(document):
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def publish_once(path, document):
    """Publish an immutable record, failing rather than clobbering an existing one.

    link() is atomic AND refuses an existing name, so a second watcher racing the same
    request cannot overwrite the first one's attestation.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(
        directory, f".{os.path.basename(path)}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp"
    )
    try:
        _write_durably(temp, encode(document))
        os.link(temp, path)
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
    _fsync_directory(directory)
    return path


def save_mutable(path, document):
    """Publish a record that legitimately changes over time (heartbeats, manifests)."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temp = f"{path}.{os.getpid()}.{next(_TEMP_SEQUENCE)}.tmp"
    try:
        _write_durably(temp, encode(document))
        os.replace(temp, path)
    except OSError:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(directory)
    return path


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        raise RegistryError(f"{path} is unreadable: {error}")
    if not isinstance(document, dict):
        raise RegistryError(f"{path} is not a JSON object")
    return document


def read_bounded(dir_fd, name, limit):
    """Read a drop-box file without following symlinks and without trusting its size."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise RequestError("symlink")
        if error.errno == errno.ENOENT:
            raise RequestError("vanished")
        raise RequestError(f"unreadable:{error.errno}")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RequestError("not_a_regular_file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RequestError("oversize")
            chunks.append(chunk)
    finally:
        os.close(fd)
    return b"".join(chunks)


def open_dir(path):
    """Directory fd for `*at` reads, so a swapped or symlinked drop dir cannot redirect us."""
    try:
        return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise RegistryError(f"{path} is not a usable directory: {error}")


def is_string(value):
    return isinstance(value, str)


def is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# --- request validation (pure) ---------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Request:
    key: str
    cap_id: str
    proof: str
    material: str
    family: str
    name: str
    cwd: str
    argv: tuple
    role: str


def proof_material(document):
    """Canonical bytes a request's proof covers: the whole request minus the proof itself.

    Everything else is covered, nested capability fields included. Reconstructing a
    stripped-down capability object here would leave future capability metadata
    silently editable the moment anything started reading it.
    """
    body = {key: value for key, value in document.items() if key != "capability"}
    capability = document.get("capability")
    if isinstance(capability, dict):
        body["capability"] = {
            key: value for key, value in capability.items() if key != "proof"
        }
    else:
        body["capability"] = capability
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def material_proof(secret, material):
    return hmac.new(
        secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def request_proof(secret, document):
    """Bind a capability to ONE request. The on-disk artifact then re-authorizes nothing:
    a worker that reads it back can only re-present the same already-claimed key, and any
    edit — a different name, cwd, argv or key — invalidates the proof."""
    return material_proof(secret, proof_material(document))


def submission_name(key, pid, nonce):
    """`<key>.<pid>-<nonce>.json`.

    The pid is what makes concurrent publication non-colliding rather than merely unlikely:
    two clients running at the same time on one host have different pids by definition, so
    their final names differ no matter what their random material does. The nonce covers the
    remaining case — a later client whose pid was recycled from a dead one — where a
    same-name collision would additionally need 64 bits to repeat.
    """
    return f"{key}.{pid:x}-{nonce}.json"


def submission_key(name):
    """`<key>.<pid>-<nonce>.json` -> key, or None when the name is not a submission."""
    if not name.endswith(".json") or name.startswith("."):
        return None
    key, _, nonce = name[:-len(".json")].rpartition(".")
    if not key or not NONCE_PATTERN.match(nonce) or not KEY_PATTERN.match(key):
        return None
    return key


def submissions(directory, key):
    """Every submission for `key`, in lexical name order.

    Name order is arbitrary with respect to time — it is chosen because it is stable, so
    two watchers or two passes over one drop dir always agree on which submission is the
    request, not because the first is the oldest.
    """
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return []
    return [os.path.join(directory, name) for name in names if submission_key(name) == key]


def parse_request(raw, key):
    """Structural check of untrusted request bytes. No filesystem, no clock, no spawn."""
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError("unparsable")
    if not isinstance(document, dict):
        raise RequestError("not_an_object")
    missing = [field for field in REQUEST_FIELDS if field not in document]
    if missing:
        raise RequestError("missing:" + ",".join(missing))
    if not is_int(document["schema_version"]) or document["schema_version"] != SCHEMA_VERSION:
        raise RequestError("schema_version")
    if document["idempotency_key"] != key:
        raise RequestError("key_mismatch")
    capability = document["capability"]
    if not isinstance(capability, dict):
        raise RequestError("bad_capability:shape")
    cap_id = capability.get("cap_id")
    proof = capability.get("proof")
    if not is_string(cap_id) or not CAP_ID_PATTERN.match(cap_id):
        raise RequestError("bad_capability:cap_id")
    if not is_string(proof) or not DIGEST_PATTERN.match(proof):
        raise RequestError("bad_capability:proof")
    if "secret" in capability:
        # A bearer secret on disk is exactly the artifact this design refuses to keep.
        raise RequestError("bad_capability:secret_present")
    if set(capability) - {"cap_id", "proof"}:
        # Signed but unread fields are a fail-open migration hazard; refuse them outright.
        raise RequestError("bad_capability:unknown_field")
    if document["family"] not in FAMILIES:
        raise RequestError("bad_family")
    if not is_string(document["name"]) or not NAME_PATTERN.match(document["name"]):
        raise RequestError("bad_name")
    if not is_string(document["role"]) or not ROLE_PATTERN.match(document["role"]):
        raise RequestError("bad_role")
    if not is_string(document["cwd"]):
        raise RequestError("bad_cwd:type")
    return Request(
        key=key, cap_id=cap_id, proof=proof, material=proof_material(document),
        family=document["family"], name=document["name"], cwd=document["cwd"],
        argv=tuple(_argv_items(document["argv"])), role=document["role"],
    )


def _argv_items(argv):
    if not isinstance(argv, list) or len(argv) > MAX_ARGV_ITEMS:
        raise RequestError("bad_argv:shape")
    for item in argv:
        if not is_string(item) or "\0" in item or "\n" in item:
            raise RequestError("bad_argv:type")
    return argv


def check_argv(family, argv, profile_label=None):
    """Flag allowlist per family. Positionals are refused: every wrapped CLI reads one as
    an initial prompt, which is a free prompt-injection channel from inside the sandbox."""
    allowed = ARGV_POLICY[family]
    normalized = []
    seen = set()
    index = 0
    while index < len(argv):
        flag = argv[index]
        if not flag.startswith("--"):
            raise RequestError("bad_argv:positional")
        if "=" in flag:
            raise RequestError("bad_argv:joined_value")
        if flag not in allowed:
            raise RequestError("bad_argv:flag_not_allowed")
        if flag in seen:
            raise RequestError("bad_argv:duplicate_flag")
        if index + 1 >= len(argv):
            raise RequestError("bad_argv:missing_value")
        value = argv[index + 1]
        rule = FLAG_VALUES[flag]
        ok = value in rule if isinstance(rule, frozenset) else bool(rule.match(value))
        if not ok:
            raise RequestError("bad_argv:value")
        seen.add(flag)
        normalized += [flag, value]
        index += 2
    if family == "claude" and "--profile" not in seen:
        # The claude shell wrapper herdr prepends requires --profile and cannot inherit it
        # from the chair's environment (panes spawn from the herdr server).
        if not profile_label or not TOKEN_PATTERN.match(profile_label):
            raise RequestError("bad_argv:profile_unavailable")
        normalized += ["--profile", profile_label]
    return tuple(normalized)


def spawn_role(family, requested=None, override=None):
    """The roles.json seat this spawn launches with.

    The request's own role wins, then the host override (`watch --default-role`,
    `launch --role`), then the family default. `worker` never wins: it is the audit
    placeholder `request --role` fills in, not a seat, and spawning with it would fail at
    routing. A family with neither an override nor a default is refused here rather than a
    moment later inside `agent-teammate`, because a refusal the requester can read beats a
    spawn failure it cannot.
    """
    for candidate in ((requested or "").strip(), (override or "").strip(),
                      FAMILY_ROLES.get(family, "")):
        if candidate and candidate != DEFAULT_ROLE:
            return candidate
    raise RequestError(f"no_role:{family}")


def resolve_cwd(cwd, allow_roots):
    """Resolve through symlinks, then confine to the allowlist recorded at `init`."""
    if not allow_roots:
        raise RequestError("bad_cwd:no_allowlist")
    if "\0" in cwd or not cwd.startswith("/"):
        raise RequestError("bad_cwd:not_absolute")
    resolved = os.path.realpath(cwd)
    if not os.path.isdir(resolved):
        raise RequestError("bad_cwd:not_a_directory")
    for root in allow_roots:
        base = os.path.realpath(root)
        if resolved == base or resolved.startswith(base.rstrip("/") + "/"):
            return resolved
    raise RequestError("bad_cwd:outside_allowlist")


def agent_name_for(root_id, name):
    """Host-owned namespace, so a request cannot name-squat an unrelated live agent."""
    prefix = re.sub(r"[^a-z0-9]", "", root_id.lower())[:6] or "reg"
    candidate = f"r{prefix}-{name}"[:AGENT_NAME_LIMIT]
    if not NAME_PATTERN.match(candidate):
        raise RequestError("bad_name")
    return candidate


def admission(live, max_panes, queued_since, now, queue_timeout):
    """Backpressure decision. Starvation is terminal and loud, never a silent downgrade."""
    if live < max_panes:
        return ADMIT, None
    if queued_since is None:
        return QUEUE, None
    if now - queued_since >= queue_timeout:
        return REFUSE, "quota_timeout"
    return QUEUE, None


# --- capabilities ----------------------------------------------------------------------


def capability_dir(store, root_id, session):
    """Session-scoped, so a later root that reuses `root_id` cannot even see the old keys."""
    return os.path.join(store, f"{root_id}-{session}")


def capability_path(store, root_id, session, cap_id):
    return os.path.join(capability_dir(store, root_id, session), f"{cap_id}.json")


def record_material(record):
    """Canonical bytes a stored record's MAC covers: the record minus the MAC itself."""
    return json.dumps(
        {key: value for key, value in record.items() if key != "mac"},
        sort_keys=True, separators=(",", ":"),
    )


def expand(master, info, length=32):
    """HKDF-Expand (RFC 5869) over an extracted PRK. Stdlib only, no key material stored."""
    prk = hmac.new(HKDF_SALT, master, hashlib.sha256).digest()
    output = b""
    block = b""
    counter = 1
    while len(output) < length:
        block = hmac.new(prk, block + info.encode("utf-8") + bytes([counter]), hashlib.sha256)
        block = block.digest()
        output += block
        counter += 1
    return output[:length]


class Keyring:
    """Capability secrets exist only in this process's memory.

    File modes are the wrong tool here: a sandboxed worker runs as the SAME uid, so 0600
    stops nobody who can read at all, and relocating the store cannot fix that. So nothing
    secret is written. A master key is generated at `watch` start (or supplied once by the
    operator through stdin/env and never persisted), each capability's secret is derived
    from it, and the store holds only non-secret metadata plus a MAC. A worker that reads
    the entire store therefore learns nothing it can forge with, and a worker that WRITES
    to the store cannot invent a grant, extend an expiry, or rebind a session, because the
    MAC is keyed by material it does not have.

    The cost is explicit: a fresh master key invalidates every capability issued under the
    previous one. Capabilities do not survive a watcher restart unless the operator
    re-supplies the same key.
    """

    def __init__(self, master):
        if not isinstance(master, bytes) or len(master) < 32:
            raise RegistryError("master key must be at least 32 bytes")
        self._master = master

    @classmethod
    def generate(cls):
        return cls(secrets.token_bytes(32))

    @classmethod
    def from_hex(cls, text):
        try:
            master = bytes.fromhex((text or "").strip())
        except ValueError:
            raise RegistryError("master key must be hex")
        return cls(master)

    def secret(self, root_id, session, cap_id):
        return expand(self._master, f"capability:{root_id}:{session}:{cap_id}").hex()

    def record_mac(self, record):
        key = expand(
            self._master,
            f"record:{record.get('root_id')}:{record.get('session')}:{record.get('cap_id')}",
        )
        return hmac.new(key, record_material(record).encode("utf-8"), hashlib.sha256).hexdigest()

    def mint(self, store, root_id, session, subject, grants,
             ttl=DEFAULT_TTL_SECONDS, max_panes=None, now=None):
        now = time.time() if now is None else now
        cap_id = "cap-" + secrets.token_hex(8)
        record = {
            "schema_version": SCHEMA_VERSION,
            "cap_id": cap_id,
            "root_id": root_id,
            "session": session,
            "subject": subject,
            "grants": sorted(set(grants)),
            "issued_at": now,
            "expires_at": now + ttl,
            "max_panes": max_panes,
        }
        record["mac"] = self.record_mac(record)
        directory = capability_dir(store, root_id, session)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        path = capability_path(store, root_id, session, cap_id)
        publish_once(path, record)
        os.chmod(path, 0o600)
        return record, self.secret(root_id, session, cap_id)

    def open(self, store, root_id, session, cap_id):
        """Returns (record, rejection). A record whose MAC fails is not a record."""
        try:
            record = load_json(capability_path(store, root_id, session, cap_id))
        except RegistryError:
            return None, "capability_unknown"
        if record is None:
            return None, "capability_unknown"
        if not hmac.compare_digest(str(record.get("mac", "")), self.record_mac(record)):
            return None, "capability_tampered"
        return record, None


class Ledger:
    """Authoritative outstanding-work accounting, outside every sandbox-writable path.

    The in-tree `claims/` and `heartbeat/` files are advisory MIRRORS. Counting from them
    was a bypass: the registry root is writable by the requester, so deleting one claim
    zeroed the count while its pane kept running, and repeating that admitted panes without
    limit. Reservations therefore live beside the capability metadata — a host-only store —
    and carry the same MAC, so a process that somehow reaches the directory still cannot
    forge or re-attribute one. A reservation that cannot be authenticated keeps counting,
    against a sentinel capability, because failing closed on the quota is the safe direction.
    """

    UNKNOWN_CAPABILITY = "unauthenticated"
    MALFORMED = "malformed"

    def __init__(self, store, root_id, session, keyring):
        self.directory = os.path.join(
            capability_dir(store, root_id, session), RESERVATIONS
        )
        self.root_id = root_id
        self.session = session
        self.keyring = keyring

    def path(self, key):
        return os.path.join(self.directory, f"{key}.json")

    def reserve(self, key, capability, state, agent_name=None, now=None, queued_since=None,
                request_path=None):
        record = {
            "schema_version": SCHEMA_VERSION,
            "root_id": self.root_id,
            "session": self.session,
            "cap_id": key,
            "key": key,
            "capability": capability,
            "state": state,
            "agent_name": agent_name,
            "queued_since": queued_since,
            "request_path": request_path,
            "reserved_at": time.time() if now is None else now,
        }
        record["mac"] = self.keyring.record_mac(record)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        save_mutable(self.path(key), record)
        os.chmod(self.path(key), 0o600)
        return record

    def release(self, key):
        try:
            os.unlink(self.path(key))
            return True
        except FileNotFoundError:
            return False

    def get(self, key):
        for entry in self.entries():
            if entry.get("key") == key:
                return entry
        return None

    def entries(self):
        try:
            names = sorted(os.listdir(self.directory))
        except FileNotFoundError:
            return []
        found = []
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            key = name[:-len(".json")]
            try:
                record = load_json(self.path(key))
            except RegistryError:
                record = None
            if record is None:
                # Ordinary disk damage or an upgrade leftover: hold the slot rather
                # than letting an unreadable file quietly release capacity.
                found.append({"key": key, "capability": self.UNKNOWN_CAPABILITY,
                              "state": self.MALFORMED, "authentic": False})
                continue
            authentic = hmac.compare_digest(
                str(record.get("mac", "")), self.keyring.record_mac(record)
            )
            found.append(dict(
                record,
                capability=record.get("capability") if authentic else self.UNKNOWN_CAPABILITY,
                authentic=authentic,
            ))
        return found

    def counts(self):
        per_capability = {}
        for entry in self.entries():
            capability = entry.get("capability")
            per_capability[capability] = per_capability.get(capability, 0) + 1
        return sum(per_capability.values()), per_capability


def check_capability(record, cap_id, proof, material, root_id, session, grant, now, secret):
    """Pure authorization check. Returns a rejection token, or None when authorized."""
    if not isinstance(record, dict):
        return "capability_unknown"
    if record.get("cap_id") != cap_id or record.get("root_id") != root_id:
        return "capability_unknown"
    if record.get("session") != session:
        return "capability_wrong_session"
    expires = record.get("expires_at")
    if not isinstance(expires, (int, float)) or isinstance(expires, bool) \
            or not math.isfinite(expires) or now > expires:
        return "capability_expired"
    if not hmac.compare_digest(material_proof(secret, material), proof):
        return "capability_bad_proof"
    grants = record.get("grants")
    if not isinstance(grants, list) or grant not in grants:
        return f"capability_no_{grant}"
    return None


# --- registry root ---------------------------------------------------------------------


class Root:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        manifest = load_json(os.path.join(self.path, MANIFEST))
        if manifest is None:
            raise RegistryError(f"{self.path} is not a registry root; run `init` first")
        root_id = manifest.get("root_id")
        if not is_string(root_id) or not KEY_PATTERN.match(root_id):
            raise RegistryError(f"{self.path}/{MANIFEST} has no usable root_id")
        session = manifest.get("session")
        if not is_string(session) or not SESSION_PATTERN.match(session):
            raise RegistryError(f"{self.path}/{MANIFEST} has no usable session nonce")
        allow = manifest.get("allow_cwd")
        if not isinstance(allow, list) or not all(is_string(item) for item in allow):
            raise RegistryError(f"{self.path}/{MANIFEST} has no usable allow_cwd list")
        self.manifest = manifest
        self.root_id = root_id
        self.session = session
        self.allow_roots = tuple(allow)

    def dir(self, *parts):
        return os.path.join(self.path, *parts)

    def record(self, kind, key):
        return self.dir(kind, f"{key}.json")


def init_root(path, root_id, allow_cwd):
    if not KEY_PATTERN.match(root_id):
        raise RegistryError(f"root id {root_id!r} is not a safe identity")
    resolved = []
    for entry in allow_cwd:
        real = os.path.realpath(os.path.expanduser(entry))
        if not os.path.isdir(real):
            raise RegistryError(f"--allow-cwd {entry} is not a directory")
        resolved.append(real)
    path = os.path.abspath(path)
    os.makedirs(path, mode=0o700, exist_ok=True)
    for name in SUBDIRS:
        os.makedirs(os.path.join(path, name), mode=0o700, exist_ok=True)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "root_id": root_id,
        # Host-minted, never caller-supplied: `root_id` is a human label and two runs may
        # reuse it, so the nonce is what makes a capability un-replayable across sessions.
        "session": secrets.token_hex(8),
        "allow_cwd": resolved,
        "created_at": time.time(),
    }
    try:
        publish_once(os.path.join(path, MANIFEST), manifest)
    except FileExistsError:
        raise RegistryError(f"{path} is already a registry root")
    return manifest


# --- herdr host facade -----------------------------------------------------------------


class Host:
    """Every privileged action, in one place, running outside any sandbox."""

    def __init__(self, binary=None, teammate=None, anchor=None):
        self.binary = binary or os.environ.get("AGENT_HARNESS_HERDR_BIN", "herdr")
        # Same shape as the herdr binary above: a test drives the real spawn path through a
        # helper of its own, and `launch` builds its Host with no arguments at all.
        self.teammate = teammate or os.environ.get("AGENT_HARNESS_TEAMMATE") or \
            os.path.join(os.path.dirname(HELPER), "agent-teammate.py")
        self.anchor = anchor or os.environ.get("HERDR_PANE_ID")

    def child_env(self, **overrides):
        """The master key never leaves this process, so it never reaches a child either."""
        env = {key: value for key, value in os.environ.items() if key != MASTER_KEY_ENV}
        env.update(overrides)
        return env

    def _json(self, command, operation):
        result = run([self.binary, *command], env=self.child_env())
        if result.returncode != 0:
            raise RegistryError(
                f"{operation} failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RegistryError(f"{operation} returned invalid JSON")
        return payload.get("result", payload)

    def agents(self):
        """Live named agents, keyed by herdr agent name."""
        payload = self._json(["agent", "list"], "herdr agent list")
        agents = payload.get("agents") if isinstance(payload, dict) else None
        found = {}
        for entry in agents or []:
            name = entry.get("name") if isinstance(entry, dict) else None
            if is_string(name):
                found[name] = entry
        return found

    def process_info(self, pane):
        try:
            payload = self._json(["pane", "process-info", "--pane", pane], "herdr pane process-info")
        except RegistryError:
            return None
        info = payload.get("process_info") if isinstance(payload, dict) else None
        if not isinstance(info, dict):
            return None
        return {
            "foreground_process_group_id": info.get("foreground_process_group_id"),
            "foreground_processes": [
                {"pid": item.get("pid"), "argv0": item.get("argv0")}
                for item in (info.get("foreground_processes") or [])
                if isinstance(item, dict)
            ][:8],
        }

    def spawn(self, family, name, cwd, argv, env, role):
        if not self.anchor:
            raise RegistryError("HERDR_PANE_ID is missing; the watcher has no anchor pane")
        command = [sys.executable, self.teammate, family, name, "--cwd", cwd, "--role", role]
        for key, value in sorted(env.items()):
            command += ["--env", f"{key}={value}"]
        command += ["--", *argv]
        result = run(command, env=self.child_env(HERDR_PANE_ID=self.anchor))
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "agent-teammate failed"
            raise RequestError(f"spawn_failed:{message[:200]}")
        try:
            spawned = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise RequestError("spawn_failed:agent-teammate returned invalid JSON")
        pane = spawned.get("pane")
        if not is_string(pane):
            raise RequestError("spawn_failed:no pane id")
        return spawned

    def prompt(self, name, text, timeout_ms):
        result = run([
            self.binary, "agent", "prompt", name, text, "--wait", "--timeout", str(timeout_ms),
        ], env=self.child_env())
        message = f"{result.stderr}{result.stdout}".lower()
        if result.returncode == 0:
            outcome = "settled"
        elif STALL_MARKER in message:
            outcome = "stalled"
        elif any(marker in message for marker in TIMEOUT_MARKERS):
            outcome = "submitted"
        else:
            outcome = "failed"
        return outcome, f"{result.stderr}{result.stdout}".strip()[:500]

    def send_keys(self, name, *keys):
        result = run([self.binary, "agent", "send-keys", name, *keys], env=self.child_env())
        return result.returncode == 0, f"{result.stderr}{result.stdout}".strip()[:500]

    def close_pane(self, pane):
        result = run([self.binary, "pane", "close", pane], env=self.child_env())
        return result.returncode == 0, f"{result.stderr}{result.stdout}".strip()[:500]


# --- watcher ---------------------------------------------------------------------------


class Watcher:
    def __init__(self, root, host, keyring, *, store=DEFAULT_STORE, max_panes=DEFAULT_MAX_PANES,
                 queue_timeout=DEFAULT_QUEUE_TIMEOUT_SECONDS,
                 submit_timeout_ms=DEFAULT_SUBMIT_TIMEOUT_MS, profile_label=None,
                 default_role=None, retention=DEFAULT_RETENTION_SECONDS,
                 temp_retention=DEFAULT_TEMP_RETENTION_SECONDS,
                 max_outstanding=DEFAULT_MAX_OUTSTANDING,
                 max_outstanding_per_capability=DEFAULT_MAX_OUTSTANDING_PER_CAPABILITY,
                 max_request_files=DEFAULT_MAX_REQUEST_FILES,
                 max_request_dir_bytes=DEFAULT_MAX_REQUEST_DIR_BYTES,
                 rate_limit=DEFAULT_RATE_LIMIT, rate_window=DEFAULT_RATE_WINDOW_SECONDS,
                 clock=time.time):
        self.root = root
        self.host = host
        self.keyring = keyring
        self.store = store
        self.ledger = Ledger(store, root.root_id, root.session, keyring)
        self.max_panes = max_panes
        self.queue_timeout = queue_timeout
        self.submit_timeout_ms = submit_timeout_ms
        self.profile_label = profile_label
        self.default_role = default_role
        self.retention = retention
        self.temp_retention = temp_retention
        self.max_outstanding = max_outstanding
        self.max_outstanding_per_capability = max_outstanding_per_capability
        self.max_request_files = max_request_files
        self.max_request_dir_bytes = max_request_dir_bytes
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.clock = clock
        self.events = []
        self._ignored = set()
        self._rate = {}
        self._saturated = set()
        self._orphans = set()
        self._pending_close = {}

    def log(self, event, **fields):
        record = dict(fields, event=event, at=self.clock())
        self.events.append(record)
        print(json.dumps(record, sort_keys=True), flush=True)

    # -- record helpers

    def settled(self, key):
        return os.path.exists(self.root.record(PANES, key)) or \
            os.path.exists(self.root.record(REJECTED, key))

    def heartbeat(self, key):
        try:
            return load_json(self.root.record(HEARTBEAT, key))
        except RegistryError:
            return None

    def beat(self, key, state, **fields):
        record = dict(fields, state=state, key=key, observed_at=self.clock())
        save_mutable(self.root.record(HEARTBEAT, key), record)
        return record

    def reject(self, key, reason, **fields):
        record = dict(
            fields,
            schema_version=SCHEMA_VERSION,
            root_id=self.root.root_id,
            key=key,
            reason=reason,
            rejected_at=self.clock(),
            no_native_fallback=True,
            guidance=NO_FALLBACK_GUIDANCE,
        )
        try:
            publish_once(self.root.record(REJECTED, key), record)
        except FileExistsError:
            return
        self.ledger.release(key)
        self.beat(key, REJECTED_STATE, reason=reason)
        self.log("rejected", key=key, reason=reason)

    # -- passes

    def reconcile(self, agents):
        """Watcher-restart recovery: a claim with no pane record is decided against live
        state and a spawn attestation, never re-spawned. Absence of an agent cannot be told
        apart from a pane that was born and died, and a blind retry would duplicate a
        visible worker.

        Adoption needs proof of identity, not a matching name. `attestations/<key>.json`
        records the pane id and the occupant the host observed at spawn time, and all of it
        must still hold. A claim with no attestation — the watcher died before or during the
        spawn — is unprovable and refused, because a live agent that merely carries the
        predictable name is co-occurrence, which is the thing this design refuses to accept.
        """
        for name in sorted(os.listdir(self.root.dir(CLAIMS))):
            if not name.endswith(".json") or name.startswith("."):
                continue
            key = name[:-len(".json")]
            if self.settled(key) or key in self._pending_close:
                continue
            claim = load_json(self.root.record(CLAIMS, key))
            if claim is None:
                continue
            attestation = load_json(self.root.record(ATTESTATIONS, key))
            refusal = self.adoption_refusal(claim, attestation, agents)
            if refusal:
                self.quarantine(key, claim, attestation, agents, refusal)
                continue
            self.publish_pane(key, claim, attestation, adopted=True)
            self.log("adopted", key=key, agent=claim.get("agent_name"))

    def quarantine(self, key, claim, attestation, agents, refusal):
        """Dispose of a claim whose worker could not be adopted, without ever guessing.

        A close is destructive, so it needs the SAME causal binding adoption needs. Without
        an attestation there is no such binding: the live agent merely carries the
        predictable claim name, which is exactly the co-occurrence adoption refuses — using
        it as authority to kill would be strictly worse than refusing to adopt on it. So an
        unattested claim is parked as an orphan for a human, never closed and never
        terminally rejected, and stays reconcilable.

        An attested pane IS bound: this watcher recorded that pane id after its own spawn,
        so closing it is warranted when its identity cannot be confirmed. A live agent whose
        occupant does not match the attestation is somebody else's and is left alone.
        """
        if refusal == "no_attestation":
            agent = agents.get(claim.get("agent_name"))
            self.orphan(key, (agent or {}).get("pane_id"), "unattested_claim",
                        agent_name=claim.get("agent_name"), live=agent is not None)
            return
        if refusal == "identity_unprovable" and (attestation or {}).get("pane_id"):
            self.retire(key, attestation["pane_id"], "watcher_restart_incomplete",
                        claim.get("request_digest"), detail=refusal)
            return
        self.reject(key, "watcher_restart_incomplete", detail=refusal, closed_pane=None,
                    request_digest=claim.get("request_digest"))

    def adoption_refusal(self, claim, attestation, agents):
        """Why this claim's worker cannot be proven to be a live agent, or None."""
        if attestation is None:
            return "no_attestation"
        agent = agents.get(claim.get("agent_name"))
        if agent is None:
            return "agent_absent"
        if agent.get("pane_id") != attestation.get("pane_id"):
            return "pane_mismatch"
        attested = attestation.get("occupant")
        if attested is not None:
            observed = (agent.get("agent_session") or {}).get("value")
            return None if observed == attested else "occupant_mismatch"
        group = attestation.get("process_group")
        if group is None:
            return "identity_unprovable"
        current = self.host.process_info(attestation.get("pane_id")) or {}
        if current.get("foreground_process_group_id") != group:
            return "process_group_mismatch"
        return None

    def pass_once(self):
        agents = self.host.agents()
        self.retry_closes()
        self.reclaim(self.clock())
        live = self.refresh_heartbeats(agents)
        # Every pass, not just at startup: a claim left unsettled by an orphan or a failed
        # close has to keep getting another look, or "reconcilable" is only a word.
        self.reconcile(agents)
        self.process_requests(agents, live)
        self.process_inbox(agents)
        self.process_control(agents)

    def reclaim(self, now):
        """Unlink aged-out request inputs, refused AND accepted alike.

        Leaving accepted inputs in place wedges the watcher: once the drop dir hits its
        inode budget, no further request is read at all. A claimed request's bytes are
        already committed to the immutable claim as `request_digest`, so the input is
        redundant once claimed; a refused request's rejection record is its trace. Neither
        removal weakens provenance, and `chain` treats a reclaimed input as expected.
        """
        removed = self.reclaim_temps(now) + self.reclaim_submissions(now)
        for kind, stamp, extra in ((REJECTED, "rejected_at", (HEARTBEAT,)),):
            for name in sorted(os.listdir(self.root.dir(kind))):
                if not name.endswith(".json") or name.startswith("."):
                    continue
                key = name[:-len(".json")]
                leftovers = [
                    path for path in (self.root.record(other, key) for other in extra)
                    if os.path.exists(path)
                ]
                if not leftovers:
                    continue
                record = load_json(self.root.record(kind, key))
                if record is None or now - (record.get(stamp) or now) < self.retention:
                    continue
                for path in leftovers:
                    try:
                        os.unlink(path)
                        removed += 1
                    except FileNotFoundError:
                        pass
        if removed:
            self.log("reclaimed", files=removed)

    def refresh_heartbeats(self, agents):
        live = 0
        for name in sorted(os.listdir(self.root.dir(PANES))):
            if not name.endswith(".json") or name.startswith("."):
                continue
            key = name[:-len(".json")]
            record = load_json(self.root.record(PANES, key))
            if record is None:
                continue
            agent = agents.get(record.get("agent_name"))
            if agent is None:
                self.ledger.release(key)
                self.beat(key, GONE, agent_name=record.get("agent_name"),
                          pane_id=record.get("pane_id"))
                continue
            live += 1
            self.beat(
                key, RUNNING,
                agent_name=record.get("agent_name"),
                pane_id=agent.get("pane_id"),
                agent_status=agent.get("agent_status"),
                interactive_ready=agent.get("interactive_ready"),
                occupant=(agent.get("agent_session") or {}).get("value"),
            )
        return live

    def ignore(self, name, reason):
        """Log an unusable drop-box entry once, not once per pass."""
        if name in self._ignored:
            return
        if len(self._ignored) < self.max_request_files:
            self._ignored.add(name)
            self.log("ignored", file=name, reason=reason)

    def directory_budget(self, dir_fd, names):
        """Hard backstop on drop-dir inodes and bytes, checked before any request is read."""
        if len(names) > self.max_request_files:
            return "too_many_request_files"
        total = 0
        for name in names:
            try:
                total += os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_size
            except OSError:
                continue
        if total > self.max_request_dir_bytes:
            return "request_dir_too_large"
        return None

    def outstanding_counts(self, agents):
        """Non-terminal work, counted from the host-only ledger.

        Deliberately NOT derived from `claims/` or `panes/`: those live in the registry
        root, which the requester can write, so a deletion there used to release a slot
        while its pane kept running. In-tree records are mirrors; this is the ledger.
        """
        return self.ledger.counts()

    def reconcile_ledger(self, agents):
        """Release reservations whose work is provably over, keep everything else.

        Run at startup, because a watcher that died mid-flight leaves reservations behind
        and the ledger has no other way to learn an agent went away. Releasing only on
        proof — a terminal rejection, or a published pane whose agent is gone — keeps the
        failure direction conservative: an extra held slot costs availability, a wrongly
        released one costs the quota it exists to enforce.
        """
        for entry in self.ledger.entries():
            key = entry.get("key")
            if not is_string(key) or not KEY_PATTERN.match(key):
                continue
            if os.path.exists(self.root.record(REJECTED, key)):
                self.ledger.release(key)
                continue
            pane = load_json(self.root.record(PANES, key))
            if pane is not None and pane.get("agent_name") not in agents:
                self.ledger.release(key)
                continue
            if entry.get("state") == RUNNING and pane is None \
                    and not os.path.exists(self.root.record(CLAIMS, key)):
                # Torn the other way: the reservation landed, the claim never did. The
                # claim precedes every spawn, so nothing was started and nothing is owed.
                self.ledger.release(key)
                self.log("reservation_released", key=key, reason="claim_never_published")

    def reconcile_claims(self, agents):
        """A claim with no reservation predates the current write order and cannot spawn.

        Reserving now would invent a slot for work that never started; leaving it parks a
        key nothing can ever settle, which is the state the docs promise never to produce.
        A terminal refusal is the loud, retryable answer.
        """
        for name in sorted(os.listdir(self.root.dir(CLAIMS))):
            if not name.endswith(".json") or name.startswith("."):
                continue
            key = name[:-len(".json")]
            if self.settled(key) or key in self._pending_close:
                continue
            if self.ledger.get(key) is not None:
                continue
            self.reject(key, "watcher_restart_incomplete", detail="no_reservation")

    def rate_limited(self, cap_id, now):
        """Per-capability admission rate. In-memory, so it resets with the watcher."""
        stamps = [stamp for stamp in self._rate.get(cap_id, ()) if now - stamp < self.rate_window]
        self._rate[cap_id] = stamps
        if len(stamps) >= self.rate_limit:
            return True
        stamps.append(now)
        return False

    def saturate(self, token, reason, **fields):
        """Report a limit once, on the transition into it, not on every pass it holds."""
        if token in self._saturated:
            return
        self._saturated.add(token)
        self.log("saturated", reason=reason, **fields)

    def relieve(self, token):
        if token in self._saturated:
            self._saturated.discard(token)
            self.log("drained", token=token)

    def selected_submission(self, key, names):
        """Which submission owns this key.

        A queue slot belongs to the bytes that acquired it. Recomputing a "winner" by name
        on every pass let a later retry that happens to sort earlier — pids are variable
        width, so `10000` sorts before `ffff` — inherit another submission's admission,
        carrying different name, cwd, argv, even a different capability, and re-charging the
        quota to whoever the replacement belonged to. So a queued key drains the exact
        submission its reservation names; only an unreserved key falls back to name order.
        """
        reservation = self.ledger.get(key) or {}
        if reservation.get("state") != QUEUED:
            return names[0]
        bound = reservation.get("request_path")
        if is_string(bound) and os.path.basename(bound) in names:
            return os.path.basename(bound)
        # The bound submission is gone, or predates this binding: the slot has nothing left
        # to drain, so release it rather than letting a sibling adopt it.
        self.ledger.release(key)
        self.log("reservation_released", key=key,
                 reason="bound_submission_missing" if is_string(bound) else "unbound_reservation")
        return names[0]

    def is_queued(self, key):
        """From the LEDGER, never the heartbeat.

        The heartbeat is a mirror, and the two are written in separate steps: a crash
        between them used to leave a reservation whose key looked fresh, so its own slot
        filled the cap and it never reached admission again — stuck with no timeout and no
        agent. The ledger is the single answer to "does this key hold a slot".
        """
        return (self.ledger.get(key) or {}).get("state") == QUEUED

    def reclaim_submissions(self, now):
        """Age out submissions nothing will ever read again.

        Every submission for a settled key is surplus — the claim records which file it
        read, and `chain` follows that pointer rather than the directory. For a key still
        waiting, the winner is live work and everything after it is a duplicate a confused
        retry produced. Both classes must be collectable or they accumulate forever: the
        drop-dir budget counts physical files, so a same-key fan-out could otherwise wedge
        the watcher out of reading any request at all, permanently.
        """
        directory = self.root.dir(REQUESTS)
        groups = {}
        for name in sorted(os.listdir(directory)):
            key = submission_key(name)
            if key is not None:
                groups.setdefault(key, []).append(name)
        removed = 0
        for key, names in groups.items():
            settled = self.settled(key) or os.path.exists(self.root.record(CLAIMS, key))
            keep = None if settled else self.selected_submission(key, names)
            for name in names:
                if name == keep:
                    continue
                path = os.path.join(directory, name)
                try:
                    if now - os.stat(path).st_mtime < self.retention:
                        continue
                    os.unlink(path)
                    removed += 1
                except OSError:
                    continue
        if removed:
            self.log("submissions_reclaimed", files=removed)
        return removed

    def reclaim_temps(self, now):
        """Sweep client temp files a crashed writer left behind.

        Request processing skips dot-files, but the drop-dir budget counts every entry, so
        a retry loop that dies between the write and the rename could fill the directory
        and wedge the watcher out of reading any real request.
        """
        removed = 0
        directory = self.root.dir(REQUESTS)
        for name in sorted(os.listdir(directory)):
            if not TEMP_PATTERN.match(name):
                continue
            path = os.path.join(directory, name)
            try:
                if now - os.stat(path).st_mtime < self.temp_retention:
                    continue
                os.unlink(path)
                removed += 1
            except OSError:
                continue
        return removed

    def process_requests(self, agents, live):
        dir_fd = open_dir(self.root.dir(REQUESTS))
        try:
            names = sorted(os.listdir(dir_fd))
            full = self.directory_budget(dir_fd, names)
            if full:
                # Deliberately writes nothing per key: a runaway requester that fills the
                # drop dir must not have its mistake amplified into host-side records.
                # Waiting clients see no decision and fail loudly on their own timeout.
                self.saturate(full, full, files=len(names))
                return
            self.relieve("too_many_request_files")
            self.relieve("request_dir_too_large")
            outstanding, per_capability = self.outstanding_counts(agents)

            grouped = {}
            for name in names:
                if TEMP_PATTERN.match(name):
                    continue
                key = submission_key(name)
                if key is None:
                    self.ignore(name, "bad_key")
                    continue
                if self.settled(key) or os.path.exists(self.root.record(CLAIMS, key)):
                    continue
                grouped.setdefault(key, []).append(name)
            for entry in self.ledger.entries():
                key = entry.get("key")
                # A queued slot with no submission left has nothing to drain and nothing
                # to time out; without this it would hold capacity until an operator
                # noticed, which is the same wedge in a different costume.
                if entry.get("state") == QUEUED and is_string(key) \
                        and key not in grouped and not self.settled(key):
                    self.ledger.release(key)
                    self.log("reservation_released", key=key, reason="submission_vanished")
            pending = []
            for key in sorted(grouped):
                # One key means one unit of work; the rest are inert duplicates.
                chosen = self.selected_submission(key, grouped[key])
                for other in grouped[key]:
                    if other != chosen:
                        self.ignore(other, "duplicate_submission")
                pending.append((chosen, key, self.is_queued(key)))
            # Already-queued work is settled first, unconditionally: it holds a slot the
            # caps count, so short-circuiting on a full cap before draining it would strand
            # that work forever — never timing out, never spawning when capacity frees.
            pending.sort(key=lambda entry: (not entry[2], entry[1]))

            for name, key, queued in pending:
                if not queued:
                    if outstanding >= self.max_outstanding:
                        self.saturate("too_many_outstanding", "too_many_outstanding",
                                      outstanding=outstanding)
                        continue
                    self.relieve("too_many_outstanding")
                accepted, capability = self.handle_request(
                    dir_fd, name, key, agents, live, per_capability, queued
                )
                if capability is not None and not queued:
                    outstanding += 1
                    per_capability[capability] = per_capability.get(capability, 0) + 1
                if accepted:
                    # A queued request that becomes a live pane still occupies its slot;
                    # releasing it here admitted one extra request per conversion.
                    live += 1
        finally:
            os.close(dir_fd)

    def handle_request(self, dir_fd, name, key, agents, live, per_capability, queued=False):
        """Returns (a pane was created, the capability now holding one more outstanding)."""
        now = self.clock()
        try:
            raw = read_bounded(dir_fd, name, MAX_REQUEST_BYTES)
            request = parse_request(raw, key)
            issuer = self.authorize(request, now)
            cwd = resolve_cwd(request.cwd, self.root.allow_roots)
            argv = check_argv(request.family, request.argv, self.profile_label)
            agent_name = agent_name_for(self.root.root_id, request.name)
            if agent_name in agents:
                raise RequestError("name_in_use")
            role = spawn_role(request.family, request.role, self.default_role)
        except RequestError as error:
            self.reject(key, error.reason)
            return False, None
        # Same reasoning as the global cap: a request already holding a slot is drained,
        # never re-gated on the cap its own slot is part of.
        if not queued and \
                per_capability.get(request.cap_id, 0) >= self.max_outstanding_per_capability:
            self.saturate(f"capability_outstanding:{request.cap_id}", "capability_outstanding",
                          capability=request.cap_id)
            return False, None
        self.relieve(f"capability_outstanding:{request.cap_id}")
        reservation = self.ledger.get(key) or {}
        if reservation.get("state") == QUEUED \
                and reservation.get("capability") not in (None, request.cap_id):
            # The slot was charged to somebody else's capability; it is not this payload's
            # to spend. Release it and let this submission be admitted on its own terms.
            self.ledger.release(key)
            self.log("reservation_released", key=key, reason="capability_changed")
            reservation = {}
            queued = False
        queued_since = (reservation.get("queued_since")
                        if reservation.get("state") == QUEUED else None)
        # The rate limit governs admissions, so a request already holding a queue slot is
        # not charged again on every pass it waits through.
        if queued_since is None and self.rate_limited(request.cap_id, now):
            self.reject(key, "rate_limited")
            return False, None
        limit = issuer.get("max_panes")
        limit = min(self.max_panes, limit) if is_int(limit) else self.max_panes
        decision, reason = admission(live, limit, queued_since, now, self.queue_timeout)
        if decision == REFUSE:
            self.reject(key, reason)
            return False, None
        if decision == QUEUE:
            self.ledger.reserve(key, request.cap_id, QUEUED, now=now,
                                queued_since=queued_since or now,
                                request_path=os.path.join(self.root.dir(REQUESTS), name))
            self.beat(key, QUEUED, queued_since=queued_since or now, capability=request.cap_id)
            return False, request.cap_id
        spawned = self.claim_and_spawn(
            key, request, issuer, cwd, argv, agent_name, byte_digest(raw),
            os.path.join(self.root.dir(REQUESTS), name), role,
        )
        return spawned, request.cap_id if spawned else None

    def authorize(self, request, now):
        """Host-side capability check. The request carries a proof bound to its own bytes,
        never a reusable secret, and the secret it is checked against is derived in memory
        rather than read from disk, so neither artifact can re-authorize anything."""
        record, reason = self.keyring.open(
            self.store, self.root.root_id, self.root.session, request.cap_id
        )
        if reason:
            raise RequestError(reason)
        reason = check_capability(
            record, request.cap_id, request.proof, request.material,
            self.root.root_id, self.root.session, SPAWN, now,
            self.keyring.secret(self.root.root_id, self.root.session, request.cap_id),
        )
        if reason:
            raise RequestError(reason)
        return record

    def claim_and_spawn(self, key, request, issuer, cwd, argv, agent_name, request_digest,
                        request_path, role):
        worker, worker_secret = self.keyring.mint(
            self.store, self.root.root_id, self.root.session, f"worker:{key}",
            grants=[], now=self.clock(),
        )
        claim = {
            "schema_version": SCHEMA_VERSION,
            "root_id": self.root.root_id,
            "session": self.root.session,
            "key": key,
            "request_digest": request_digest,
            "request_path": request_path,
            "claimed_at": self.clock(),
            "issuer_capability": request.cap_id,
            "issuer_subject": issuer.get("subject"),
            "worker_capability": worker["cap_id"],
            "family": request.family,
            "role": request.role,
            # What the pane was actually launched with, which the request's audit `role`
            # need not be: a placeholder is filled from the family default above.
            "spawn_role": role,
            "requested_name": request.name,
            "agent_name": agent_name,
            "cwd": cwd,
            "argv": list(argv),
        }
        # Ledger first, always: a crash after this and before the claim leaves a
        # reservation with nothing published and nothing spawned, which reconciliation
        # releases. The reverse order left a claim that could never spawn and never
        # released, which is the shape with no way out.
        self.ledger.reserve(key, request.cap_id, RUNNING, agent_name=agent_name)
        try:
            publish_once(self.root.record(CLAIMS, key), claim)
        except FileExistsError:
            return False
        results = self.root.dir(RESULTS, key)
        os.makedirs(results, mode=0o700, exist_ok=True)
        os.makedirs(self.root.dir(INBOX, key), mode=0o700, exist_ok=True)
        os.makedirs(self.root.dir(CONTROL, key), mode=0o700, exist_ok=True)
        env = {
            "HERDR_REGISTRY_ROOT": self.root.path,
            "HERDR_REGISTRY_KEY": key,
            "HERDR_REGISTRY_RESULTS": results,
            "HERDR_REGISTRY_CAPABILITY": f"{worker['cap_id']}:{worker_secret}",
        }
        try:
            spawned = self.host.spawn(request.family, agent_name, cwd, argv, env, role)
        except RequestError as error:
            self.reject(key, error.reason, request_digest=request_digest)
            return False
        pane = spawned.get("pane")
        process = self.host.process_info(pane) if pane else None
        agent = self.host.agents().get(agent_name) or {}
        attestation = self.attest(key, agent_name, pane, agent, process)
        if attestation is None:
            # The pane exists but the host could not observe who is in it. Publishing it
            # would report success for provenance nobody can check, so the pane goes away
            # and the request fails — a refusal is recoverable, a false success is not.
            return self.retire(key, pane, "provenance_unprovable", request_digest)
        self.publish_pane(key, claim, attestation, adopted=False)
        self.log("spawned", key=key, agent=agent_name, pane=pane)
        return True

    def retire(self, key, pane, reason, request_digest=None, **fields):
        """Close a pane this watcher created and could not accept, then settle the key.

        A close that FAILS must not settle anything: a rejection makes the key settled, so
        writing one over a still-running pane would strand it forever. The key stays
        reconcilable instead and the next pass retries.
        """
        ok, message = self.host.close_pane(pane) if pane else (True, None)
        if not ok:
            # Retried from memory, never from a persisted pane id: herdr recycles pane ids,
            # so a restarted watcher acting on a remembered one could kill a stranger. A
            # restart therefore leaves this to the human the orphan record is addressed to.
            self._pending_close[key] = (pane, reason, request_digest, fields)
            self.orphan(key, pane, f"{reason}:close_failed", close_error=message, **fields)
            return False
        self.reject(key, reason, request_digest=request_digest, pane_id=pane,
                    closed_pane=pane, **fields)
        return False

    def retry_closes(self):
        for key, (pane, reason, request_digest, fields) in list(self._pending_close.items()):
            ok, _ = self.host.close_pane(pane)
            if not ok:
                continue
            del self._pending_close[key]
            self._orphans.discard(key)
            self.reject(key, reason, request_digest=request_digest, pane_id=pane,
                        closed_pane=pane, **fields)

    def orphan(self, key, pane, reason, **fields):
        """Park a key that needs a human: no terminal record, so it stays reconcilable."""
        self.beat(key, ORPHANED, pane_id=pane, reason=reason, **fields)
        if key in self._orphans:
            return
        self._orphans.add(key)
        self.log("orphan_needs_attention", key=key, pane=pane, reason=reason, **fields)

    def attest(self, key, agent_name, pane_id, agent, process):
        """Host-observed worker identity, published BEFORE the pane record so that a crash
        between the two still leaves restart adoption something to verify against.

        Returns None when neither an occupant session nor a process group could be read:
        an attestation with no identity in it attests to nothing.
        """
        existing = load_json(self.root.record(ATTESTATIONS, key))
        if existing is not None:
            return existing
        agent = agent or {}
        record = {
            "schema_version": SCHEMA_VERSION,
            "key": key,
            "agent_name": agent_name,
            "pane_id": pane_id,
            "occupant": (agent.get("agent_session") or {}).get("value"),
            "process_group": (process or {}).get("foreground_process_group_id"),
            "pane_process": process,
            "attested_at": self.clock(),
        }
        if not pane_id or (record["occupant"] is None and record["process_group"] is None):
            return None
        try:
            publish_once(self.root.record(ATTESTATIONS, key), record)
        except FileExistsError:
            return load_json(self.root.record(ATTESTATIONS, key))
        return record

    def publish_pane(self, key, claim, attestation, adopted):
        record = {
            "schema_version": SCHEMA_VERSION,
            "root_id": self.root.root_id,
            "key": key,
            "agent_name": claim.get("agent_name"),
            "pane_id": attestation.get("pane_id"),
            "family": claim.get("family"),
            "role": claim.get("role"),
            "spawn_role": claim.get("spawn_role"),
            "cwd": claim.get("cwd"),
            "argv": claim.get("argv"),
            "spawned_at": self.clock(),
            "results_dir": self.root.dir(RESULTS, key),
            "inbox_dir": self.root.dir(INBOX, key),
            "provenance": {
                "request_digest": claim.get("request_digest"),
                "request_path": claim.get("request_path"),
                "claim_path": self.root.record(CLAIMS, key),
                "attestation_path": self.root.record(ATTESTATIONS, key),
                "issuer_capability": claim.get("issuer_capability"),
                "issuer_subject": claim.get("issuer_subject"),
                "worker_capability": claim.get("worker_capability"),
                "occupant": attestation.get("occupant"),
                "pane_process": attestation.get("pane_process"),
                "adopted": adopted,
            },
        }
        try:
            publish_once(self.root.record(PANES, key), record)
        except FileExistsError:
            return
        self.beat(key, RUNNING, agent_name=record["agent_name"], pane_id=record["pane_id"])

    # -- control bus

    def _pending(self, directory):
        """Sequenced messages with no result yet, in numeric order."""
        pending = []
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return pending
        for name in names:
            if not name.endswith(".json") or name.endswith(".result.json") or name.startswith("."):
                continue
            seq = name[:-len(".json")]
            if not SEQ_PATTERN.match(seq):
                continue
            if os.path.exists(os.path.join(directory, f"{seq}.result.json")):
                continue
            pending.append((int(seq), seq, name))
        return [entry[1:] for entry in sorted(pending)]

    def _result(self, directory, seq, document):
        try:
            publish_once(os.path.join(directory, f"{seq}.result.json"),
                         dict(document, at=self.clock()))
        except FileExistsError:
            pass

    def _live_pane(self, key, agents):
        record = load_json(self.root.record(PANES, key))
        if record is None:
            return None, None
        return record, agents.get(record.get("agent_name"))

    def _message_keys(self, kind):
        try:
            return sorted(
                name for name in os.listdir(self.root.dir(kind))
                if KEY_PATTERN.match(name) and os.path.isdir(self.root.dir(kind, name))
            )
        except FileNotFoundError:
            return []

    def process_inbox(self, agents):
        for key in self._message_keys(INBOX):
            record, agent = self._live_pane(key, agents)
            if record is None:
                continue
            directory = self.root.dir(INBOX, key)
            dir_fd = open_dir(directory)
            try:
                for seq, name in self._pending(directory):
                    if agent is None:
                        self._result(directory, seq, {"ok": False, "outcome": "agent_gone"})
                        continue
                    status = agent.get("agent_status")
                    if status == BLOCKED_STATUS:
                        self._result(directory, seq, {"ok": False, "outcome": "agent_blocked"})
                        continue
                    if status not in SETTLED_STATES:
                        # Prompting a working occupant stacks a turn `--wait` cannot tell
                        # apart from the first; leave it for a later pass.
                        break
                    try:
                        message = self.read_message(dir_fd, name)
                    except RequestError as error:
                        self._result(directory, seq,
                                     {"ok": False, "outcome": "rejected", "reason": error.reason})
                        continue
                    outcome, detail = self.host.prompt(
                        record["agent_name"], message["text"],
                        message.get("timeout_ms") or self.submit_timeout_ms,
                    )
                    self._result(directory, seq, {
                        "ok": outcome in ("settled", "submitted"),
                        "outcome": outcome,
                        "detail": detail,
                        "note": "delivery only; completion is an artifact under results/",
                    })
                    self.log("prompted", key=key, seq=seq, outcome=outcome)
                    break
            finally:
                os.close(dir_fd)

    def read_message(self, dir_fd, name):
        raw = read_bounded(dir_fd, name, MAX_MESSAGE_BYTES)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestError("unparsable")
        if not isinstance(document, dict):
            raise RequestError("not_an_object")
        text = document.get("text")
        if not is_string(text) or not text.strip():
            raise RequestError("bad_text")
        timeout = document.get("timeout_ms")
        if timeout is not None and (not is_int(timeout) or not 1000 <= timeout <= 600000):
            raise RequestError("bad_timeout")
        return {"text": text, "timeout_ms": timeout}

    def process_control(self, agents):
        for key in self._message_keys(CONTROL):
            record, agent = self._live_pane(key, agents)
            if record is None:
                continue
            directory = self.root.dir(CONTROL, key)
            dir_fd = open_dir(directory)
            try:
                for seq, name in self._pending(directory):
                    try:
                        action = self.read_action(dir_fd, name)
                    except RequestError as error:
                        self._result(directory, seq,
                                     {"ok": False, "outcome": "rejected", "reason": error.reason})
                        continue
                    if agent is None:
                        self._result(directory, seq, {"ok": True, "outcome": "already_gone"})
                        continue
                    if action == "cancel":
                        ok, detail = self.host.close_pane(record["pane_id"])
                        if ok:
                            self.beat(key, CANCELLED, agent_name=record["agent_name"],
                                      pane_id=record["pane_id"])
                    else:
                        ok, detail = self.host.send_keys(record["agent_name"], "ctrl-c")
                    self._result(directory, seq, {"ok": ok, "outcome": action, "detail": detail})
                    self.log("control", key=key, seq=seq, action=action, ok=ok)
            finally:
                os.close(dir_fd)

    def read_action(self, dir_fd, name):
        raw = read_bounded(dir_fd, name, MAX_MESSAGE_BYTES)
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RequestError("unparsable")
        if not isinstance(document, dict) or document.get("action") not in ("cancel", "interrupt"):
            raise RequestError("bad_action")
        return document["action"]


# --- provenance ------------------------------------------------------------------------


def chain(root, key, agents=None):
    """Request -> claim -> pane -> worker -> result, with every break named.

    Co-occurrence is not acceptance: the claim's digest is compared against the request
    bytes still on disk, so a request rewritten after it was claimed reads as a break.
    """
    breaks = []
    claim = load_json(root.record(CLAIMS, key))
    found = submissions(root.dir(REQUESTS), key)
    # The claim records which submission it actually read; only that one is the request.
    request_path = (claim or {}).get("request_path") or (found[0] if found else None)
    attestation = load_json(root.record(ATTESTATIONS, key))
    pane = load_json(root.record(PANES, key))
    rejected = load_json(root.record(REJECTED, key))
    beat = load_json(root.record(HEARTBEAT, key))
    request_digest = None
    try:
        with open(request_path, "rb") as handle:
            request_digest = byte_digest(handle.read(MAX_REQUEST_BYTES + 1))
    except (FileNotFoundError, TypeError):
        # Inputs are reclaimed once claimed or refused: the claim already commits the bytes'
        # digest and the rejection is its own trace, so absence here is expected, not a gap.
        if rejected is None and claim is None:
            breaks.append("request_missing")
    except OSError:
        breaks.append("request_unreadable")
    if claim is None:
        # A refusal terminates the chain before a claim exists; that is consistent, not broken.
        if rejected is None:
            breaks.append("claim_missing")
    elif request_digest and claim.get("request_digest") != request_digest:
        breaks.append("request_rewritten_after_claim")
    if pane is None and rejected is None:
        breaks.append("undecided")
    if pane is not None:
        if claim and pane.get("agent_name") != claim.get("agent_name"):
            breaks.append("agent_name_mismatch")
        provenance = pane.get("provenance") or {}
        if claim and provenance.get("request_digest") != claim.get("request_digest"):
            breaks.append("pane_digest_mismatch")
        # A pane record with no host-observed identity proves nothing beyond its own
        # existence, so it is undecided rather than intact.
        if attestation is None:
            breaks.append("attestation_missing")
        else:
            if attestation.get("pane_id") != pane.get("pane_id"):
                breaks.append("attestation_pane_mismatch")
            if attestation.get("occupant") is None and attestation.get("process_group") is None:
                breaks.append("worker_identity_unproven")
        if (provenance.get("pane_process") or {}).get("foreground_process_group_id") is None:
            breaks.append("pane_process_missing")
        if agents is not None:
            agent = agents.get(pane.get("agent_name"))
            if agent is None:
                breaks.append("agent_not_live")
            elif attestation is not None:
                occupant = (agent.get("agent_session") or {}).get("value")
                if attestation.get("occupant") is not None \
                        and occupant != attestation.get("occupant"):
                    breaks.append("live_occupant_mismatch")
    results = []
    directory = root.dir(RESULTS, key)
    if os.path.isdir(directory):
        results = sorted(os.listdir(directory))
    elif pane is not None:
        breaks.append("results_dir_missing")
    return {
        "key": key,
        "root_id": root.root_id,
        "session": root.session,
        "request_digest": request_digest,
        "claim": claim,
        "attestation": attestation,
        "pane": pane,
        "rejected": rejected,
        "heartbeat": beat,
        "results": results,
        "breaks": breaks,
        "intact": not breaks,
    }


# --- commands --------------------------------------------------------------------------


def command_init(args):
    manifest = init_root(args.root, args.root_id, args.allow_cwd)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def check_issue_fd(fd):
    """Validate the token descriptor BEFORE this process opens anything of its own.

    Checking later would be worse than useless: the first fd this process opens takes the
    lowest free number, so an unredirected default would already have become the watcher's
    own lock file and the token would be written into it — a bearer secret on disk, which
    is exactly what the keyless store exists to prevent.
    """
    if fd in (0, 1, 2):
        raise RegistryError(
            "--issue-fd may not be stdin, stdout or stderr; those carry the log stream"
        )
    try:
        os.fstat(fd)
    except OSError as error:
        raise RegistryError(
            f"--issue-fd {fd} is not open ({error}); redirect it, e.g. "
            f"`watch --issue SUBJECT {fd}>token`"
        )
    return fd


def emit_token(fd, token):
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
    except OSError as error:
        raise RegistryError(f"--issue-fd {fd} is not writable ({error})")


def load_keyring(source):
    """Resolve the in-memory master key. `generate` is fine for a fresh run; an operator
    who needs capabilities to survive a watcher restart must re-supply the same key."""
    if source == "generate":
        return Keyring.generate(), True
    if source == "env":
        text = os.environ.get(MASTER_KEY_ENV)
        if not text:
            raise RegistryError(f"{MASTER_KEY_ENV} is empty; cannot take the master key from env")
        return Keyring.from_hex(text), False
    if source == "stdin":
        return Keyring.from_hex(sys.stdin.readline()), False
    raise RegistryError(f"unknown master key source {source!r}")


def command_serve_capability(args):
    root = Root(args.root)
    keyring, _ = load_keyring(args.master_key)
    grants = [] if args.grants == "none" else [args.grants]
    record, secret = keyring.mint(
        args.store, root.root_id, root.session, args.subject, grants,
        ttl=args.ttl, max_panes=args.max_panes,
    )
    token = f"{record['cap_id']}:{secret}"
    print(json.dumps({
        "cap_id": record["cap_id"],
        "subject": record["subject"],
        "grants": record["grants"],
        "session": record["session"],
        "expires_at": record["expires_at"],
        "token": token,
        "env": {
            "HERDR_REGISTRY_ROOT": root.path,
            "HERDR_REGISTRY_CAPABILITY": token,
        },
    }, indent=2, sort_keys=True))
    return 0


def command_watch(args):
    if args.init_root_id and not os.path.exists(os.path.join(args.root, MANIFEST)):
        init_root(args.root, args.init_root_id, args.allow_cwd)
    root = Root(args.root)
    if args.issue:
        if args.issue_fd is None:
            # An inherited descriptor is not consent: a supervisor, shell integration or
            # socket activation can leave any fd open, and silently treating it as the
            # secret channel would deliver a bearer token to whatever happens to be there.
            raise RegistryError("--issue requires an explicit --issue-fd N (e.g. `3>token`)")
        check_issue_fd(args.issue_fd)
    lock_fd = os.open(os.path.join(root.path, WATCHER_LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise RegistryError(f"another watcher already owns {root.path}")
    host = Host(anchor=args.anchor)
    if not host.anchor:
        os.close(lock_fd)
        raise RegistryError("--anchor or HERDR_PANE_ID is required; the watcher splits from it")
    try:
        keyring, generated = load_keyring(args.master_key)
        if generated and not args.issue:
            # A generated key cannot be shared with a separate `serve-capability` process,
            # so without --issue this watcher can never authorize anything at all.
            raise RegistryError(
                "a generated master key can authorize nobody: pass --issue SUBJECT, or "
                f"share one key with `--master-key env|stdin` (${MASTER_KEY_ENV})"
            )
    except RegistryError:
        os.close(lock_fd)
        raise
    watcher = Watcher(
        root, host, keyring, store=args.store, max_panes=args.max_panes,
        queue_timeout=args.queue_timeout, submit_timeout_ms=args.submit_timeout_ms,
        profile_label=args.profile or os.environ.get("AGENT_PROFILE_LABEL"),
        default_role=args.default_role,
        retention=args.retention, temp_retention=args.temp_retention,
        max_outstanding=args.max_outstanding,
        max_outstanding_per_capability=args.max_outstanding_per_capability,
        max_request_files=args.max_request_files,
        max_request_dir_bytes=args.max_request_dir_bytes,
        rate_limit=args.rate_limit, rate_window=args.rate_window,
    )
    stopping = []
    for received in (signal.SIGINT, signal.SIGTERM):
        signal.signal(received, lambda *_: stopping.append(True))
    try:
        watcher.log("watching", root=root.path, root_id=root.root_id, max_panes=args.max_panes,
                    master_key="generated" if generated else args.master_key,
                    capabilities_survive_restart=not generated)
        startup_agents = watcher.host.agents()
        watcher.reconcile_ledger(startup_agents)
        watcher.reconcile_claims(startup_agents)
        for subject in args.issue:
            record, secret = keyring.mint(
                args.store, root.root_id, root.session, subject, [SPAWN], ttl=args.ttl
            )
            # The token is a bearer secret. It goes to the operator's own descriptor and
            # NEVER to the structured log, which is routinely redirected, captured by a
            # terminal, or shipped to a collector — all of which would put a signing secret
            # back on disk, which is the one thing this design exists to avoid.
            emit_token(args.issue_fd, f"{record['cap_id']}:{secret}")
            watcher.log("issued", subject=subject, cap_id=record["cap_id"],
                        expires_at=record["expires_at"], token_delivered_on_fd=args.issue_fd)
        while True:
            watcher.pass_once()
            if args.once or stopping:
                break
            time.sleep(args.interval)
        watcher.log("stopped", root=root.path)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    return 0


def command_request(args):
    """Client side, and deliberately undemanding: a successful publication writes a temp
    file and renames it, needing no other permission. It also reads (existence checks and a
    directory listing), and on failure attempts one best-effort unlink whose denial is
    swallowed. See the requester capability contract at the top of this module."""
    root_path = args.root or os.environ.get("HERDR_REGISTRY_ROOT")
    token = args.capability or os.environ.get("HERDR_REGISTRY_CAPABILITY")
    if not root_path:
        raise RegistryError("--root or HERDR_REGISTRY_ROOT is required")
    if not token or ":" not in token:
        raise RegistryError("--capability or HERDR_REGISTRY_CAPABILITY is required (cap_id:secret)")
    cap_id, _, secret = token.partition(":")
    key = args.key or f"{args.name}-{secrets.token_hex(4)}"
    if not KEY_PATTERN.match(key):
        raise RegistryError(f"idempotency key {key!r} is not a safe identity")
    if not SECRET_PATTERN.match(secret):
        raise RegistryError("capability token is malformed (expected cap_id:secret)")
    document = {
        "schema_version": SCHEMA_VERSION,
        # Wire field name, distinct from the host records' `key` on purpose: a request is
        # what a client asserts, and renaming its schema would break every existing client.
        "idempotency_key": key,
        "capability": {"cap_id": cap_id},
        "family": args.family,
        "name": args.name,
        "cwd": os.path.abspath(args.cwd),
        "argv": args.argv,
        # Role is audit metadata, never an authorization input, so an absent or blank one
        # is filled rather than refused. A root that reads `--role <r>` in a contract and
        # has nothing to put there should not have its request rejected for it.
        "role": (args.role or "").strip() or DEFAULT_ROLE,
        "created_at": time.time(),
    }
    # The secret signs the request and is then dropped: what lands on disk authorizes this
    # one request and nothing else, so a worker that reads it back gains no spawn rights.
    document["capability"]["proof"] = request_proof(secret, document)
    directory = os.path.join(root_path, REQUESTS)
    # An idempotency key means what it says: a re-run polling for its own result must not
    # add a second submission, nor resurrect an input the host reclaimed.
    decided = any(
        os.path.exists(os.path.join(root_path, kind, f"{key}.json"))
        for kind in (CLAIMS, REJECTED)
    )
    if not decided and not submissions(directory, key):
        # Publication uses write + rename and NOTHING else. That pair is the one primitive
        # the sandboxed root is known to have — the whole reason this bridge exists — so a
        # no-clobber scheme must not reach for link(), whose availability under the target
        # seatbelt is an assumption rather than a measured fact. Instead each submission
        # gets its own name, so no rename can ever land on a file another client wrote and
        # the host picks a winner among whatever it finds.
        final = os.path.join(
            directory, submission_name(key, os.getpid(), secrets.token_hex(8))
        )
        temp = os.path.join(directory, f".{key}.{os.getpid()}.tmp")
        try:
            _write_durably(temp, encode(document))
            os.rename(temp, final)
        except BaseException:
            # Only on the failure path, and never at the cost of the real error: a sandbox
            # may permit write and rename while denying unlink, and reporting EPERM here
            # would turn a tidy-up into the failure. The success path does not clean up at
            # all — rename consumed the temp name, and the host sweeps any stray.
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
    if not args.wait:
        submitted = submissions(directory, key)
        print(json.dumps({"key": key, "request": submitted[0] if submitted else None}))
        return 0
    deadline = time.time() + args.wait
    pane_path = os.path.join(root_path, PANES, f"{key}.json")
    reject_path = os.path.join(root_path, REJECTED, f"{key}.json")
    while time.time() < deadline:
        if os.path.exists(pane_path):
            # A pane FILE is not success. Success is a provenance chain that holds, so a
            # pane published without checkable identity reports failure to its requester.
            report = chain(Root(root_path), key)
            if not report["intact"]:
                print(json.dumps(dict(report, guidance=NO_FALLBACK_GUIDANCE),
                                 indent=2, sort_keys=True), file=sys.stderr)
                return 5
            print(json.dumps(load_json(pane_path), indent=2, sort_keys=True))
            return 0
        if os.path.exists(reject_path):
            record = load_json(reject_path)
            print(json.dumps(record, indent=2, sort_keys=True), file=sys.stderr)
            return 3
        time.sleep(0.25)
    print(json.dumps({
        "key": key, "outcome": "no_decision_within_wait",
        "guidance": NO_FALLBACK_GUIDANCE,
    }, indent=2, sort_keys=True), file=sys.stderr)
    return 4


def command_release(args):
    """Operator resolution of an orphan: release its host-side reservation explicitly.

    Deleting the in-tree claim does NOT do this, and never did safely — the claim is a
    mirror, and removing it while the worker may still be running is exactly the erasure
    the ledger exists to survive.
    """
    root = Root(args.root)
    keyring, _ = load_keyring(args.master_key)
    ledger = Ledger(args.store, root.root_id, root.session, keyring)
    agents = Host().agents() if args.check_live else {}
    entry = next((item for item in ledger.entries() if item.get("key") == args.key), None)
    if entry is None:
        raise RegistryError(f"no reservation for {args.key}")
    if args.check_live and entry.get("agent_name") in agents:
        raise RegistryError(
            f"{args.key} still has a live agent ({entry['agent_name']}); close its pane first "
            "or pass --no-check-live to release anyway"
        )
    ledger.release(args.key)
    print(json.dumps({"key": args.key, "released": True,
                      "capability": entry.get("capability")}, indent=2, sort_keys=True))
    return 0


def read_reservations(store, root_id, session):
    """Reservation states for display. Unauthenticated on purpose: `status` is a read, and
    requiring the master key to see that a key holds a slot would make the authoritative
    state the hardest thing to look at."""
    directory = os.path.join(capability_dir(store, root_id, session), RESERVATIONS)
    found = {}
    try:
        names = sorted(os.listdir(directory))
    except FileNotFoundError:
        return found
    for name in names:
        if not name.endswith(".json") or name.startswith("."):
            continue
        key = name[:-len(".json")]
        try:
            record = load_json(os.path.join(directory, name))
        except RegistryError:
            record = None
        found[key] = (record or {}).get("state") or Ledger.MALFORMED
    return found


def command_launch(args):
    """Spawn a codex (or other) ROOT with its registry environment attached.

    This is the wiring that makes the drop box load-bearing: without it a sandboxed root
    has a contract telling it to use the registry and no capability to do so. The token
    reaches the pane through `agent-teammate --env` and is deliberately NOT printed — the
    caller gets the capability id for the audit trail and nothing it could replay.
    """
    if os.environ.get("HERDR_ENV") != "1":
        raise RegistryError("launch requires a herdr session (HERDR_ENV=1)")
    root = Root(args.root)
    cwd = os.path.realpath(os.path.expanduser(args.cwd))
    if not os.path.isdir(cwd):
        raise RegistryError(f"--cwd {args.cwd} is not a directory")
    keyring, _ = load_keyring(args.master_key)
    record, secret = keyring.mint(
        args.store, root.root_id, root.session, f"root:{args.name}", [SPAWN],
        ttl=args.ttl, max_panes=args.max_panes,
    )
    host = Host(anchor=args.anchor)
    env = {
        "HERDR_REGISTRY_ROOT": root.path,
        "HERDR_REGISTRY_CAPABILITY": f"{record['cap_id']}:{secret}",
    }
    try:
        role = spawn_role(args.family, override=args.role)
        spawned = host.spawn(args.family, args.name, cwd, tuple(args.argv), env, role)
    except RequestError as error:
        raise RegistryError(f"launch failed: {error.reason}")
    print(json.dumps({
        "root_id": root.root_id,
        "session": root.session,
        "name": args.name,
        "family": args.family,
        "pane": spawned.get("pane"),
        "cap_id": record["cap_id"],
        "expires_at": record["expires_at"],
    }, indent=2, sort_keys=True))
    return 0


def command_status(args):
    root = Root(args.root)
    keys = set()
    for name in os.listdir(root.dir(REQUESTS)):
        key = submission_key(name)
        if key is not None:
            keys.add(key)
    for kind in (CLAIMS, PANES, REJECTED, HEARTBEAT):
        for name in os.listdir(root.dir(kind)):
            if name.endswith(".json") and not name.startswith("."):
                keys.add(name[:-len(".json")])
    reservations = read_reservations(args.store, root.root_id, root.session)
    keys |= set(reservations)
    rows = []
    for key in sorted(keys):
        beat = load_json(root.record(HEARTBEAT, key)) or {}
        pane = load_json(root.record(PANES, key))
        rejected = load_json(root.record(REJECTED, key))
        rows.append({
            "key": key,
            # The reservation is what the watcher acts on, so it leads. Reporting the
            # heartbeat alone showed a torn-write queued key as `pending` while it was
            # holding a slot — the authoritative state being the invisible one.
            "reservation": reservations.get(key),
            "state": reservations.get(key) or beat.get("state")
                     or ("claimed" if os.path.exists(root.record(CLAIMS, key)) else "pending"),
            "agent_name": (pane or {}).get("agent_name"),
            "pane_id": beat.get("pane_id") or (pane or {}).get("pane_id"),
            "agent_status": beat.get("agent_status"),
            "reason": (rejected or {}).get("reason"),
        })
    print(json.dumps({"root_id": root.root_id, "entries": rows}, indent=2, sort_keys=True))
    return 0


def command_chain(args):
    root = Root(args.root)
    agents = None
    if args.live:
        agents = Host().agents()
    report = chain(root, args.key, agents)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["intact"] else 1


def finite_seconds(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a finite positive number of seconds")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Host-side visible-spawn registry for sandboxed herdr orchestrator roots."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    initialize = subparsers.add_parser("init", help="create a run-scoped registry root")
    initialize.add_argument("--root", required=True)
    initialize.add_argument("--root-id", required=True)
    initialize.add_argument("--allow-cwd", action="append", default=[], required=True,
                            help="directory a request may name as a worker cwd; repeatable")
    initialize.set_defaults(handler=command_init)

    capability = subparsers.add_parser(
        "serve-capability", help="mint a host-held capability and print its one-time token"
    )
    capability.add_argument("--root", required=True)
    capability.add_argument("--subject", required=True)
    capability.add_argument("--grants", choices=("spawn", "none"), default="none",
                            help="`none` is the worker grant: it can present identity, not spawn")
    capability.add_argument("--ttl", type=finite_seconds, default=DEFAULT_TTL_SECONDS,
                            help="seconds until the capability expires; there is no unlimited")
    capability.add_argument("--max-panes", type=int)
    capability.add_argument("--store", default=DEFAULT_STORE)
    capability.add_argument("--master-key", choices=("env", "stdin"), default="env",
                            help="where to read the watcher's master key from; never written")
    capability.set_defaults(handler=command_serve_capability)

    watch = subparsers.add_parser("watch", help="run the host-side daemon (outside any sandbox)")
    watch.add_argument("--root", required=True)
    watch.add_argument("--anchor", help="pane to split from; defaults to HERDR_PANE_ID")
    watch.add_argument("--init-root-id", metavar="ID",
                       help="create the root with this id if it does not exist yet, so one "
                            "command can init, watch and issue")
    watch.add_argument("--allow-cwd", action="append", default=[],
                       help="allowed worker cwd for --init-root-id; repeatable")
    watch.add_argument("--store", default=DEFAULT_STORE)
    watch.add_argument("--master-key", choices=("generate", "env", "stdin"), default="env",
                       help="master key source; it is held in memory and never written, so "
                            "`generate` invalidates capabilities issued by a previous watcher "
                            "and requires --issue to be usable at all")
    watch.add_argument("--issue", action="append", default=[], metavar="SUBJECT",
                       help="mint a spawn capability for SUBJECT at startup and write its "
                            "token to --issue-fd; the only way a generated master key can "
                            "authorize anyone")
    watch.add_argument("--issue-fd", type=int, metavar="N",
                       help="descriptor --issue writes bearer tokens to; required with "
                            "--issue, and never the log stream")
    watch.add_argument("--ttl", type=finite_seconds, default=DEFAULT_TTL_SECONDS,
                       help="lifetime of capabilities minted by --issue")
    watch.add_argument("--max-panes", type=int, default=DEFAULT_MAX_PANES)
    watch.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    watch.add_argument("--queue-timeout", type=float, default=DEFAULT_QUEUE_TIMEOUT_SECONDS)
    watch.add_argument("--submit-timeout-ms", type=int, default=DEFAULT_SUBMIT_TIMEOUT_MS)
    watch.add_argument("--retention", type=float, default=DEFAULT_RETENTION_SECONDS,
                       help="seconds before a refused request's input is reclaimed")
    watch.add_argument("--temp-retention", type=float, default=DEFAULT_TEMP_RETENTION_SECONDS,
                       help="seconds before an abandoned client temp file is swept")
    watch.add_argument("--max-outstanding", type=int, default=DEFAULT_MAX_OUTSTANDING)
    watch.add_argument("--max-outstanding-per-capability", type=int,
                       default=DEFAULT_MAX_OUTSTANDING_PER_CAPABILITY)
    watch.add_argument("--max-request-files", type=int, default=DEFAULT_MAX_REQUEST_FILES)
    watch.add_argument("--max-request-dir-bytes", type=int,
                       default=DEFAULT_MAX_REQUEST_DIR_BYTES)
    watch.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT,
                       help="requests one capability may have admitted per --rate-window")
    watch.add_argument("--rate-window", type=float, default=DEFAULT_RATE_WINDOW_SECONDS)
    watch.add_argument("--profile", help="claude --profile to inject; defaults to AGENT_PROFILE_LABEL")
    watch.add_argument("--default-role", help="role a spawn carries when the request holds "
                       "only the audit placeholder `{}`; overrides the per-family default "
                       "for every family".format(DEFAULT_ROLE))
    watch.add_argument("--once", action="store_true", help="single pass, for tests and cron")
    watch.set_defaults(handler=command_watch)

    request = subparsers.add_parser("request", help="drop a spawn request (client side)")
    request.add_argument("family", choices=FAMILIES)
    request.add_argument("name")
    request.add_argument("--root")
    request.add_argument("--capability")
    request.add_argument("--key")
    request.add_argument("--cwd", default=os.getcwd())
    request.add_argument("--role", default=DEFAULT_ROLE,
                         help="audit metadata only; blank or omitted becomes `{}`".format(DEFAULT_ROLE))
    request.add_argument("--wait", type=float, metavar="SECONDS",
                         help="block for a pane or a refusal; refusal exits 3, no decision exits 4")
    request.set_defaults(handler=command_request)

    status = subparsers.add_parser("status", help="every key and its state")
    status.add_argument("--root", required=True)
    status.add_argument("--store", default=DEFAULT_STORE)
    status.set_defaults(handler=command_status)

    release = subparsers.add_parser(
        "release", help="operator: release a key's host-side outstanding reservation"
    )
    release.add_argument("--root", required=True)
    release.add_argument("--key", required=True)
    release.add_argument("--store", default=DEFAULT_STORE)
    release.add_argument("--master-key", choices=("env", "stdin"), default="env")
    release.add_argument("--no-check-live", dest="check_live", action="store_false",
                         help="release even while the reservation's agent is still live")
    release.set_defaults(handler=command_release, check_live=True)

    launch = subparsers.add_parser(
        "launch", help="spawn an orchestrator root pane wired to this registry"
    )
    launch.add_argument("family", choices=FAMILIES)
    launch.add_argument("name")
    launch.add_argument("--root", required=True)
    launch.add_argument("--cwd", default=os.getcwd())
    launch.add_argument("--anchor", help="pane to split from; defaults to HERDR_PANE_ID")
    launch.add_argument("--store", default=DEFAULT_STORE)
    launch.add_argument("--master-key", choices=("env", "stdin"), default="env")
    launch.add_argument("--ttl", type=finite_seconds, default=DEFAULT_TTL_SECONDS)
    launch.add_argument("--max-panes", type=int)
    launch.add_argument("--role", help="role the root pane is spawned with; overrides this "
                        "family's default")
    launch.set_defaults(handler=command_launch)

    walk = subparsers.add_parser("chain", help="host-attested provenance chain for one key")
    walk.add_argument("--root", required=True)
    walk.add_argument("--key", required=True)
    walk.add_argument("--live", action="store_true", help="also check the agent is still live")
    walk.set_defaults(handler=command_chain)

    args, extra = parser.parse_known_args(argv)
    if args.command == "launch":
        if extra[:1] == ["--"]:
            extra = extra[1:]
        args.argv = extra
    elif args.command == "request":
        if extra[:1] == ["--"]:
            extra = extra[1:]
        args.argv = extra
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        return args.handler(args)
    except (RegistryError, RequestError) as error:
        print(f"herdr-registry: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
