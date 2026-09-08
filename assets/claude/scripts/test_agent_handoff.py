#!/usr/bin/env python3

import builtins
import contextlib
import errno
import importlib.util
import io
import json
import os
import pathlib
import shlex
import socket
import sys
import tempfile
import threading
import time
import unittest


sys.dont_write_bytecode = True
SCRIPT_DIR = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolves deferred annotations through
    # sys.modules, which a bare module_from_spec is not in.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


handoff = load("agent_handoff", "agent-handoff.py")

RUN_ID = "run-729b"
WORKFLOW = "hunter-challenger"
INPUT_DIGEST = handoff.digest("brief text")

# Inside a Herdr session HERDR_WORKSPACE_ID is set, so mark_pending() falls back to the live
# workspace bus and the collect/dispatch tests leave real breadcrumbs behind; the Stop gate then
# blocks the chair's turn on seats that never existed (observed 2026-09-02). Every test in this
# module runs against a throwaway bus root instead. Classes that manage these variables
# themselves save and restore around this value, so they are unaffected.
_MODULE_ENV = ("HERDR_BUS_DIR", "HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH")
_module_bus = None
_saved_module_env = {}


def setUpModule():
    global _module_bus
    _module_bus = tempfile.TemporaryDirectory()
    for name in _MODULE_ENV:
        _saved_module_env[name] = os.environ.pop(name, None)
    os.environ["HERDR_BUS_DIR"] = _module_bus.name


def tearDownModule():
    for name in _MODULE_ENV:
        os.environ.pop(name, None)
        if _saved_module_env.get(name) is not None:
            os.environ[name] = _saved_module_env[name]
    _module_bus.cleanup()


def expectation(seat_id="hunter-types", round_id=1, input_digest=INPUT_DIGEST, attempts=None):
    return handoff.Expectation(RUN_ID, WORKFLOW, seat_id, round_id, input_digest,
                               None if attempts is None else frozenset(attempts))


def document(seat_id="hunter-types", round_id=1, attempt=1, outcome="ok",
             input_digest=INPUT_DIGEST, payload="findings"):
    return handoff.envelope(
        run_id=RUN_ID, workflow=WORKFLOW, seat_id=seat_id, round_id=round_id,
        attempt=attempt, outcome=outcome, input_digest=input_digest, payload=payload,
    )


class ScriptedAgent:
    """One fake seat occupant. `on_prompt` is where a worker publishes (or doesn't)."""

    def __init__(self, status="idle", occupant="w1:p1:session:a", kind="claude",
                 ready=True, on_prompt=None, on_wait=None, on_get=None, prompt_code=None,
                 missing=False, prompt_delay=0.0, wait_status=None, prompt_status=None):
        # `wait_status`/`prompt_status` let a wait or prompt reply report a status the
        # following `get` does not, which is how herdr's idle fallback presents.
        self.wait_status = wait_status
        self.prompt_status = prompt_status
        self.status = status
        self.occupant = occupant
        self.kind = kind
        self.ready = ready
        self.on_prompt = on_prompt
        self.on_wait = on_wait
        self.on_get = on_get
        self.prompt_code = prompt_code
        self.missing = missing
        self.prompt_delay = prompt_delay
        self.prompts = 0
        self.gets = 0
        self.waits = 0


class ScriptedTransport:
    """Fake Socket API. Records concurrency and refuses every terminal-text call."""

    def __init__(self, agents, process_groups=None):
        self.agents = agents
        self.cancelled = False
        self.inflight = 0
        self.max_inflight = 0
        # Pane -> foreground process group, consulted only to confirm a pgid->session
        # fingerprint upgrade. Absent means herdr could not read it.
        self.process_groups = process_groups or {}
        self.process_group_calls = []
        self._lock = threading.Lock()

    def process_group(self, pane_id):
        self.process_group_calls.append(pane_id)
        return self.process_groups.get(pane_id)

    def prompt(self, target, text, until, timeout_ms):
        agent = self.agents[target]
        agent.prompts += 1
        agent.last_prompt = text
        agent.last_prompt_until = tuple(until)
        agent.last_prompt_timeout_ms = timeout_ms
        with self._lock:
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if agent.prompt_delay:
                time.sleep(agent.prompt_delay)
            if agent.on_prompt:
                agent.on_prompt(agent, agent.prompts, text)
            if agent.prompt_code:
                return handoff.Reply(False, agent.prompt_code)
            if agent.prompt_status is not None:
                return handoff.Reply(True, None, agent.prompt_status, agent.occupant,
                                     agent.kind, agent.ready)
            return self.get(target)
        finally:
            with self._lock:
                self.inflight -= 1

    def wait(self, target, until, timeout_ms):
        agent = self.agents[target]
        agent.waits += 1
        if agent.on_wait:
            agent.on_wait(agent, agent.waits)
        if agent.wait_status is not None:
            return handoff.Reply(True, None, agent.wait_status, agent.occupant,
                                 agent.kind, agent.ready)
        return self.get(target)

    def get(self, target):
        agent = self.agents[target]
        agent.gets += 1
        if agent.on_get:
            agent.on_get(agent, agent.gets)
        if agent.missing:
            return handoff.Reply(False, "agent_not_found")
        return handoff.Reply(True, None, agent.status, agent.occupant, agent.kind,
                             agent.ready)

    def read(self, *args, **kwargs):
        raise AssertionError("the collector must never read terminal text")

    def cancel_all(self):
        self.cancelled = True


class FakeHerdrServer:
    """Minimal AF_UNIX server speaking the herdr request/response framing."""

    def __init__(self, responses=None, silent=False):
        # A value may be a single response or a list consumed one call at a time, which
        # is how a pane that changes between two requests is reproduced.
        self.responses = responses or {}
        self.requests = []
        self.silent = silent
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "herdr.sock")
        self.connected = threading.Event()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(self.path)
        self._server.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                client, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        with client:
            stream = client.makefile("rwb")
            line = stream.readline()
            self.connected.set()
            if self.silent or not line:
                while not self._stop:  # hold it open like a long agent.wait
                    time.sleep(0.02)
                return
            request = json.loads(line)
            self.requests.append(request["method"])
            payload = self.responses.get(request["method"], {})
            if isinstance(payload, list):
                payload = payload.pop(0) if len(payload) > 1 else payload[0]
            answer = {"id": request["id"]}
            answer.update(payload if "error" in payload else {"result": payload})
            stream.write((json.dumps(answer) + "\n").encode("utf-8"))
            stream.flush()

    def stop(self):
        self._stop = True
        self._server.close()
        self.directory.cleanup()


def agent_info(status="idle", pane_id="w1:p1", session=None, kind="agy",
               interactive_ready=True, state_change_seq=7, terminal_id="term_657acf6fd65a81"):
    """A herdr 0.7.5 `agent_info` result; screen-manifest kinds carry no agent_session."""
    agent = {
        "terminal_id": terminal_id,
        "agent": kind,
        "agent_status": status,
        "pane_id": pane_id,
        "interactive_ready": interactive_ready,
        "state_change_seq": state_change_seq,
        "launch_pending": False,
        "focused": False,
        "agent_session": None,
    }
    if session:
        agent["agent_session"] = {"source": f"herdr:{kind}", "agent": kind, "kind": "id",
                                  "value": session}
    return {"type": "agent_info", "agent": agent}


def process_info(pane_id="w1:p1", group=4242):
    return {
        "type": "pane_process_info",
        "process_info": {
            "pane_id": pane_id,
            "shell_pid": 100,
            "foreground_process_group_id": group,
            "foreground_processes": [{"pid": group + 1, "name": "agy"}],
        },
    }


class ValidationTests(unittest.TestCase):
    def test_rejects_malformed_documents(self):
        expect = expectation()
        self.assertEqual(handoff.validate("{not json", expect).reason, "unparsable")
        self.assertEqual(handoff.validate("[]", expect).reason, "not_an_object")
        partial = document()
        del partial["payload_digest"]
        self.assertEqual(
            handoff.validate(json.dumps(partial), expect).reason, "missing:payload_digest"
        )
        future = document()
        future["schema_version"] = handoff.SCHEMA_VERSION + 1
        self.assertEqual(
            handoff.validate(json.dumps(future), expect).reason,
            f"schema_version:{handoff.SCHEMA_VERSION + 1}",
        )
        typed = document()
        typed["attempt"] = "1"
        self.assertEqual(handoff.validate(json.dumps(typed), expect).reason, "type:attempt")
        unknown_outcome = document()
        unknown_outcome["outcome"] = "approve"
        self.assertEqual(
            handoff.validate(json.dumps(unknown_outcome), expect).reason, "outcome:approve"
        )

    def test_rejects_misattributed_and_stale_artifacts(self):
        expect = expectation()
        for candidate, reason in (
            (document(seat_id="hunter-perf"), "misattributed:seat_id"),
            (document(round_id=2), "misattributed:round"),
            (document(input_digest=handoff.digest("older brief")), "misattributed:input_digest"),
        ):
            self.assertEqual(handoff.validate(json.dumps(candidate), expect).reason, reason)
        foreign = document()
        foreign["run_id"] = "run-other"
        self.assertEqual(
            handoff.validate(json.dumps(foreign), expect).reason, "misattributed:run_id"
        )

    def test_rejects_an_attempt_that_was_never_dispatched(self):
        expect = expectation(attempts=[1, 2])
        self.assertTrue(handoff.validate(json.dumps(document(attempt=2)), expect).ok)
        self.assertEqual(
            handoff.validate(json.dumps(document(attempt=99)), expect).reason,
            "undispatched:99",
        )
        self.assertEqual(
            handoff.validate(json.dumps(document(attempt=1)), expectation(attempts=[])).reason,
            "undispatched:1",
        )

    def test_rejects_payload_digest_mismatch(self):
        tampered = document()
        tampered["payload"] = "findings, edited after publication"
        self.assertEqual(
            handoff.validate(json.dumps(tampered), expectation()).reason, "payload_digest"
        )

    def test_accepts_a_bound_artifact(self):
        checked = handoff.validate(json.dumps(document()), expectation())
        self.assertTrue(checked.ok)
        self.assertEqual(checked.document["payload"], "findings")


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = self.directory.name

    def temps(self):
        return [name for name in os.listdir(handoff.artifact_dir(self.run_dir))
                if name.endswith(".tmp")]

    def test_publishes_atomically_and_never_overwrites_an_attempt(self):
        path = handoff.publish(self.run_dir, document(payload="first"))
        self.assertEqual(os.path.basename(path), "hunter-types.r1.a1.json")
        with self.assertRaises(FileExistsError):
            handoff.publish(self.run_dir, document(payload="second"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["payload"], "first")
        retry = handoff.publish(self.run_dir, document(attempt=2, payload="second"))
        self.assertEqual(os.path.basename(retry), "hunter-types.r1.a2.json")
        self.assertEqual(sorted(os.listdir(handoff.artifact_dir(self.run_dir))),
                         ["hunter-types.r1.a1.json", "hunter-types.r1.a2.json"])

    def test_a_failed_write_leaves_no_temporary_file(self):
        real_fsync = os.fsync

        def exploding(fd):
            raise OSError(28, "No space left on device")

        os.fsync = exploding
        self.addCleanup(setattr, os, "fsync", real_fsync)
        with self.assertRaises(OSError):
            handoff.publish(self.run_dir, document())
        os.fsync = real_fsync
        self.assertEqual(os.listdir(handoff.artifact_dir(self.run_dir)), [])

    def test_concurrent_publications_do_not_share_a_temporary_path(self):
        errors = []
        barrier = threading.Barrier(6)

        def publish_attempt(attempt):
            barrier.wait()
            try:
                handoff.publish(self.run_dir, document(attempt=attempt))
            except Exception as error:  # collected, so a thread failure is visible
                errors.append(error)

        threads = [threading.Thread(target=publish_attempt, args=(attempt,))
                   for attempt in range(1, 7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(os.listdir(handoff.artifact_dir(self.run_dir))), 6)
        self.assertEqual(self.temps(), [])

    def test_concurrent_publication_of_one_attempt_has_a_single_winner(self):
        outcomes = []
        barrier = threading.Barrier(5)

        def publish_same():
            barrier.wait()
            try:
                handoff.publish(self.run_dir, document())
                outcomes.append("published")
            except FileExistsError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=publish_same) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("published"), 1)
        self.assertEqual(outcomes.count("rejected"), 4)
        self.assertEqual(self.temps(), [])

    def test_scan_takes_the_highest_valid_attempt_and_explains_rejections(self):
        handoff.publish(self.run_dir, document(attempt=1, payload="first"))
        handoff.publish(self.run_dir, document(attempt=2, payload="second"))
        handoff.publish(self.run_dir,
                        document(attempt=3, input_digest=handoff.digest("older brief")))
        partial = pathlib.Path(handoff.artifact_dir(self.run_dir)) / "hunter-types.r1.a4.json"
        partial.write_text('{"schema_version": 1, "run_i', encoding="utf-8")
        accepted, rejected = handoff.scan(self.run_dir, expectation(attempts=[1, 2, 3, 4]))
        self.assertEqual(accepted[1]["attempt"], 2)
        self.assertEqual(sorted(reason for _, reason in rejected),
                         ["misattributed:input_digest", "unparsable"])

    def test_scan_ignores_other_seats_and_missing_directories(self):
        handoff.publish(self.run_dir, document(seat_id="challenger-types"))
        accepted, rejected = handoff.scan(self.run_dir, expectation())
        self.assertIsNone(accepted)
        self.assertEqual(rejected, [])
        self.assertEqual(handoff.scan(os.path.join(self.run_dir, "absent"), expectation()),
                         (None, []))

    def test_rejects_unsafe_seat_ids(self):
        with self.assertRaises(ValueError):
            handoff.publish(self.run_dir, document(seat_id="../escape"))


def publisher(run_dir, seat_id, attempt=1, payload="findings", outcome="ok", **kwargs):
    def publish(agent, count, text):
        handoff.publish(run_dir, document(seat_id=seat_id, attempt=attempt, payload=payload,
                                          outcome=outcome, **kwargs))
    return publish


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = self.directory.name

    def collector(self, transport, deadline_seconds=5.0, grace_seconds=0.01, round_id=1):
        return handoff.Collector(self.run_dir, RUN_ID, WORKFLOW, round_id, transport,
                                 deadline_seconds=deadline_seconds,
                                 grace_seconds=grace_seconds)

    def seat(self, seat_id="hunter-types", target=None, max_attempts=1,
             allow_idle_retry=False):
        return handoff.Seat(seat_id, target or seat_id, "hunt for bugs", INPUT_DIGEST,
                            max_attempts=max_attempts,
                            allow_idle_retry=allow_idle_retry)

    def collect_one(self, agent, seat=None, **kwargs):
        seat = seat or self.seat()
        transport = ScriptedTransport({seat.target: agent})
        report = self.collector(transport, **kwargs).collect([seat])
        return report, report["seats"][0], transport

    def test_valid_artifact_completes_without_terminal_output(self):
        # The 23-column hard-wrap case: no marker is readable, lifecycle never leaves
        # idle, and `read` would raise — the artifact alone completes the seat.
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types"))
        report, seat, _ = self.collect_one(agent)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertEqual(seat["accepted_attempt"], 1)
        self.assertEqual(agent.prompts, 1)
        self.assertTrue(seat["artifact"].endswith("hunter-types.r1.a1.json"))

    def test_seats_are_dispatched_concurrently_under_one_deadline(self):
        agents = {name: ScriptedAgent(prompt_delay=0.2,
                                      on_prompt=publisher(self.run_dir, name))
                  for name in ("hunter-types", "hunter-perf", "challenger-types")}
        transport = ScriptedTransport(agents)
        seats = [self.seat(name) for name in agents]
        started = time.monotonic()
        report = self.collector(transport, deadline_seconds=3.0).collect(seats)
        elapsed = time.monotonic() - started
        self.assertTrue(report["complete"])
        self.assertEqual(transport.max_inflight, 3)
        self.assertLess(elapsed, 0.6)

    def test_dispatch_returns_after_uptake_without_collecting_the_turn(self):
        def begin(agent, _count, _text):
            agent.status = "working"

        agent = ScriptedAgent(on_prompt=begin)
        transport = ScriptedTransport({"hunter-types": agent})
        report = self.collector(transport, deadline_seconds=30.0).collect(
            [self.seat()], dispatch_only=True)
        seat = report["seats"][0]
        self.assertTrue(report["uptake_complete"])
        self.assertFalse(report["complete"])
        self.assertEqual(seat["state"], handoff.DISPATCHED)
        self.assertEqual(agent.last_prompt_until, handoff.UPTAKE_STATES)
        self.assertEqual(agent.last_prompt_timeout_ms, handoff.UPTAKE_TIMEOUT_MS)

    def test_dispatch_does_not_wait_for_or_prompt_an_already_busy_seat(self):
        agent = ScriptedAgent(status="working")
        transport = ScriptedTransport({"hunter-types": agent})
        report = self.collector(transport, deadline_seconds=30.0).collect(
            [self.seat()], dispatch_only=True)
        seat = report["seats"][0]
        self.assertFalse(report["uptake_complete"])
        self.assertEqual(seat["state"], handoff.BUSY)
        self.assertEqual(agent.waits, 0)
        self.assertEqual(agent.prompts, 0)

    def test_dispatch_resume_never_sends_the_checkpointed_attempt_twice(self):
        first = ScriptedAgent(on_prompt=lambda agent, _count, _text:
                              setattr(agent, "status", "working"))
        first_transport = ScriptedTransport({"hunter-types": first})
        first_report = self.collector(first_transport).collect(
            [self.seat()], dispatch_only=True)
        self.assertTrue(first_report["uptake_complete"])

        resumed = ScriptedAgent()
        resumed_transport = ScriptedTransport({"hunter-types": resumed})
        resumed_report = self.collector(resumed_transport).collect(
            [self.seat()], dispatch_only=True)
        self.assertTrue(resumed_report["uptake_complete"])
        self.assertTrue(resumed_report["seats"][0]["resumed"])
        self.assertEqual(resumed.prompts, 0)

    def test_dispatch_resume_never_upgrades_an_uncertain_attempt_to_delivered(self):
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                        INPUT_DIGEST)
        state = checkpoint.load()
        checkpoint.record_dispatch(state, 1, "w1:p1:session:a")
        agent = ScriptedAgent()
        transport = ScriptedTransport({"hunter-types": agent})
        report = self.collector(transport).collect([self.seat()], dispatch_only=True)
        self.assertFalse(report["uptake_complete"])
        self.assertEqual(report["seats"][0]["state"], handoff.UNKNOWN)
        self.assertEqual(agent.prompts, 0)

    def test_an_undispatched_attempt_never_completes_a_seat(self):
        # Finding 1: a stray artifact nobody dispatched must not be accepted.
        handoff.publish(self.run_dir, document(attempt=99))
        agent = ScriptedAgent()
        report, seat, _ = self.collect_one(agent, seat=self.seat(max_attempts=1))
        self.assertFalse(report["complete"])
        self.assertEqual(seat["state"], handoff.READY_WITHOUT_ARTIFACT)
        self.assertEqual(agent.prompts, 1)
        self.assertIn("undispatched:99", [entry["reason"] for entry in seat["rejected"]])

    def test_overlapping_collectors_dispatch_a_seat_exactly_once(self):
        # Final review 1: two collectors on one run dir both read an empty checkpoint,
        # both pick attempt 1, and both prompt the same pane.
        agent = ScriptedAgent(prompt_delay=0.2)
        transport = ScriptedTransport({"hunter-types": agent})
        reports = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            reports.append(self.collector(transport, deadline_seconds=2.0)
                           .collect([self.seat()]))

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(agent.prompts, 1)
        states = sorted(report["seats"][0]["state"] for report in reports)
        self.assertIn(handoff.OWNED_ELSEWHERE, states)

    def test_the_losing_collector_still_reports_a_published_artifact(self):
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                        INPUT_DIGEST)
        state = checkpoint.load()
        self.assertTrue(checkpoint.acquire())
        self.addCleanup(checkpoint.release)
        checkpoint.record_dispatch(state, 1, "w1:p1:session:a")
        handoff.publish(self.run_dir, document(attempt=1, payload="published anyway"))
        agent = ScriptedAgent()
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertFalse(seat["owned"])
        self.assertEqual(agent.prompts, 0)

    def test_duplicate_seats_and_targets_are_refused(self):
        collector = self.collector(ScriptedTransport({}))
        with self.assertRaises(ValueError):
            collector.collect([self.seat("hunter-types"), self.seat("hunter-types")])
        with self.assertRaises(ValueError):
            collector.collect([self.seat("hunter-types", target="pane-1"),
                               self.seat("hunter-perf", target="pane-1")])

    def test_replacing_the_accepted_artifact_content_fails_closed(self):
        # Final review 1: sticky by attempt number alone let the accepted path be
        # unlinked and refilled with different, internally valid bytes.
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types",
                                                  payload="original"))
        _, first, _ = self.collect_one(agent)
        os.unlink(first["artifact"])
        handoff.publish(self.run_dir, document(attempt=1, payload="swapped"))
        resumed_agent = ScriptedAgent()
        report, resumed, _ = self.collect_one(resumed_agent)
        self.assertFalse(report["complete"])
        self.assertEqual(resumed["state"], handoff.CHECKPOINT_CONFLICT)
        self.assertEqual(resumed["checkpoint_conflict"], "accepted_payload_changed")
        self.assertEqual(resumed_agent.prompts, 0)

    def test_a_corrupt_checkpoint_fails_closed_instead_of_redispatching(self):
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                        INPUT_DIGEST)
        os.makedirs(os.path.dirname(checkpoint.path), exist_ok=True)
        with open(checkpoint.path, "w", encoding="utf-8") as handle:
            handle.write('{"run_id": "run-729b", "workflow": "hunter-cha')
        agent = ScriptedAgent()
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.CHECKPOINT_CONFLICT)
        self.assertEqual(seat["checkpoint_conflict"], "unparsable")
        self.assertEqual(agent.prompts, 0)

    def write_checkpoint(self, dispatched, accepted, seat_id="hunter-types", round_id=1):
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, seat_id, round_id,
                                        INPUT_DIGEST)
        os.makedirs(os.path.dirname(checkpoint.path), exist_ok=True)
        state = dict(checkpoint.identity)
        state.update({"dispatched": dispatched, "accepted": accepted, "state": None})
        with open(checkpoint.path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        return checkpoint

    def test_an_accepted_attempt_that_was_never_dispatched_fails_closed(self):
        # Final2 finding 1: a structurally valid checkpoint claiming acceptance of an
        # attempt absent from `dispatched` would launder a stray artifact into done work.
        path = handoff.publish(self.run_dir, document(attempt=99))
        self.write_checkpoint([], {
            "attempt": 99, "path": path, "outcome": "ok",
            "payload_digest": handoff.digest("findings"), "accepted_at": 1.0,
        })
        agent = ScriptedAgent()
        report, seat, _ = self.collect_one(agent)
        self.assertFalse(report["complete"])
        self.assertEqual(seat["state"], handoff.CHECKPOINT_CONFLICT)
        self.assertEqual(seat["checkpoint_conflict"], "accepted_attempt_undispatched:99")
        self.assertEqual(agent.prompts, 0)
        verify = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                  "--workflow", WORKFLOW, "--seat", "hunter-types", "--round", "1",
                  "--input-digest", INPUT_DIGEST]
        self.assertEqual(handoff.main(verify), 2)

    def test_semantically_corrupt_dispatch_records_fail_closed(self):
        dispatch = {"attempt": 1, "occupant": "w1:p1:session:a", "dispatched_at": 1.0}
        accepted = {"attempt": 1, "path": "/nowhere.json", "outcome": "ok",
                    "payload_digest": handoff.digest("findings"), "accepted_at": 1.0}
        cases = [
            ([dispatch, dict(dispatch)], accepted, "dispatched_duplicate:1"),
            ([{"attempt": 0}], None, "dispatched_attempt"),
            ([{"attempt": True}], None, "dispatched_attempt"),
            ([dispatch], dict(accepted, payload_digest="nope"), "accepted_payload_digest"),
            ([dispatch], dict(accepted, outcome="approve"), "accepted_outcome"),
            ([dispatch], dict(accepted, path=""), "accepted_path"),
            ([dispatch], dict(accepted, attempt=0), "accepted_attempt"),
        ]
        for dispatched, accepted_entry, reason in cases:
            with self.subTest(reason=reason):
                self.setUp()
                self.write_checkpoint(dispatched, accepted_entry)
                agent = ScriptedAgent()
                _, seat, _ = self.collect_one(agent)
                self.assertEqual(seat["state"], handoff.CHECKPOINT_CONFLICT)
                self.assertEqual(seat["checkpoint_conflict"], reason)
                self.assertEqual(agent.prompts, 0)

    def test_a_checkpoint_for_another_dispatch_fails_closed(self):
        other = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                   handoff.digest("a different brief"))
        other.save(other.load())
        agent = ScriptedAgent()
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.CHECKPOINT_CONFLICT)
        self.assertEqual(seat["checkpoint_conflict"], "identity_mismatch:input_digest")
        self.assertEqual(agent.prompts, 0)

    def test_acceptance_is_stable_when_a_later_attempt_appears(self):
        # Finding 1: acceptance is idempotent across resumes.
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types",
                                                  payload="first"))
        _, first, _ = self.collect_one(agent)
        self.assertEqual(first["accepted_attempt"], 1)
        handoff.publish(self.run_dir, document(attempt=2, payload="late duplicate"))
        resumed_agent = ScriptedAgent()
        report, resumed, _ = self.collect_one(resumed_agent)
        self.assertEqual(resumed["accepted_attempt"], 1)
        self.assertEqual(resumed["payload_digest"], handoff.digest("first"))
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed_agent.prompts, 0)
        self.assertTrue(report["complete"])

    def test_resume_accepts_an_attempt_dispatched_before_the_chair_died(self):
        stalled = ScriptedAgent()
        self.collect_one(stalled, seat=self.seat(max_attempts=1))
        self.assertEqual(stalled.prompts, 1)
        handoff.publish(self.run_dir, document(attempt=1, payload="late but dispatched"))
        resumed_agent = ScriptedAgent()
        report, resumed, _ = self.collect_one(resumed_agent)
        self.assertTrue(report["complete"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(resumed["accepted_attempt"], 1)
        self.assertEqual(resumed_agent.prompts, 0)

    def test_a_working_agent_is_never_prompted(self):
        # Finding 2: `agent.prompt --wait` cannot tell turns apart, so a second prompt
        # would stack a duplicate turn onto an active one.
        agent = ScriptedAgent(status="working")
        report, seat, transport = self.collect_one(agent, deadline_seconds=0.3)
        self.assertEqual(agent.prompts, 0)
        self.assertFalse(report["complete"])
        self.assertEqual(seat["last_status"], "working")
        self.assertTrue(transport.cancelled)

    def test_an_active_turn_is_awaited_and_its_artifact_accepted(self):
        def finish_turn(agent, waits):
            agent.status = "idle"
            handoff.publish(self.run_dir, document(attempt=1, payload="from the first turn"))

        agent = ScriptedAgent(status="working", on_wait=finish_turn)
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                        INPUT_DIGEST)
        checkpoint.record_dispatch(checkpoint.load(), 1, "w1:p1:session:a")
        report, seat, _ = self.collect_one(agent)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["accepted_attempt"], 1)
        self.assertEqual(agent.prompts, 0)
        self.assertEqual(agent.waits, 1)

    def test_idle_never_retries_by_itself_on_any_kind(self):
        # An integration-backed kind is no different: idle is a screen-manifest fallback,
        # so a Codex turn that idles longer than the grace must not be prompted again.
        for kind in ("codex", "claude", "agy"):
            with self.subTest(kind=kind):
                self.setUp()
                agent = ScriptedAgent(kind=kind)
                _, seat, _ = self.collect_one(agent, seat=self.seat(max_attempts=3))
                self.assertEqual(agent.prompts, 1)
                self.assertEqual(seat["state"], handoff.READY_WITHOUT_ARTIFACT)
                self.assertEqual(seat["retry_declined"], "idle_is_not_turn_completion")

    def test_a_stalled_prompt_is_never_retried_automatically(self):
        # The trust-dialog case: the text never reached a turn, so a second task prompt
        # would queue behind the dialog. Recovery is inspect-first, not this gate.
        agent = ScriptedAgent(prompt_code="agent_prompt_stalled")
        _, seat, _ = self.collect_one(agent,
                                      seat=self.seat(max_attempts=3, allow_idle_retry=True))
        self.assertEqual(agent.prompts, 1)
        self.assertEqual(seat["state"], handoff.PROMPT_STALLED)
        self.assertEqual(seat["retry_declined"], "prompt_stalled_needs_inspection")

    def test_opted_in_retry_uses_a_new_attempt_path(self):
        dispatched = []

        def second_attempt_publishes(agent, count, text):
            dispatched.append(text)
            if count == 2:
                handoff.publish(self.run_dir, document(attempt=2))

        agent = ScriptedAgent(on_prompt=second_attempt_publishes)
        _, seat, _ = self.collect_one(
            agent, seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertEqual(seat["accepted_attempt"], 2)
        self.assertEqual(seat["attempts"], [1, 2])
        self.assertIn("--attempt 1 ", dispatched[0])
        self.assertIn("--attempt 2 ", dispatched[1])

    def test_retry_is_declined_when_the_confirmation_shows_a_turn_running(self):
        # The idle that licensed the retry was a lull: the turn resumes inside the grace
        # interval, so the confirmation must veto the second prompt.
        def resume_turn_during_grace(agent, count, text):
            threading.Timer(0.05, setattr, (agent, "status", "working")).start()

        agent = ScriptedAgent(on_prompt=resume_turn_during_grace)
        _, seat, _ = self.collect_one(agent, grace_seconds=0.25, deadline_seconds=1.0,
                                      seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(agent.prompts, 1)
        self.assertEqual(seat["retry_declined"], "not_settled:working")

    def test_retry_is_declined_when_the_pane_is_not_interactive_ready(self):
        agent = ScriptedAgent(ready=False)
        _, seat, _ = self.collect_one(agent,
                                      seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(agent.prompts, 1)
        self.assertEqual(seat["retry_declined"], "not_settled:idle")

    def test_retry_is_declined_without_an_identifiable_occupant(self):
        # Finding 3: no session and no process evidence means replacement is undetectable.
        agent = ScriptedAgent(kind="agy", occupant=None)
        _, seat, _ = self.collect_one(agent,
                                      seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(agent.prompts, 1)
        self.assertEqual(seat["retry_declined"], "unidentifiable_occupant")

    def test_replacement_between_pin_and_prompt_is_reported(self):
        def swap_occupant(agent, count, text):
            agent.occupant = "w1:p1:pgid999"

        agent = ScriptedAgent(occupant="w1:p1:pgid100", kind="agy", on_prompt=swap_occupant)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.REPLACED)
        self.assertEqual(seat["misdelivered_attempt"], 1)

    def test_a_wait_ending_on_a_question_is_not_lost_to_the_idle_fallback(self):
        """Round 7, collector half: the `agent.wait` reply was discarded and the loop
        re-read the status. If that read falls back to idle the seat looks promptable."""
        def fall_back_to_idle(agent, _count):
            agent.status = "idle"

        agent = ScriptedAgent(status="working", wait_status="blocked",
                              on_wait=fall_back_to_idle)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.BLOCKED)
        self.assertEqual(seat["last_status"], "blocked")
        self.assertEqual(agent.prompts, 0)

    def test_a_prompt_reply_ending_on_a_question_is_not_lost_either(self):
        """The collector read only the prompt reply's CODE, never its status, so a brief
        that drew a question left the seat looking merely artifact-less — and retry-
        enabled seats could then re-prompt into the dialog."""
        agent = ScriptedAgent(prompt_status="blocked")
        _, seat, _ = self.collect_one(
            agent, seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(seat["state"], handoff.BLOCKED)
        self.assertEqual(agent.prompts, 1, "the dialog must not be prompted again")

    def test_a_question_from_a_replacement_is_not_credited_to_our_seat(self):
        """Round 8 finding 1, prompt path: B takes the pane and asks something. Applying
        sticky `blocked` before validating identity reports our seat as merely waiting on
        an answer and silently drops the misdelivery."""
        def replace_and_ask(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(prompt_status="blocked", on_prompt=replace_and_ask)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.REPLACED)
        self.assertEqual(seat["misdelivered_attempt"], 1)

    def test_a_question_from_a_replacement_during_a_wait_is_not_ours_either(self):
        # Same direction, wait path: the sticky flag must survive only revalidation.
        def replace_and_ask(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(status="working", wait_status="blocked",
                              on_wait=replace_and_ask)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.REPLACED)
        self.assertEqual(agent.prompts, 0)

    def test_a_stalled_prompt_outranks_later_occupant_churn(self):
        """Round 8 finding 2, the other direction: herdr proved the text reached no turn,
        so churn afterwards cannot make it a misdelivery. `replaced` plus a
        `misdelivered_attempt` would name an attempt that never left."""
        def churn(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(prompt_code="agent_prompt_stalled", on_prompt=churn)
        _, seat, _ = self.collect_one(agent, seat=self.seat(max_attempts=2))
        self.assertEqual(seat["state"], handoff.PROMPT_STALLED)
        self.assertNotIn("misdelivered_attempt", seat)
        # The churn is not lost, only demoted to the identity axis.
        self.assertEqual(seat["occupant_verdict"], handoff.OCCUPANT_CHANGED)

    def test_a_stalled_prompt_outranks_an_unverifiable_occupant_too(self):
        def vanish(agent, _count, _text):
            agent.occupant = None

        agent = ScriptedAgent(prompt_code="agent_prompt_stalled", kind="agy",
                              on_prompt=vanish)
        _, seat, _ = self.collect_one(agent, seat=self.seat(max_attempts=2))
        self.assertEqual(seat["state"], handoff.PROMPT_STALLED)
        self.assertEqual(seat["occupant_verdict"], handoff.OCCUPANT_UNVERIFIABLE)

    def test_a_stalled_prompt_survives_every_later_observation(self):
        """Round 9 finding 1: round 8 guarded only the identity branch, so an unreachable
        seat, a question or an odd status on the next pass each overwrote the sticky
        delivery fact with `exited`/`blocked`/`unknown`."""
        def mutate(field, value):
            def hook(agent, _count, _text):
                setattr(agent, field, value)
            return hook

        cases = {
            "seat becomes unreachable": mutate("missing", True),
            "seat reports a question": mutate("status", "blocked"),
            "seat reports an odd status": mutate("status", "starting"),
            "occupant vanishes": mutate("occupant", None),
        }
        for index, (label, hook) in enumerate(cases.items()):
            with self.subTest(after=label):
                # A distinct seat per case: checkpoints are durable and share this run
                # dir, so reusing one id would carry the previous case's attempts in.
                agent = ScriptedAgent(prompt_code="agent_prompt_stalled", on_prompt=hook)
                _, seat, _ = self.collect_one(
                    agent, seat=self.seat(f"stalled-{index}", max_attempts=2))
                self.assertEqual(seat["state"], handoff.PROMPT_STALLED)
                self.assertEqual(seat["retry_declined"],
                                 "prompt_stalled_needs_inspection")

    def test_a_transport_error_never_earns_a_second_delivery(self):
        """Round 10: a failed prompt call does not say whether herdr submitted the text —
        a closed socket can drop the response to a prompt already delivered. The collector
        treated it as nothing-happened, so a retry-enabled seat prompted twice and ended
        `retry_exhausted`, risking the brief arriving in duplicate."""
        agent = ScriptedAgent(prompt_code="closed")
        # Short deadline on purpose: with no artifact forthcoming this seat spends the
        # rest of its budget polling for one (round 11), which is the intended behaviour.
        _, seat, _ = self.collect_one(
            agent, seat=self.seat(max_attempts=3, allow_idle_retry=True),
            deadline_seconds=0.3)
        self.assertEqual(seat["state"], handoff.UNKNOWN)
        self.assertEqual(seat["transport_error"], "closed")
        self.assertEqual(agent.prompts, 1, "an indeterminate delivery must not be repeated")

    def test_an_indeterminate_reply_outranks_occupant_churn(self):
        # Tier 1 with `stalled`: claiming a misdelivery of text that may never have been
        # sent would be the round-3 overclaim in a new costume.
        def churn(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(prompt_code="closed", on_prompt=churn)
        _, seat, _ = self.collect_one(agent, deadline_seconds=0.3)
        self.assertEqual(seat["state"], handoff.UNKNOWN)
        self.assertNotIn("misdelivered_attempt", seat)
        self.assertEqual(seat["occupant_verdict"], handoff.OCCUPANT_CHANGED)

    def test_a_transport_error_never_discards_an_artifact_already_on_disk(self):
        """Round 11: the artifact outranks transport state (HL-043/050). The worker
        published and the socket then dropped the response — reporting the seat unknown
        would throw away work that is finished and on disk."""
        agent = ScriptedAgent(prompt_code="closed",
                              on_prompt=publisher(self.run_dir, "hunter-types"))
        report, seat, _ = self.collect_one(agent, deadline_seconds=0.3)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["state"], handoff.COMPLETED)
        # The failed call is still recorded — the work completed, the call did not.
        self.assertEqual(seat["transport_error"], "closed")

    def test_an_artifact_published_after_the_failed_call_is_still_accepted(self):
        """The commoner shape: the response was dropped by a socket that had already
        delivered the prompt, so the turn is running and publishes shortly after."""
        def publish_late():
            time.sleep(0.1)
            handoff.publish(self.run_dir, document())

        threading.Thread(target=publish_late, daemon=True).start()
        agent = ScriptedAgent(prompt_code="closed")
        report, seat, _ = self.collect_one(agent, deadline_seconds=3.0)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertEqual(agent.prompts, 1, "waiting must never become a second delivery")

    def test_a_wait_timeout_is_not_a_transport_error(self):
        # The one reply code that looks like a failure and is not: herdr only answers
        # `timeout` after the turn started, so the collector must keep waiting it out.
        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE,
                              on_prompt=publisher(self.run_dir, "hunter-types"))
        report, seat, _ = self.collect_one(agent)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["state"], handoff.COMPLETED)

    def test_a_blocked_reply_under_unverifiable_identity_is_not_credited_yet(self):
        """Round 9 finding 2: the collector applied `blocked` when identity was merely
        unverifiable, where the ad-hoc path reports unknown. Crediting a question to a
        seat whose occupant cannot be confirmed is the step-2-before-step-3 error."""
        def ask_and_vanish(agent, _count, _text):
            agent.status = "blocked"
            agent.occupant = None

        agent = ScriptedAgent(prompt_status="blocked", kind="agy",
                              on_prompt=ask_and_vanish)
        _, seat, _ = self.collect_one(agent)
        self.assertNotEqual(seat["state"], handoff.BLOCKED)
        self.assertEqual(seat["occupant_verdict"], handoff.OCCUPANT_UNVERIFIABLE)

    def test_a_deferred_question_is_applied_once_identity_holds_again(self):
        """The evidence must survive the deferral, not be dropped by it: identity is
        unverifiable at the post-prompt read and readable again on the next pass."""
        def ask_and_vanish(agent, _count, _text):
            agent.status = "blocked"
            agent.occupant = None

        def restore(agent, count):
            if count > 2:
                agent.occupant = "w1:p1:session:a"
                agent.status = "idle"

        agent = ScriptedAgent(prompt_status="blocked", on_prompt=ask_and_vanish,
                              on_get=restore)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.BLOCKED)
        self.assertEqual(agent.prompts, 1, "the dialog must not be prompted again")

    def test_an_artifact_still_outranks_a_blocked_prompt_reply(self):
        # Blocked is not allowed to overturn the file's one authority: the artifact.
        agent = ScriptedAgent(prompt_status="blocked",
                              on_prompt=publisher(self.run_dir, "hunter-types"))
        report, seat, _ = self.collect_one(agent)
        self.assertTrue(report["complete"])
        self.assertEqual(seat["state"], handoff.COMPLETED)

    def test_blocked_artifact_publishes_the_seat_without_completing_it(self):
        # Finding 7: a durable "I am blocked" is a published, degraded seat.
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types",
                                                  outcome="blocked",
                                                  payload="need repo access"))
        report, seat, _ = self.collect_one(agent)
        self.assertFalse(report["complete"])
        self.assertTrue(report["published"])
        self.assertEqual(seat["state"], handoff.DECLARED_BLOCKED)
        self.assertEqual(seat["outcome"], "blocked")
        self.assertTrue(seat["published"])

    def test_failed_artifact_publishes_the_seat_without_completing_it(self):
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types",
                                                  outcome="failed", payload="crashed"))
        report, seat, _ = self.collect_one(agent)
        self.assertFalse(report["complete"])
        self.assertEqual(seat["state"], handoff.DECLARED_FAILED)

    def test_generated_command_survives_hostile_paths_without_injection(self):
        # Finding 6: the worker is told to run this line in a shell.
        hostile = os.path.join(self.run_dir, "run dir 'quoted' $(touch /tmp/owned); x")
        collector = handoff.Collector(hostile, RUN_ID, WORKFLOW, 1, None,
                                      deadline_seconds=1.0)
        seat = handoff.Seat("hunter-types", "t", "hunt", INPUT_DIGEST)
        text = collector._dispatch_text(seat, 1)
        command = [line for line in text.splitlines() if " publish " in line][0]
        argv = shlex.split(command)
        self.assertEqual(argv, [
            handoff.PUBLISH_COMMAND, "publish", "--run-dir", hostile, "--run-id", RUN_ID,
            "--workflow", WORKFLOW, "--seat", "hunter-types", "--round", "1",
            "--attempt", "1", "--input-digest", INPUT_DIGEST, "--outcome", "ok",
        ])

    def test_unsafe_identities_are_refused_before_dispatch(self):
        with self.assertRaises(ValueError):
            handoff.Collector(self.run_dir, "run; touch /tmp/owned", WORKFLOW, 1, None,
                              deadline_seconds=1.0)
        collector = self.collector(ScriptedTransport({}))
        with self.assertRaises(ValueError):
            collector.collect([handoff.Seat("hunter-types", "t", "p", "not-a-digest")])

    def test_resume_accepts_published_seats_without_redispatch(self):
        first = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types"))
        self.collect_one(first)
        agent = ScriptedAgent()
        pending = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-perf"))
        transport = ScriptedTransport({"hunter-types": agent, "hunter-perf": pending})
        report = self.collector(transport).collect(
            [self.seat("hunter-types"), self.seat("hunter-perf")]
        )
        resumed, dispatched = report["seats"]
        self.assertTrue(report["complete"])
        self.assertTrue(resumed["resumed"])
        self.assertEqual(agent.prompts, 0)
        self.assertFalse(dispatched["resumed"])
        self.assertEqual(pending.prompts, 1)

    def test_global_deadline_reports_state_and_reaps_owned_waits(self):
        agent = ScriptedAgent(status="working")
        started = time.monotonic()
        report, seat, transport = self.collect_one(agent, deadline_seconds=0.3)
        elapsed = time.monotonic() - started
        self.assertFalse(report["complete"])
        self.assertEqual(seat["last_status"], "working")
        self.assertTrue(transport.cancelled)
        self.assertEqual(transport.inflight, 0)
        self.assertLess(elapsed, 2.0)

    def test_late_publication_during_grace_is_accepted_without_a_retry(self):
        def late(agent, count, text):
            threading.Timer(0.05, handoff.publish,
                            (self.run_dir, document())).start()

        agent = ScriptedAgent(on_prompt=late)
        _, seat, _ = self.collect_one(agent, grace_seconds=0.5,
                                      seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertEqual(agent.prompts, 1)

    def test_retry_exhaustion_is_explicit(self):
        agent = ScriptedAgent()
        _, seat, _ = self.collect_one(
            agent, seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(seat["state"], handoff.RETRY_EXHAUSTED)
        self.assertEqual(agent.prompts, 2)

    def test_a_late_original_publication_beats_its_retry(self):
        def publish_first_attempt_late(agent, count, text):
            if count == 2:
                handoff.publish(self.run_dir, document(attempt=1, payload="original"))

        agent = ScriptedAgent(on_prompt=publish_first_attempt_late)
        _, seat, _ = self.collect_one(
            agent, seat=self.seat(max_attempts=2, allow_idle_retry=True))
        self.assertEqual(seat["state"], handoff.COMPLETED)
        self.assertEqual(seat["accepted_attempt"], 1)

    def test_unknown_status_is_not_retried(self):
        agent = ScriptedAgent(status="unknown")
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.UNKNOWN)
        self.assertEqual(agent.prompts, 0)

    def test_blocked_occupant_is_reported_and_not_prompted(self):
        agent = ScriptedAgent(status="blocked")
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.BLOCKED)
        self.assertEqual(agent.prompts, 0)

    def test_exited_occupant_is_reported(self):
        agent = ScriptedAgent(missing=True)
        _, seat, _ = self.collect_one(agent)
        self.assertEqual(seat["state"], handoff.EXITED)
        self.assertEqual(agent.prompts, 0)

    def test_a_transport_failure_isolates_to_its_own_seat(self):
        class Exploding(ScriptedTransport):
            def get(self, target):
                if target == "hunter-perf":
                    raise RuntimeError("socket exploded")
                return super().get(target)

        transport = Exploding({
            "hunter-types": ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types")),
            "hunter-perf": ScriptedAgent(),
        })
        report = self.collector(transport).collect(
            [self.seat("hunter-types"), self.seat("hunter-perf")]
        )
        healthy, broken = report["seats"]
        self.assertFalse(report["complete"])
        self.assertEqual(healthy["state"], handoff.COMPLETED)
        self.assertIn("socket exploded", broken["error"])

    def test_stale_round_artifact_does_not_complete_a_new_round(self):
        handoff.publish(self.run_dir, document(round_id=1))
        agent = ScriptedAgent()
        transport = ScriptedTransport({"hunter-types": agent})
        report = self.collector(transport, round_id=2).collect([self.seat(max_attempts=1)])
        self.assertEqual(report["seats"][0]["state"], handoff.READY_WITHOUT_ARTIFACT)
        self.assertEqual(agent.prompts, 1)


class SocketTransportTests(unittest.TestCase):
    def test_a_pane_that_changes_between_the_two_reads_yields_no_identity(self):
        # Final review 5: agent A answers agent.get, exits, and B's process group answers
        # pane.process_info — pairing them would hand A's status B's identity, and the
        # post-prompt comparison could never notice.
        server = FakeHerdrServer({
            "agent.get": [
                agent_info(kind="agy", session=None, state_change_seq=7),
                agent_info(kind="agy", session=None, state_change_seq=9,
                           terminal_id="term_replacement"),
            ],
            "pane.process_info": process_info(group=5150),
        })
        self.addCleanup(server.stop)
        reply = handoff.SocketTransport(server.path).get("agy-seat")
        self.assertTrue(reply.ok)
        self.assertIsNone(reply.occupant)
        self.assertEqual(server.requests,
                         ["agent.get", "pane.process_info", "agent.get"])

    def test_a_stable_pane_keeps_its_process_group_identity(self):
        server = FakeHerdrServer({
            "agent.get": [agent_info(kind="agy", session=None, state_change_seq=7)],
            "pane.process_info": process_info(group=4242),
        })
        self.addCleanup(server.stop)
        self.assertEqual(handoff.SocketTransport(server.path).get("agy-seat").occupant,
                         "w1:p1:pgid4242")

    def test_sessionless_occupant_comes_from_the_foreground_process_group(self):
        # Finding 3: with no agent_session, terminal_id would compare equal across a
        # replacement; the process group does not.
        server = FakeHerdrServer({
            "agent.get": agent_info(kind="agy", session=None),
            "pane.process_info": process_info(group=4242),
        })
        self.addCleanup(server.stop)
        transport = handoff.SocketTransport(server.path)
        first = transport.get("agy-seat")
        self.assertEqual(first.occupant, "w1:p1:pgid4242")
        self.assertEqual(first.kind, "agy")
        server.responses["pane.process_info"] = process_info(group=5150)
        self.assertEqual(transport.get("agy-seat").occupant, "w1:p1:pgid5150")

    def test_session_backed_occupant_is_used_directly(self):
        server = FakeHerdrServer({"agent.get": agent_info(kind="claude", session="sid-1")})
        self.addCleanup(server.stop)
        self.assertEqual(handoff.SocketTransport(server.path).get("claude-seat").occupant,
                         "w1:p1:session:sid-1")

    def test_missing_process_evidence_leaves_the_occupant_unidentified(self):
        server = FakeHerdrServer({
            "agent.get": agent_info(kind="agy", session=None),
            "pane.process_info": {"error": {"code": "pane_not_found", "message": "gone"}},
        })
        self.addCleanup(server.stop)
        self.assertIsNone(handoff.SocketTransport(server.path).get("agy-seat").occupant)

    def test_cancel_all_unblocks_an_in_flight_wait(self):
        server = FakeHerdrServer(silent=True)
        self.addCleanup(server.stop)
        transport = handoff.SocketTransport(server.path)
        replies = []
        waiter = threading.Thread(
            target=lambda: replies.append(transport.wait("seat", ("idle",), 60000))
        )
        waiter.start()
        self.assertTrue(server.connected.wait(2.0))
        started = time.monotonic()
        transport.cancel_all()
        waiter.join(3.0)
        self.assertFalse(waiter.is_alive())
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertEqual(replies[0].code, "cancelled")

    def test_sockets_are_created_and_registered_under_one_lock(self):
        # Finding 5: a cancel_all landing between creation and registration would leave
        # a socket nobody can shut down, holding collection past the global deadline.
        server = FakeHerdrServer({"agent.get": agent_info(session="sid-1")})
        self.addCleanup(server.stop)
        transport = handoff.SocketTransport(server.path)
        observed = []
        real = handoff.socket

        class Watched:
            """Rebinding only the module under test leaves the fake server alone."""

            AF_UNIX = real.AF_UNIX
            SOCK_STREAM = real.SOCK_STREAM
            SHUT_RDWR = real.SHUT_RDWR
            SOL_SOCKET = real.SOL_SOCKET
            SO_ERROR = real.SO_ERROR

            @staticmethod
            def socket(*args, **kwargs):
                # acquire() fails while the registration lock is already held.
                observed.append(transport._lock.acquire(blocking=False))
                return real.socket(*args, **kwargs)

        handoff.socket = Watched
        self.addCleanup(setattr, handoff, "socket", real)
        transport.get("seat")
        self.assertEqual(observed, [False])

    def test_a_connect_still_in_progress_obeys_the_global_deadline(self):
        # Final review 6: a connect that never completes (saturated accept queue) cannot
        # be interrupted by shutdown, so it must be bounded by the deadline itself.
        server = FakeHerdrServer(silent=True)
        self.addCleanup(server.stop)
        real = handoff.socket

        class NeverConnects(real.socket):
            def connect(self, address):
                raise BlockingIOError(errno.EINPROGRESS, "in progress")

        class Stuck:
            AF_UNIX = real.AF_UNIX
            SOCK_STREAM = real.SOCK_STREAM
            SHUT_RDWR = real.SHUT_RDWR
            SOL_SOCKET = real.SOL_SOCKET
            SO_ERROR = real.SO_ERROR
            socket = NeverConnects

        handoff.socket = Stuck
        self.addCleanup(setattr, handoff, "socket", real)
        collector = handoff.Collector(
            tempfile.mkdtemp(), RUN_ID, WORKFLOW, 1,
            handoff.SocketTransport(server.path), deadline_seconds=0.05,
        )
        collector.transport.budget = collector.remaining
        seat = handoff.Seat("hunter-types", "hunter-types", "hunt", INPUT_DIGEST)
        started = time.monotonic()
        report = collector.collect([seat])
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.35)
        self.assertFalse(report["complete"])

    def test_cancellation_during_connect_does_not_block_on_a_read(self):
        # shutdown() cannot interrupt a socket that was still connecting, so the call must
        # notice the cancellation itself instead of waiting out its socket timeout.
        server = FakeHerdrServer(silent=True)
        self.addCleanup(server.stop)
        transport = handoff.SocketTransport(server.path)
        real = handoff.socket

        class RacingSocket(real.socket):
            def connect(self, address):
                super().connect(address)
                transport.cancel_all()

        class Racing:
            AF_UNIX = real.AF_UNIX
            SOCK_STREAM = real.SOCK_STREAM
            SHUT_RDWR = real.SHUT_RDWR
            SOL_SOCKET = real.SOL_SOCKET
            SO_ERROR = real.SO_ERROR
            socket = RacingSocket

        handoff.socket = Racing
        self.addCleanup(setattr, handoff, "socket", real)
        started = time.monotonic()
        reply = transport.wait("seat", ("idle",), 60000)
        self.assertEqual(reply.code, "cancelled")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_cancelled_transport_refuses_further_calls(self):
        server = FakeHerdrServer({"agent.get": agent_info(session="sid-1")})
        self.addCleanup(server.stop)
        transport = handoff.SocketTransport(server.path)
        self.assertTrue(transport.get("seat").ok)
        transport.cancel_all()
        self.assertEqual(transport.get("seat").code, "cancelled")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = self.directory.name

    def publish_argv(self, seat, payload_file, attempt=1, outcome="ok"):
        return ["publish", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                "--workflow", WORKFLOW, "--seat", seat, "--round", "1",
                "--attempt", str(attempt), "--input-digest", INPUT_DIGEST,
                "--outcome", outcome, "--payload-file", payload_file]

    def test_dispatch_command_returns_after_uptake(self):
        spec_path = os.path.join(self.run_dir, "run.json")
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump({
                "run_dir": self.run_dir,
                "run_id": RUN_ID,
                "workflow": WORKFLOW,
                "round": 1,
                "deadline_seconds": 1800,
                "seats": [{
                    "seat_id": "hunter-types",
                    "target": "hunter-types",
                    "prompt": "hunt",
                    "input_digest": INPUT_DIGEST,
                }],
            }, handle)
        agent = ScriptedAgent(on_prompt=lambda worker, _count, _text:
                              setattr(worker, "status", "working"))
        transport = ScriptedTransport({"hunter-types": agent})
        real_transport = handoff.SocketTransport
        handoff.SocketTransport = lambda budget=None: transport
        self.addCleanup(setattr, handoff, "SocketTransport", real_transport)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = handoff.main(["dispatch", "--spec", spec_path])
        report = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["mode"], "dispatch")
        self.assertTrue(report["uptake_complete"])
        self.assertEqual(report["deadline_seconds"], 15.0)

    def test_publish_verify_round_trip(self):
        payload = os.path.join(self.run_dir, "verdicts.md")
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write("VERDICT: CHANGES_NEEDED\n")
        self.assertEqual(handoff.main(self.publish_argv("challenger-types", payload)), 0)
        verify = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                  "--workflow", WORKFLOW, "--seat", "challenger-types", "--round", "1",
                  "--input-digest", INPUT_DIGEST]
        self.assertEqual(handoff.main(verify + ["--attempt", "1"]), 0)
        self.assertEqual(handoff.main(verify + ["--attempt", "2"]), 1)
        wrong = verify + ["--attempt", "1"]
        wrong[wrong.index(INPUT_DIGEST)] = handoff.digest("a different brief")
        self.assertEqual(handoff.main(wrong), 1)

    def test_verify_refuses_to_accept_an_attempt_nobody_dispatched(self):
        # Final review 3: `verify` without --attempt used to accept any valid artifact.
        handoff.publish(self.run_dir, document(attempt=99))
        verify = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                  "--workflow", WORKFLOW, "--seat", "hunter-types", "--round", "1",
                  "--input-digest", INPUT_DIGEST]
        self.assertEqual(handoff.main(verify), 2)
        self.assertEqual(handoff.main(verify + ["--attempt", "99"]), 0)

    def test_verify_derives_attempts_from_the_checkpoint(self):
        checkpoint = handoff.Checkpoint(self.run_dir, RUN_ID, WORKFLOW, "hunter-types", 1,
                                        INPUT_DIGEST)
        checkpoint.record_dispatch(checkpoint.load(), 1, "w1:p1:session:a")
        handoff.publish(self.run_dir, document(attempt=1))
        handoff.publish(self.run_dir, document(attempt=2, payload="undispatched"))
        verify = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                  "--workflow", WORKFLOW, "--seat", "hunter-types", "--round", "1",
                  "--input-digest", INPUT_DIGEST]
        self.assertEqual(handoff.main(verify), 0)
        accepted, _ = handoff.scan(self.run_dir, expectation(attempts=[1]))
        self.assertEqual(accepted[1]["attempt"], 1)

    def test_diagnose_lists_candidates_but_never_reports_acceptance(self):
        handoff.publish(self.run_dir, document(attempt=99))
        argv = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                "--workflow", WORKFLOW, "--seat", "hunter-types", "--round", "1",
                "--input-digest", INPUT_DIGEST, "--diagnose"]
        self.assertEqual(handoff.main(argv), 1)

    def verify_argv(self, seat, attempt, round_id=1, payload_out=None):
        argv = ["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                "--workflow", WORKFLOW, "--seat", seat, "--round", str(round_id),
                "--input-digest", INPUT_DIGEST, "--attempt", str(attempt)]
        return argv + (["--payload-out", payload_out] if payload_out else [])

    def materialized_paths(self, base):
        stem = os.path.basename(base).split(".")[0]
        return sorted(name for name in os.listdir(self.run_dir)
                      if name.startswith(stem) and name != os.path.basename(base))

    def test_each_round_materializes_its_own_payload_path(self):
        # Final2 finding 2: round 2 must not collide with round 1's immutable copy.
        base = os.path.join(self.run_dir, "hunter-types.accepted.md")
        digests = []
        for round_id, text in ((1, "round one findings\n"), (2, "round two findings\n")):
            source = os.path.join(self.run_dir, f"findings-r{round_id}.md")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write(text)
            argv = self.publish_argv("hunter-types", source)
            argv[argv.index("--round") + 1] = str(round_id)
            self.assertEqual(handoff.main(argv), 0)
            self.assertEqual(
                handoff.main(self.verify_argv("hunter-types", 1, round_id, base)), 0)
            digests.append(handoff.digest(text))
        written = self.materialized_paths(base)
        self.assertEqual(len(written), 2, written)
        self.assertTrue(any(digests[0][:12] in name and ".r1.a1." in name for name in written))
        self.assertTrue(any(digests[1][:12] in name and ".r2.a1." in name for name in written))
        for name, expected in zip(written, ("round one findings\n", "round two findings\n")):
            with open(os.path.join(self.run_dir, name), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), expected)

    def materialize_findings(self, text="finding A\n"):
        source = os.path.join(self.run_dir, "findings.md")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write(text)
        self.assertEqual(handoff.main(self.publish_argv("hunter-types", source)), 0)
        base = os.path.join(self.run_dir, "hunter-types.accepted.md")
        self.assertEqual(handoff.main(self.verify_argv("hunter-types", 1, 1, base)), 0)
        return os.path.join(self.run_dir, self.materialized_paths(base)[0])

    def consume(self, path, expected):
        """Run the consumer, capturing the bytes it emits and every open it performs."""
        opens = []
        real_open = builtins.open

        def counting_open(target, *args, **kwargs):
            if target == path:
                opens.append(target)
            return real_open(target, *args, **kwargs)

        class Captured:
            def __init__(self):
                self.buffer = io.BytesIO()

            def write(self, text):
                self.buffer.write(text.encode("utf-8"))

            def flush(self):
                pass

        captured = Captured()
        builtins.open = counting_open
        stdout, sys.stdout = sys.stdout, captured
        try:
            code = handoff.main(["consume", "--file", path, "--digest", expected])
        finally:
            sys.stdout = stdout
            builtins.open = real_open
        return code, captured.buffer.getvalue(), opens

    def test_consumed_bytes_survive_an_overwrite_after_the_command_returns(self):
        # Final3: the consumer reads once, verifies that buffer, and emits it. What the
        # reader adjudicates is this stdout, so a swap landing after the command returned
        # cannot reach it — there is no second open to poison.
        materialized = self.materialize_findings()
        expected = handoff.digest("finding A\n")
        code, emitted, opens = self.consume(materialized, expected)
        self.assertEqual(code, 0)
        self.assertEqual(emitted, b"finding A\n")
        self.assertEqual(len(opens), 1)
        with open(materialized, "w", encoding="utf-8") as handle:
            handle.write("finding B, swapped after consume returned\n")
        # The verified bytes already in hand are unchanged, and nothing reopened the path.
        self.assertEqual(emitted, b"finding A\n")
        self.assertEqual(len(opens), 1)
        self.assertEqual(handoff.digest(emitted.decode("utf-8")), expected)

    def test_a_swapped_payload_makes_the_consumer_emit_nothing(self):
        materialized = self.materialize_findings()
        expected = handoff.digest("finding A\n")
        with open(materialized, "w", encoding="utf-8") as handle:
            handle.write("finding B, swapped before consume\n")
        code, emitted, opens = self.consume(materialized, expected)
        self.assertEqual(code, 2)
        self.assertEqual(emitted, b"")
        self.assertEqual(len(opens), 1)

    def test_materialized_payload_cannot_be_swapped_under_the_challenger(self):
        # Final review 4: the copy the Challenger reads must be the bytes the digest
        # describes, checked at dispatch time — not a mutable file written once.
        findings = os.path.join(self.run_dir, "findings.md")
        with open(findings, "w", encoding="utf-8") as handle:
            handle.write("finding A\n")
        self.assertEqual(handoff.main(self.publish_argv("hunter-types", findings)), 0)
        base = os.path.join(self.run_dir, "hunter.accepted.md")
        verify = self.verify_argv("hunter-types", 1, 1, base)
        self.assertEqual(handoff.main(verify), 0)
        self.assertEqual(handoff.main(verify), 0)  # idempotent while the bytes match
        materialized = os.path.join(self.run_dir, self.materialized_paths(base)[0])
        with open(materialized, "w", encoding="utf-8") as handle:
            handle.write("finding B, swapped before dispatch\n")
        self.assertEqual(handoff.main(verify), 2)
        with open(materialized, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "finding B, swapped before dispatch\n")

    def test_verify_materializes_the_immutable_accepted_payload(self):
        # Finding 4: the downstream seat must read the bytes its digest describes, not a
        # file the producer can still edit.
        findings = os.path.join(self.run_dir, "findings.md")
        with open(findings, "w", encoding="utf-8") as handle:
            handle.write("finding A\n")
        self.assertEqual(handoff.main(self.publish_argv("hunter-types", findings)), 0)
        base = os.path.join(self.run_dir, "hunter-findings.accepted.md")
        self.assertEqual(handoff.main(self.verify_argv("hunter-types", 1, 1, base)), 0)
        with open(findings, "w", encoding="utf-8") as handle:
            handle.write("finding B, swapped after acceptance\n")
        copy = os.path.join(self.run_dir, self.materialized_paths(base)[0])
        with open(copy, encoding="utf-8") as handle:
            materialized = handle.read()
        self.assertEqual(materialized, "finding A\n")
        accepted, _ = handoff.scan(self.run_dir,
                                   expectation(seat_id="hunter-types", attempts=[1]))
        self.assertEqual(handoff.digest(materialized), accepted[1]["payload_digest"])

    def test_duplicate_attempt_publication_fails_loudly(self):
        payload = os.path.join(self.run_dir, "findings.md")
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write("findings\n")
        argv = self.publish_argv("hunter-types", payload)
        self.assertEqual(handoff.main(argv), 0)
        self.assertEqual(handoff.main(argv), 2)

    def test_publish_refuses_unsafe_identities(self):
        payload = os.path.join(self.run_dir, "findings.md")
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write("findings\n")
        argv = self.publish_argv("hunter-types", payload)
        argv[argv.index(RUN_ID)] = "run; touch /tmp/owned"
        self.assertEqual(handoff.main(argv), 2)
        argv = self.publish_argv("hunter-types", payload)
        argv[argv.index(INPUT_DIGEST)] = "not-a-digest"
        self.assertEqual(handoff.main(argv), 2)


class DispatchOnceTests(unittest.TestCase):
    """The ad-hoc prompt path: sotto 135a533a surfaced ten bare wait timeouts in six
    hours, every one of them a prompt that had actually landed."""

    def dispatch(self, agent, settle_timeout_ms=15000, process_groups=None):
        transport = ScriptedTransport({"seat": agent}, process_groups)
        return handoff.dispatch_once(transport, "seat", "Read /brief.md and follow it",
                                     settle_timeout_ms=settle_timeout_ms,
                                     wait_timeout_ms=1000)

    def dispatch_retry(self, agent, retry_stalled_for_ms, sleeper=lambda _seconds: None):
        transport = ScriptedTransport({"seat": agent})
        result = handoff.dispatch_with_stalled_backoff(
            transport, "seat", "Read /brief.md and follow it",
            settle_timeout_ms=0, wait_timeout_ms=1000,
            retry_stalled_for_ms=retry_stalled_for_ms, sleeper=sleeper)
        return result, transport

    def test_default_stalled_prompt_never_retries(self):
        agent = ScriptedAgent(kind="agy", prompt_code=handoff.PROMPT_STALLED_CODE)
        result, _transport = self.dispatch_retry(agent, 0)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertEqual(agent.prompts, 1)
        self.assertNotIn("retry_count", result)
        self.assertNotIn("[agent-worker-history]", agent.last_prompt)

    def test_retry_is_refused_for_claude_without_prompting(self):
        agent = ScriptedAgent(kind="claude", prompt_code=handoff.PROMPT_STALLED_CODE)
        result, _transport = self.dispatch_retry(agent, 10_000)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "retry_requires_agy_or_codex")
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(agent.prompts, 0)

    def test_codex_stalled_prompt_is_not_retried(self):
        agent = ScriptedAgent(kind="codex", prompt_code=handoff.PROMPT_STALLED_CODE)
        result, _transport = self.dispatch_retry(agent, 10_000)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(agent.prompts, 1)

    def test_codex_swallow_signature_retries_once_and_lands(self):
        def accept_second_prompt(agent, count, _text):
            if count == 2:
                agent.prompt_code = None
                agent.prompt_status = "working"

        sleeps = []
        agent = ScriptedAgent(kind="codex", status="done",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE,
                              on_prompt=accept_second_prompt)
        result, _transport = self.dispatch_retry(agent, 10_000, sleeps.append)
        self.assertEqual(result["outcome"], handoff.LANDED_WORKING)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(agent.prompts, 2)

    def test_codex_swallow_retry_is_capped_at_one(self):
        agent = ScriptedAgent(kind="codex", status="done",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE)
        result, _transport = self.dispatch_retry(agent, 10_000)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)
        self.assertEqual(result["reason"], handoff.UPTAKE_UNOBSERVED_CODE)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(agent.prompts, 2)

    def test_agy_swallow_signature_is_not_retried(self):
        agent = ScriptedAgent(kind="agy", status="done",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE)
        result, _transport = self.dispatch_retry(agent, 10_000)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)
        self.assertEqual(result["retry_count"], 0)
        self.assertEqual(agent.prompts, 1)

    def test_agy_stalled_retry_lands_after_backoff(self):
        def accept_second_prompt(agent, count, _text):
            if count == 2:
                agent.prompt_code = None
                agent.prompt_status = "working"

        sleeps = []
        agent = ScriptedAgent(kind="agy", prompt_code=handoff.PROMPT_STALLED_CODE,
                              on_prompt=accept_second_prompt)
        result, _transport = self.dispatch_retry(agent, 10_000, sleeps.append)
        self.assertEqual(result["outcome"], handoff.LANDED_WORKING)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(result["retry_wait_ms"], 1_000)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(agent.prompts, 2)
        self.assertEqual(agent.last_prompt.count("[agent-worker-history]"), 1)

    def test_agy_stalled_retry_exhausts_exact_delay_budget(self):
        sleeps = []
        agent = ScriptedAgent(kind="agy", prompt_code=handoff.PROMPT_STALLED_CODE)
        result, _transport = self.dispatch_retry(agent, 10_000, sleeps.append)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertEqual(result["retry_count"], 4)
        self.assertEqual(result["retry_wait_ms"], 10_000)
        self.assertEqual(sleeps, [1.0, 2.0, 4.0, 3.0])
        self.assertEqual(agent.prompts, 5)

    def test_agy_stalled_retry_stops_when_delivery_is_no_longer_proven_absent(self):
        def become_indeterminate(agent, count, _text):
            if count == 2:
                agent.prompt_code = handoff.WAIT_TIMEOUT_CODE

        sleeps = []
        agent = ScriptedAgent(kind="agy", prompt_code=handoff.PROMPT_STALLED_CODE,
                              on_prompt=become_indeterminate)
        result, _transport = self.dispatch_retry(agent, 10_000, sleeps.append)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)
        self.assertEqual(result["retry_count"], 1)
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(agent.prompts, 2)

    def test_a_timed_out_uptake_on_an_unchanged_settle_tags_the_swallow_signature(self):
        agent = ScriptedAgent(kind="codex", status="done",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)
        self.assertEqual(result["reason"], handoff.UPTAKE_UNOBSERVED_CODE)

    def test_a_busy_seat_is_reported_rather_than_prompted(self):
        agent = ScriptedAgent(status="working")
        result = self.dispatch(agent, settle_timeout_ms=0)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "status:working")
        self.assertFalse(result["prompted"])
        self.assertEqual(agent.prompts, 0)

    def test_prompt_wait_stops_at_worker_uptake(self):
        agent = ScriptedAgent(prompt_status="working")
        transport = ScriptedTransport({"seat": agent})
        result = handoff.dispatch_once(
            transport, "seat", "Read /brief.md and follow it",
            settle_timeout_ms=0, wait_timeout_ms=30_000)
        self.assertEqual(result["outcome"], handoff.LANDED_WORKING)
        self.assertEqual(agent.last_prompt_until, handoff.UPTAKE_STATES)
        self.assertEqual(agent.last_prompt_timeout_ms, handoff.UPTAKE_TIMEOUT_MS)

    def test_a_working_seat_is_waited_out_and_then_prompted(self):
        def settle(agent, _count):
            agent.status = "idle"

        agent = ScriptedAgent(status="working", on_wait=settle)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SETTLED)
        self.assertEqual(agent.waits, 1)
        self.assertEqual(agent.prompts, 1)

    def test_a_blocked_seat_is_never_prompted(self):
        # `blocked` settles, so a plain `--wait` returns happy while the text queues
        # behind the dialog the seat is holding.
        agent = ScriptedAgent(status="blocked")
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "status:blocked")
        self.assertEqual(agent.prompts, 0)

    def test_a_seat_that_is_not_input_ready_is_never_prompted(self):
        agent = ScriptedAgent(ready=False)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "not_interactive_ready")
        self.assertEqual(agent.prompts, 0)

    def test_a_missing_seat_is_never_prompted(self):
        agent = ScriptedAgent(missing=True)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "agent_not_found")
        self.assertEqual(agent.prompts, 0)

    def test_a_wait_timeout_on_a_working_seat_reports_that_the_prompt_landed(self):
        # The exact 135a533a shape: herdr answers `timeout`, the seat is working, and the
        # brief is already being executed. A re-prompt here stacks a second turn.
        def start_turn(agent, _count, _text):
            agent.status = "working"

        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE, on_prompt=start_turn)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.LANDED_WORKING)
        self.assertIn("do not re-prompt", result["next_action"])
        self.assertIn(result["outcome"], handoff.LANDED_OUTCOMES)

    def test_a_stalled_prompt_is_the_only_never_landed_verdict(self):
        agent = ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertEqual(result["reason"], handoff.PROMPT_STALLED_CODE)
        self.assertIn("never blind re-prompt", result["next_action"])

    def test_a_wait_timeout_on_a_settled_seat_is_unconfirmed_not_never_landed(self):
        # herdr only answers `timeout` after observing a state change, so the turn did
        # start. Calling this never-landed would license the duplicate turn.
        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)
        self.assertNotIn(result["outcome"], handoff.UNSENT_OUTCOMES)
        self.assertIn(result["outcome"], handoff.LANDED_OUTCOMES)

    def test_a_replaced_pane_is_reported_instead_of_counted_as_delivery(self):
        def replace(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE, on_prompt=replace)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.REPLACED)
        self.assertEqual(result["occupant"], "w1:p1:session:b")

    def test_a_blocked_turn_after_a_wait_timeout_asks_for_an_answer(self):
        def ask(agent, _count, _text):
            agent.status = "blocked"

        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE, on_prompt=ask)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.BLOCKED)

    def test_a_prompt_that_settles_into_blocked_is_not_reported_as_done(self):
        """Round 5: `blocked` is a settle state, so a prompt landing on a question
        SATISFIES `--wait` and replies ok — the happy path, not the timeout path, is the
        normal way to observe a blocked seat. Reporting `settled` there sends the chair
        off to read an artifact the seat is waiting on it to unblock."""
        def ask(agent, _count, _text):
            agent.status = "blocked"

        agent = ScriptedAgent(on_prompt=ask)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.BLOCKED)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("answer it", result["next_action"])
        self.assertNotIn("read the seat's artifact", result["next_action"])

    def test_blocked_survives_either_read_falling_back_to_idle(self):
        """Round 6: herdr's `idle` means "nothing said otherwise", so one of the two reads
        can miss a question the other saw. Requiring both to agree loses it — and the
        round-5 shape returns, reporting `settled` while the payload still says blocked."""
        def flip_on(target_get, value):
            def hook(agent, count):
                if count == target_get:
                    agent.status = value
            return hook

        # get #1 preflight, #2 builds the prompt reply, #3 is the post-prompt read.
        cases = {
            "reply saw it, post-read fell back to idle": (
                flip_on(2, "blocked"), flip_on(3, "idle")),
            "reply fell back to idle, post-read saw it": (
                None, flip_on(3, "blocked")),
        }
        for label, (during_prompt, after_prompt) in cases.items():
            with self.subTest(divergence=label):
                def on_get(agent, count):
                    if during_prompt:
                        during_prompt(agent, count)
                    after_prompt(agent, count)

                agent = ScriptedAgent(on_get=on_get)
                result = self.dispatch(agent)
                self.assertEqual(result["outcome"], handoff.BLOCKED)
                # outcome and status must not contradict each other in the payload.
                self.assertEqual(result["status"], "blocked")
                self.assertIn("answer it", result["next_action"])

    def test_a_settle_wait_that_ends_on_a_question_stops_before_prompting(self):
        """Round 7: the settle wait's own reply was discarded. It can end on `blocked`
        while the get straight after falls back to idle — and the seat then gets prompted
        behind the unanswered dialog."""
        def fall_back_to_idle(agent, _count):
            agent.status = "idle"

        agent = ScriptedAgent(status="working", wait_status="blocked",
                              on_wait=fall_back_to_idle)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "status:blocked")
        self.assertEqual(agent.prompts, 0, "nothing may be sent behind a live question")

    def test_a_transport_error_is_classified_not_short_circuited(self):
        """Round 10: the ad-hoc path returned before the shared classifier ever ran, so
        the two callers disagreed about the same reply. It now routes through, which also
        means the identity metadata is populated as on every other outcome."""
        def churn(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        result = self.dispatch(ScriptedAgent(prompt_code="closed", on_prompt=churn))
        self.assertEqual(result["outcome"], handoff.UNKNOWN)
        self.assertEqual(result["reason"], "closed")
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_CHANGED)
        self.assertNotIn(result["outcome"], handoff.LANDED_OUTCOMES)
        self.assertNotIn(result["outcome"], handoff.UNSENT_OUTCOMES)
        self.assertIn("do not send again", result["next_action"])

    def test_both_callers_agree_on_every_reply_code(self):
        """The convergence check: one reply code must not mean two things. `stalled` is
        provably unsent, `timeout` landed, anything else is indeterminate — and each
        caller's own vocabulary has to line up with that on both sides."""
        cases = {
            handoff.PROMPT_STALLED_CODE: (handoff.NEVER_LANDED, handoff.POST_PROMPT_STALLED),
            handoff.WAIT_TIMEOUT_CODE: (handoff.LANDED_UNCONFIRMED, handoff.POST_PROMPT_OPEN),
            "closed": (handoff.UNKNOWN, handoff.POST_PROMPT_INDETERMINATE),
            "cancelled": (handoff.UNKNOWN, handoff.POST_PROMPT_INDETERMINATE),
        }
        for code, (expected_outcome, expected_decision) in cases.items():
            with self.subTest(reply_code=code):
                self.assertEqual(
                    handoff.classify_post_prompt(handoff.Reply(False, code),
                                                 handoff.OCCUPANT_SAME),
                    expected_decision)
                self.assertEqual(
                    self.dispatch(ScriptedAgent(prompt_code=code))["outcome"],
                    expected_outcome)

    def test_the_same_precedence_holds_on_the_ad_hoc_path(self):
        """Round 8 asked for the rule at BOTH sites. The ad-hoc path already ordered it
        this way (rounds 3 and 7); these pin it so the two cannot drift apart."""
        # Direction 1 — identity is validated before a question is credited to our seat.
        def replace_and_ask(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"
            agent.status = "blocked"

        replaced = self.dispatch(ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE,
                                               on_prompt=replace_and_ask))
        self.assertEqual(replaced["outcome"], handoff.REPLACED)

        # Direction 2 — a proven non-delivery outranks that same churn.
        def churn(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        stalled = self.dispatch(ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE,
                                              on_prompt=churn))
        self.assertEqual(stalled["outcome"], handoff.NEVER_LANDED)
        self.assertEqual(stalled["occupant_verdict"], handoff.OCCUPANT_CHANGED)

    def test_a_question_from_a_replacement_during_the_settle_wait_is_not_ours(self):
        # Ad-hoc mirror of the collector's wait-path direction-1 case.
        def replace_and_ask(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(status="working", wait_status="blocked",
                              on_wait=replace_and_ask)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "occupant_changed_while_settling")
        self.assertEqual(agent.prompts, 0)

    def test_blocked_is_reported_the_same_by_whichever_path_reveals_it(self):
        for label, code in (("happy reply", None), ("wait timeout", handoff.WAIT_TIMEOUT_CODE)):
            with self.subTest(path=label):
                def ask(agent, _count, _text):
                    agent.status = "blocked"

                agent = ScriptedAgent(prompt_code=code, on_prompt=ask)
                result = self.dispatch(agent)
                self.assertEqual(result["outcome"], handoff.BLOCKED)
                # Delivered either way: the text reached the seat, which then asked.
                self.assertIn(result["outcome"], handoff.LANDED_OUTCOMES)

    def register_session(self, value="w1:p5:session:abc"):
        def hook(agent, _count, _text):
            agent.occupant = value
        return hook

    def test_a_fresh_pane_naming_its_session_is_not_a_replacement(self):
        """Live dogfooding: the first prompt to a freshly spawned codex pane reported
        `replaced`. Nothing moved — herdr's integration registered the session between
        the two reads, so the fingerprint flipped from the process-group form. The pane's
        process group is unchanged, which is what makes the flip provably benign."""
        agent = ScriptedAgent(occupant="w1:p5:pgid4242", kind="codex",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE,
                              on_prompt=self.register_session())
        result = self.dispatch(agent, process_groups={"w1:p5": 4242})
        self.assertNotEqual(result["outcome"], handoff.REPLACED)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)

    def test_a_new_occupant_that_registers_a_session_is_still_a_replacement(self):
        """Round 2 finding 2: the sessionless occupant exits and its replacement
        registers a session in the same pane. Same-pane pgid->session alone would have
        called that benign; the moved process group is what exposes it."""
        agent = ScriptedAgent(occupant="w1:p5:pgid4242", kind="codex",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE,
                              on_prompt=self.register_session("w1:p5:session:new-agent"))
        result = self.dispatch(agent, process_groups={"w1:p5": 9999})
        self.assertEqual(result["outcome"], handoff.REPLACED)

    def test_an_unreadable_process_group_never_blesses_an_upgrade(self):
        # Cannot confirm the pgid -> cannot claim the occupant survived.
        agent = ScriptedAgent(occupant="w1:p5:pgid4242", kind="codex",
                              prompt_code=handoff.WAIT_TIMEOUT_CODE,
                              on_prompt=self.register_session())
        result = self.dispatch(agent, process_groups={})
        self.assertEqual(result["outcome"], handoff.UNKNOWN)
        self.assertEqual(result["reason"], "occupant_unverifiable")

    def test_an_occupant_lost_after_dispatch_is_not_a_clean_delivery(self):
        """Round 2 finding 1: preflight pinned an occupant, the post-prompt read cannot
        derive one. Delivery to the pinned seat is indeterminate, so `settled`/exit 0
        would be a lie."""
        def lose_identity(agent, _count, _text):
            agent.occupant = None

        agent = ScriptedAgent(kind="agy", on_prompt=lose_identity)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.UNKNOWN)
        self.assertEqual(result["reason"], "occupant_unverifiable")
        self.assertNotIn(result["outcome"], handoff.LANDED_OUTCOMES)
        self.assertNotIn(result["outcome"], handoff.UNSENT_OUTCOMES)

    def test_an_unpinnable_seat_is_refused_before_anything_is_sent(self):
        """Round 3 finding 2 reverses the round-2 call here. With no identity pinned there
        is no way to say afterwards that the text reached THIS seat, and nothing has been
        sent yet — so declining costs nothing and carries no duplicate-turn risk."""
        agent = ScriptedAgent(kind="agy", occupant=None)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "occupant_unpinned")
        self.assertFalse(result["prompted"])
        self.assertEqual(agent.prompts, 0)
        self.assertIn(result["outcome"], handoff.UNSENT_OUTCOMES)

    def test_a_replacement_during_the_settle_wait_stops_before_sending(self):
        """Round 3 finding 1: waiting out a turn is its own identity window. The occupant
        we agreed to wait for can exit and be replaced before the wait returns."""
        def replace_while_waiting(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(status="working", on_wait=replace_while_waiting)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "occupant_changed_while_settling")
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_CHANGED)
        self.assertEqual(agent.prompts, 0, "the replacement must not inherit the text")

    def test_an_occupant_lost_during_the_settle_wait_also_stops(self):
        def lose_while_waiting(agent, _count):
            agent.status = "idle"
            agent.occupant = None

        agent = ScriptedAgent(status="working", on_wait=lose_while_waiting)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "occupant_unverifiable_while_settling")
        self.assertEqual(agent.prompts, 0)

    def test_a_benign_upgrade_during_the_settle_wait_still_prompts(self):
        # The fresh-codex flip must not become a false stop on the settle path either.
        def register_session(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p5:session:abc"

        agent = ScriptedAgent(status="working", occupant="w1:p5:pgid4242", kind="codex",
                              on_wait=register_session)
        result = self.dispatch(agent, process_groups={"w1:p5": 4242})
        self.assertEqual(result["outcome"], handoff.SETTLED)
        self.assertEqual(agent.prompts, 1)

    def test_a_stalled_prompt_stays_never_landed_even_when_the_occupant_changes(self):
        """Round 3 finding 3: `agent_prompt_stalled` is direct evidence on the delivery
        axis — the text reached no turn. Later churn cannot promote that to `replaced`
        ("the text went to the new occupant"), which would drop a provably unsent prompt
        and swap its exit 1 for a 2."""
        def stall_then_replace(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE,
                              on_prompt=stall_then_replace)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertIn(result["outcome"], handoff.UNSENT_OUTCOMES)
        # The churn is not lost — it moves to the identity axis and the next action.
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_CHANGED)
        self.assertIn("Occupant identity afterwards: changed", result["next_action"])

    def test_a_stalled_prompt_survives_an_unreadable_post_prompt_get(self):
        """Round 4 finding 2: the same override as round 3, one branch higher. A failed
        identity re-read is absence of identity evidence, not evidence about delivery —
        `agent_prompt_stalled` already proved the text reached no turn."""
        def stall_then_vanish(agent, _count, _text):
            agent.missing = True

        agent = ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE,
                              on_prompt=stall_then_vanish)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
        self.assertIn(result["outcome"], handoff.UNSENT_OUTCOMES)
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_UNVERIFIABLE)

    def test_an_unreadable_post_prompt_get_is_still_unknown_when_not_stalled(self):
        # Without the stalled proof there is nothing authoritative to preserve.
        def vanish(agent, _count, _text):
            agent.missing = True

        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE, on_prompt=vanish)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.UNKNOWN)
        self.assertEqual(result["reason"], "agent_not_found")

    def test_a_working_seat_with_no_pinnable_occupant_is_refused_before_waiting(self):
        """Round 4 finding 1: the settle guard compares pre- against post-wait identity,
        but with no pre-wait identity there is nothing to compare. Whoever turns up
        settled would then be prompted and reported as a clean delivery."""
        def arrive(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(status="working", occupant=None, kind="agy", on_wait=arrive)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.SEAT_UNSETTLED)
        self.assertEqual(result["reason"], "occupant_unpinned")
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_UNPINNED)
        self.assertEqual(agent.prompts, 0)
        self.assertEqual(agent.waits, 0, "there is nothing to wait for without a baseline")
        self.assertIn(result["outcome"], handoff.UNSENT_OUTCOMES)

    def test_the_two_axes_are_reported_independently(self):
        agent = ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE)
        result = self.dispatch(agent)
        self.assertEqual(result["occupant_verdict"], handoff.OCCUPANT_SAME)
        self.assertEqual(result["outcome"], handoff.LANDED_UNCONFIRMED)

    def test_a_genuine_swap_within_one_scheme_is_still_a_replacement(self):
        for before, after in (("w1:p5:pgid100", "w1:p5:pgid999"),
                              ("w1:p5:session:a", "w1:p5:session:b"),
                              ("w1:p5:session:a", "w2:p9:session:a")):
            with self.subTest(before=before, after=after):
                def swap(agent, _count, _text, new=after):
                    agent.occupant = new

                agent = ScriptedAgent(occupant=before,
                                      prompt_code=handoff.WAIT_TIMEOUT_CODE, on_prompt=swap)
                self.assertEqual(self.dispatch(agent)["outcome"], handoff.REPLACED)

    def test_the_pure_classifier_defers_exactly_the_ambiguous_cases(self):
        cases = (
            (None, "w1:p5:session:a", handoff.OCCUPANT_UNPINNED),
            ("w1:p5:session:a", None, handoff.OCCUPANT_UNVERIFIABLE),
            ("w1:p5:session:a", "w1:p5:session:a", handoff.OCCUPANT_SAME),
            # A vanished session is not evidence the occupant survived.
            ("w1:p5:session:a", "w1:p5:pgid100", handoff.OCCUPANT_CHANGED),
            ("w1:p5:pgid100", "w1:p5:session:a", handoff.OCCUPANT_UPGRADED),
            ("w1:p5:pgid100", "w2:p9:session:a", handoff.OCCUPANT_CHANGED),
            ("w1:p5:pgid100", "w1:p5:pgid999", handoff.OCCUPANT_CHANGED),
        )
        for before, after, expected in cases:
            with self.subTest(before=before, after=after):
                self.assertEqual(handoff._classify_occupant(before, after), expected)

    def test_only_an_upgrade_costs_a_process_group_read(self):
        transport = ScriptedTransport({}, {"w1:p5": 100})
        for before, after in (("w1:p5:session:a", "w1:p5:session:a"),
                              ("w1:p5:pgid100", "w1:p5:pgid999"),
                              (None, "w1:p5:session:a")):
            handoff._occupant_verdict(transport, before, after)
        self.assertEqual(transport.process_group_calls, [])
        handoff._occupant_verdict(transport, "w1:p5:pgid100", "w1:p5:session:a")
        self.assertEqual(transport.process_group_calls, ["w1:p5"])

    def test_a_replacement_is_caught_even_when_the_prompt_reply_is_happy(self):
        # A settled reply proves a turn ended, never that it was our occupant's turn.
        def swap(agent, _count, _text):
            agent.occupant = "w1:p1:session:b"

        agent = ScriptedAgent(on_prompt=swap)
        result = self.dispatch(agent)
        self.assertEqual(result["outcome"], handoff.REPLACED)

    def test_exit_codes_never_call_a_delivered_prompt_unsent(self):
        """Finding 1: exit 1 is the documented 'nothing was sent' signal, so every
        outcome that did reach the seat has to stay out of it."""
        for outcome in handoff.LANDED_OUTCOMES:
            self.assertNotIn(outcome, handoff.UNSENT_OUTCOMES)
        for outcome in (handoff.UNKNOWN, handoff.REPLACED):
            self.assertNotIn(outcome, handoff.LANDED_OUTCOMES)
            self.assertNotIn(outcome, handoff.UNSENT_OUTCOMES)
        self.assertIn(handoff.BLOCKED, handoff.LANDED_OUTCOMES)
        self.assertIn(handoff.LANDED_UNCONFIRMED, handoff.LANDED_OUTCOMES)

    def test_the_cli_maps_each_outcome_to_its_exit_code(self):
        """End to end through `main`, because the exit code is what a chair scripts on."""
        cases = (
            ({"agent.prompt": agent_info(status="idle", session="s1")}, 0, "settled"),
            ({"agent.prompt": {"error": {"code": handoff.WAIT_TIMEOUT_CODE}},
              "agent.get": [agent_info(status="idle", session="s1"),
                            agent_info(status="working", session="s1")]},
             0, "landed_and_working"),
            ({"agent.prompt": {"error": {"code": handoff.PROMPT_STALLED_CODE}}},
             1, "never_landed"),
            ({"agent.get": agent_info(status="working", session="s1")},
             1, "seat_unsettled"),
            ({"agent.prompt": {"error": {"code": "closed"}}}, 2, "unknown"),
        )
        for responses, expected_code, expected_outcome in cases:
            with self.subTest(outcome=expected_outcome):
                responses.setdefault("agent.get", agent_info(status="idle", session="s1"))
                responses.setdefault("agent.wait", agent_info(status="working", session="s1"))
                server = FakeHerdrServer(responses)
                self.addCleanup(server.stop)
                os.environ["HERDR_SOCKET_PATH"] = server.path
                self.addCleanup(os.environ.pop, "HERDR_SOCKET_PATH", None)
                stdout = io.StringIO()
                real_stdout, sys.stdout = sys.stdout, stdout
                try:
                    code = handoff.main(["prompt", "--target", "seat", "--text", "go",
                                         "--settle-timeout", "0", "--wait-timeout", "50"])
                finally:
                    sys.stdout = real_stdout
                self.assertEqual(json.loads(stdout.getvalue())["outcome"], expected_outcome)
                self.assertEqual(code, expected_code)

    def test_stalled_is_authoritative_across_every_post_prompt_condition(self):
        """Invariant, not an instance. Rounds 3 and 4 each found one branch that let a
        later observation overwrite `agent_prompt_stalled`; this sweeps the conditions so
        a future branch cannot reintroduce a third."""
        def mutate(field, value):
            def hook(agent, _count, _text):
                setattr(agent, field, value)
            return hook

        conditions = {
            "unchanged": mutate("status", "idle"),
            "occupant replaced": mutate("occupant", "w1:p1:session:b"),
            "identity lost": mutate("occupant", None),
            "seat unreachable": mutate("missing", True),
            "now working": mutate("status", "working"),
            "now blocked": mutate("status", "blocked"),
        }
        for label, hook in conditions.items():
            with self.subTest(after=label):
                agent = ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE,
                                      on_prompt=hook)
                result = self.dispatch(agent)
                self.assertEqual(result["outcome"], handoff.NEVER_LANDED)
                self.assertIn(result["outcome"], handoff.UNSENT_OUTCOMES)

    def test_no_route_reports_delivery_without_a_pinned_baseline(self):
        """The other recurring shape: an unpinned occupant reaching a clean delivery.
        Both routes to one — straight through, and via the settle wait — must refuse."""
        def arrive(agent, _count):
            agent.status = "idle"
            agent.occupant = "w1:p1:session:b"

        routes = {
            "direct": ScriptedAgent(occupant=None, kind="agy"),
            "after settle wait": ScriptedAgent(status="working", occupant=None,
                                               kind="agy", on_wait=arrive),
        }
        for label, agent in routes.items():
            with self.subTest(route=label):
                result = self.dispatch(agent)
                self.assertNotIn(result["outcome"], handoff.LANDED_OUTCOMES)
                self.assertEqual(result["reason"], "occupant_unpinned")
                self.assertEqual(agent.prompts, 0)

    def test_no_outcome_is_an_unclassified_timeout(self):
        """The refutation condition: a bare `timeout` must never reach the chair."""
        agents = {
            "busy": ScriptedAgent(status="working"),
            "stalled": ScriptedAgent(prompt_code=handoff.PROMPT_STALLED_CODE),
            "timed-out": ScriptedAgent(prompt_code=handoff.WAIT_TIMEOUT_CODE),
            "gone": ScriptedAgent(missing=True),
        }
        classified = {handoff.SEAT_UNSETTLED, handoff.NEVER_LANDED, handoff.SETTLED,
                      handoff.LANDED_WORKING, handoff.LANDED_UNCONFIRMED,
                      handoff.BLOCKED, handoff.REPLACED}
        for label, agent in agents.items():
            with self.subTest(seat=label):
                result = self.dispatch(agent, settle_timeout_ms=0)
                self.assertIn(result["outcome"], classified)
                self.assertNotEqual(result["outcome"], handoff.WAIT_TIMEOUT_CODE)
                self.assertTrue(result["next_action"])


class SandboxedRunDirTests(unittest.TestCase):
    """ORCH-01: a Codex `workspace-write` seat cannot publish into a home-state run dir."""

    ENV = ("HERDR_BUS_DIR", "HERDR_WORKSPACE_ID", "TMPDIR", "HERDR_AGENT_PANE",
           "AGENT_TEAMMATE_CHILD")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bus_root = os.path.join(self.directory.name, "bus")
        # Stands in for ~/.local/state/deliver/...: a real directory outside every root a
        # sandboxed pane can write. TMPDIR is repointed so the harness's own temp root
        # does not whitelist it.
        self.home_state = os.path.join(self.directory.name, "home-state", "run")
        os.makedirs(os.path.join(self.directory.name, "tmpdir"))
        os.makedirs(self.home_state)
        self.scoped_env(HERDR_BUS_DIR=self.bus_root,
                        TMPDIR=os.path.join(self.directory.name, "tmpdir"))

    def scoped_env(self, **values):
        saved = {name: os.environ.get(name) for name in self.ENV}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in self.ENV:
            os.environ.pop(name, None)
        os.environ.update(values)

    def collect(self, run_dir, agent):
        transport = ScriptedTransport({"hunter-types": agent})
        collector = handoff.Collector(run_dir, RUN_ID, WORKFLOW, 1, transport,
                                      deadline_seconds=5.0, grace_seconds=0.01)
        seat = handoff.Seat("hunter-types", "hunter-types", "hunt for bugs", INPUT_DIGEST)
        return collector.collect([seat])

    def test_a_codex_seat_refuses_a_home_state_run_dir_before_dispatch(self):
        agent = ScriptedAgent(kind="codex",
                              on_prompt=publisher(self.home_state, "hunter-types"))
        with self.assertRaises(ValueError) as caught:
            self.collect(self.home_state, agent)
        message = str(caught.exception)
        self.assertIn(self.home_state, message)
        self.assertIn("hunter-types", message)
        self.assertIn("codex", message)
        # Refusal is free of side effects: no prompt, no checkpoint, no artifact dir.
        self.assertEqual(agent.prompts, 0)
        self.assertEqual(sorted(os.listdir(self.home_state)), [])

    def test_the_refusal_exits_non_zero_with_a_stderr_line(self):
        spec_path = os.path.join(self.home_state, "run.json")
        with open(spec_path, "w", encoding="utf-8") as handle:
            json.dump({
                "run_dir": self.home_state,
                "run_id": RUN_ID,
                "workflow": WORKFLOW,
                "round": 1,
                "deadline_seconds": 5,
                "seats": [{
                    "seat_id": "hunter-types",
                    "target": "hunter-types",
                    "prompt": "hunt",
                    "input_digest": INPUT_DIGEST,
                }],
            }, handle)
        transport = ScriptedTransport({"hunter-types": ScriptedAgent(kind="codex")})
        real_transport = handoff.SocketTransport
        handoff.SocketTransport = lambda budget=None: transport
        self.addCleanup(setattr, handoff, "SocketTransport", real_transport)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = handoff.main(["dispatch", "--spec", spec_path])
        self.assertEqual(code, 2)
        self.assertIn("not writable from a codex sandbox", stderr.getvalue())

    def test_a_codex_seat_under_the_bus_root_is_dispatched(self):
        run_dir = os.path.join(self.bus_root, RUN_ID)
        os.makedirs(run_dir)
        agent = ScriptedAgent(kind="codex", on_prompt=publisher(run_dir, "hunter-types"))
        report = self.collect(run_dir, agent)
        self.assertTrue(report["complete"])
        self.assertEqual(agent.prompts, 1)

    def test_a_claude_seat_keeps_a_home_state_run_dir(self):
        agent = ScriptedAgent(on_prompt=publisher(self.home_state, "hunter-types"))
        report = self.collect(self.home_state, agent)
        self.assertTrue(report["complete"])
        self.assertEqual(agent.prompts, 1)


class PendingSeatTests(unittest.TestCase):
    """ORCH-02: a turn must not end with a live worker and nothing listening for it."""

    ENV = ("HERDR_BUS_DIR", "HERDR_WORKSPACE_ID", "HERDR_SOCKET_PATH")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = os.path.join(self.directory.name, "run")
        os.makedirs(self.run_dir)
        self.bus_root = os.path.join(self.directory.name, "bus")
        saved = {name: os.environ.get(name) for name in self.ENV}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in self.ENV:
            os.environ.pop(name, None)
        os.environ["HERDR_BUS_DIR"] = self.bus_root

    def breadcrumbs(self):
        directory = os.path.join(self.bus_root, handoff.PENDING_DIR)
        if not os.path.isdir(directory):
            return []
        return sorted(name for name in os.listdir(directory) if name.endswith(".json"))

    def run_pending(self, *argv):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = handoff.main(["pending", *argv])
        return code, json.loads(stdout.getvalue())

    def collect(self, agent, *, dispatch_only=False, seat="hunter-types"):
        transport = ScriptedTransport({seat: agent})
        collector = handoff.Collector(self.run_dir, RUN_ID, WORKFLOW, 1, transport,
                                      deadline_seconds=5.0, grace_seconds=0.01)
        return collector.collect(
            [handoff.Seat(seat, seat, "hunt for bugs", INPUT_DIGEST)],
            dispatch_only=dispatch_only)

    def test_a_dispatched_seat_is_recorded_and_unowned(self):
        agent = ScriptedAgent(on_prompt=lambda worker, _count, _text:
                              setattr(worker, "status", "working"))
        report = self.collect(agent, dispatch_only=True)
        self.assertTrue(report["uptake_complete"])
        self.assertEqual(self.breadcrumbs(), ["hunter-types.json"])
        code, body = self.run_pending()
        self.assertEqual(code, 1)
        self.assertEqual(body["unowned"], ["hunter-types"])
        self.assertEqual(body["pending"][0]["via"], "dispatch")

    def test_a_completed_seat_leaves_nothing_owed(self):
        agent = ScriptedAgent(on_prompt=publisher(self.run_dir, "hunter-types"))
        report = self.collect(agent)
        self.assertTrue(report["complete"])
        self.assertEqual(self.breadcrumbs(), [])
        self.assertEqual(self.run_pending()[0], 0)

    def test_a_live_lease_covers_every_recorded_seat(self):
        self.collect(ScriptedAgent(on_prompt=lambda worker, _count, _text:
                                   setattr(worker, "status", "working")),
                     dispatch_only=True)
        self.assertEqual(self.run_pending()[0], 1)
        bus = handoff._load_bus("test")
        # what an armed `herdr-bus.py watch` leaves behind, owned by a live process
        bus.write_lease(bus.bus_dirs(), "chair", 900)
        code, body = self.run_pending()
        self.assertEqual(code, 0)
        self.assertEqual(body["pending"][0]["listeners"], ["chair"])

    def test_a_dead_watchers_lease_owns_nothing(self):
        self.collect(ScriptedAgent(on_prompt=lambda worker, _count, _text:
                                   setattr(worker, "status", "working")),
                     dispatch_only=True)
        bus = handoff._load_bus("test")
        path = bus.write_lease(bus.bus_dirs(), "chair", 900)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": 2 ** 22, "ttl_s": 900}, handle)  # exited watcher, never released
        code, body = self.run_pending()
        self.assertEqual(code, 1)
        self.assertEqual(body["pending"][0]["listeners"], [])

    def test_a_blocking_collect_holds_a_lease_while_it_waits(self):
        seen = []

        def look(worker, _count, _text):
            seen.append(sorted(name for name in
                               os.listdir(os.path.join(self.bus_root, "watch"))
                               if name.endswith(".lease")))
            handoff.publish(self.run_dir, document(seat_id="hunter-types"))

        self.collect(ScriptedAgent(on_prompt=look))
        self.assertEqual(seen, [[f"collect-{RUN_ID}.lease"]])
        # released on return: a collect that stopped waiting must not look like a listener
        self.assertEqual(os.listdir(os.path.join(self.bus_root, "watch")), [])

    def test_verify_acceptance_clears_the_breadcrumb(self):
        self.collect(ScriptedAgent(on_prompt=lambda worker, _count, _text:
                                   setattr(worker, "status", "working")),
                     dispatch_only=True)
        handoff.publish(self.run_dir, document(seat_id="hunter-types"))
        with contextlib.redirect_stdout(io.StringIO()):
            code = handoff.main(["verify", "--run-dir", self.run_dir, "--run-id", RUN_ID,
                                 "--workflow", WORKFLOW, "--seat", "hunter-types",
                                 "--round", "1", "--input-digest", INPUT_DIGEST])
        self.assertEqual(code, 0)
        self.assertEqual(self.breadcrumbs(), [])

    def test_clear_drops_a_seat_by_name(self):
        self.collect(ScriptedAgent(on_prompt=lambda worker, _count, _text:
                                   setattr(worker, "status", "working")),
                     dispatch_only=True)
        code, body = self.run_pending("--clear", "hunter-types")
        self.assertEqual(code, 0)
        self.assertEqual(body["pending"], [])

    def test_prompt_carries_the_emit_line_and_records_the_seat(self):
        sent = {}

        def capture(_transport, target, text, **_kwargs):
            sent["text"] = text
            return {"outcome": handoff.LANDED_WORKING, "prompted": True, "reason": None,
                    "status": "working", "occupant": None, "occupant_verdict": None,
                    "next_action": "wait"}

        real = handoff.dispatch_with_stalled_backoff
        handoff.dispatch_with_stalled_backoff = capture
        self.addCleanup(setattr, handoff, "dispatch_with_stalled_backoff", real)
        real_transport = handoff.SocketTransport
        handoff.SocketTransport = lambda budget=None: None
        self.addCleanup(setattr, handoff, "SocketTransport", real_transport)
        with contextlib.redirect_stdout(io.StringIO()):
            code = handoff.main(["prompt", "--target", "hunter-types", "--text", "round 2"])
        self.assertEqual(code, 0)
        self.assertIn("round 2", sent["text"])
        self.assertIn("herdr-bus", sent["text"])
        self.assertIn("--from hunter-types --kind done", sent["text"])
        self.assertIn(self.bus_root, sent["text"])
        self.assertEqual(self.breadcrumbs(), ["hunter-types.json"])
        self.assertEqual(self.run_pending()[0], 1)

    def test_an_unsent_prompt_records_nothing(self):
        def capture(_transport, target, text, **_kwargs):
            return {"outcome": handoff.NEVER_LANDED, "prompted": False, "reason": None,
                    "status": "idle", "occupant": None, "occupant_verdict": None,
                    "next_action": "resend"}

        real = handoff.dispatch_with_stalled_backoff
        handoff.dispatch_with_stalled_backoff = capture
        self.addCleanup(setattr, handoff, "dispatch_with_stalled_backoff", real)
        real_transport = handoff.SocketTransport
        handoff.SocketTransport = lambda budget=None: None
        self.addCleanup(setattr, handoff, "SocketTransport", real_transport)
        with contextlib.redirect_stdout(io.StringIO()):
            code = handoff.main(["prompt", "--target", "hunter-types", "--text", "round 2"])
        self.assertEqual(code, 1)
        self.assertEqual(self.breadcrumbs(), [])

    def test_without_a_bus_nothing_is_recorded_and_the_gate_passes(self):
        os.environ.pop("HERDR_BUS_DIR")
        agent = ScriptedAgent(on_prompt=lambda worker, _count, _text:
                              setattr(worker, "status", "working"))
        self.collect(agent, dispatch_only=True)
        self.assertFalse(os.path.exists(self.bus_root))
        self.assertEqual(self.run_pending()[0], 0)


class WorkerSessionGuardTests(unittest.TestCase):
    """HL-064 residual: a leaf must not chair collect/dispatch loops."""

    MARKERS = ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD", "HERDR_REGISTRY_ROOT",
               "HERDR_REGISTRY_CAPABILITY", "HERDR_REGISTRY_KEY")

    def scoped_env(self, **values):
        saved = {name: os.environ.get(name) for name in self.MARKERS}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        for name in self.MARKERS:
            os.environ.pop(name, None)
        os.environ.update(values)

    def test_collect_refuses_in_a_child_session(self):
        self.scoped_env(AGENT_TEAMMATE_CHILD="1")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(handoff.main(["collect", "--spec", "/nonexistent.json"]), 3)
        self.assertIn("orchestrator-only", stderr.getvalue())

    def test_dispatch_refuses_under_the_pane_marker(self):
        self.scoped_env(HERDR_AGENT_PANE="1")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(handoff.main(["dispatch", "--spec", "/nonexistent.json"]), 3)

    def test_a_registry_root_outranks_its_child_markers(self):
        self.scoped_env(AGENT_TEAMMATE_CHILD="1", HERDR_REGISTRY_ROOT="run",
                        HERDR_REGISTRY_CAPABILITY="cap")
        self.assertFalse(handoff._worker_session())

    def test_a_registry_leaf_key_keeps_the_refusal(self):
        self.scoped_env(AGENT_TEAMMATE_CHILD="1", HERDR_REGISTRY_ROOT="run",
                        HERDR_REGISTRY_CAPABILITY="cap", HERDR_REGISTRY_KEY="k")
        self.assertTrue(handoff._worker_session())


if __name__ == "__main__":
    unittest.main()
