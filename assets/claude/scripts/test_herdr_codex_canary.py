#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest


sys.dont_write_bytecode = True
SCRIPT_DIR = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


canary = load("herdr_codex_canary", "herdr-codex-canary.py")


def rollout_line(kind, payload):
    return json.dumps({"timestamp": "2026-07-29T00:00:00Z", "type": kind, "payload": payload}) + "\n"


def exec_call(call_id, cmd):
    return rollout_line("response_item", {"type": "function_call", "name": "exec_command",
                                          "call_id": call_id,
                                          "arguments": json.dumps({"cmd": cmd})})


def exec_output(call_id, output):
    return rollout_line("response_item", {"type": "function_call_output",
                                          "call_id": call_id, "output": output})


def spawn_call(name="spawn_agent"):
    return rollout_line("response_item", {"type": "function_call", "name": name,
                                          "namespace": "collaboration", "arguments": "{}"})


SCRATCH = "/tmp/herdr-canary/20260729T000000"


class ListDiffTests(unittest.TestCase):
    def test_pane_cwds_ignore_entries_without_an_id(self):
        payload = {"result": {"panes": [{"pane_id": "wK:p1", "cwd": SCRATCH},
                                        {"terminal_id": "term_x"}]}}
        self.assertEqual(canary.pane_cwds(payload), {"wK:p1": SCRATCH})

    def test_agent_cwds_pair_pane_with_session(self):
        payload = {"result": {"agents": [
            {"pane_id": "wK:p1", "agent_session": {"value": "s1"}, "cwd": SCRATCH},
            {"pane_id": "wK:p2", "name": "canary-codex-1", "cwd": "/Users/me"},
        ]}}
        self.assertEqual(canary.agent_cwds(payload),
                         {"wK:p1:s1": SCRATCH, "wK:p2:canary-codex-1": "/Users/me"})

    def test_a_replacement_occupant_in_a_known_pane_counts_as_new(self):
        before = canary.agent_cwds({"result": {"agents": [
            {"pane_id": "wK:p1", "agent_session": {"value": "s1"}, "cwd": SCRATCH}]}})
        after = canary.agent_cwds({"result": {"agents": [
            {"pane_id": "wK:p1", "agent_session": {"value": "s2"}, "cwd": SCRATCH}]}})
        self.assertEqual(canary.appeared_under(before, after, SCRATCH), ["wK:p1:s2"])

    def test_missing_lists_are_empty_not_an_error(self):
        self.assertEqual(canary.pane_cwds({}), {})
        self.assertEqual(canary.agent_cwds({"result": {}}), {})

    def test_only_newcomers_under_the_scratch_dir_count(self):
        before = {"wK:p1": SCRATCH}
        after = {
            "wK:p1": SCRATCH,
            "wK:p2": SCRATCH + "/worker",
            "wK:p3": "/Users/me/dotfiles",
            "wK:p4": "",
        }
        self.assertEqual(canary.appeared_under(before, after, SCRATCH), ["wK:p2"])

    def test_a_sibling_scratch_dir_is_not_under_this_run(self):
        self.assertFalse(canary.under(SCRATCH + "-other", SCRATCH))
        self.assertTrue(canary.under(SCRATCH, SCRATCH))


class RolloutParsingTests(unittest.TestCase):
    def test_counts_only_spawn_agent_function_calls(self):
        lines = [
            spawn_call(),
            spawn_call("wait_agent"),
            rollout_line("event_msg", {"type": "agent_message", "text": "calling spawn_agent"}),
            spawn_call(),
            "not json at all, mentions spawn_agent\n",
        ]
        self.assertEqual(canary.count_native_spawns(lines), 2)

    def test_a_clean_rollout_counts_zero(self):
        self.assertEqual(canary.count_native_spawns([rollout_line("response_item", {
            "type": "function_call", "name": "shell"})]), 0)

    def test_instructions_are_empty_when_every_channel_is_blank(self):
        lines = [
            rollout_line("session_meta", {"session_id": "s", "instructions": ""}),
            rollout_line("world_state", {"state": {"agents_md": {"text": "  "}}}),
        ]
        self.assertIs(canary.instructions_are_empty(lines), True)

    def test_instructions_are_not_empty_when_agents_md_carries_text(self):
        lines = [
            rollout_line("session_meta", {"session_id": "s"}),
            rollout_line("world_state", {"state": {"agents_md": {"text": "## Orchestration"}}}),
        ]
        self.assertIs(canary.instructions_are_empty(lines), False)

    def test_instructions_accept_the_structured_session_meta_form(self):
        lines = [rollout_line("session_meta", {"instructions": {"text": "you are codex"}})]
        self.assertIs(canary.instructions_are_empty(lines), False)

    def test_a_rollout_that_says_nothing_is_unknown(self):
        self.assertIsNone(canary.instructions_are_empty([spawn_call()]))

    def test_collaboration_state_reads_the_latest_turn_context(self):
        lines = [
            rollout_line("turn_context", {"multi_agent_version": "v2",
                                          "multi_agent_mode": "explicitRequestOnly"}),
            spawn_call(),
            rollout_line("turn_context", {"multi_agent_version": "v2",
                                          "multi_agent_mode": "off"}),
        ]
        self.assertEqual(canary.collaboration_state(lines), ("v2", "off"))

    def test_collaboration_state_ignores_non_turn_context_mentions(self):
        lines = [rollout_line("event_msg", {"type": "agent_message",
                                            "text": "multi_agent_mode is off"})]
        self.assertEqual(canary.collaboration_state(lines), (None, None))


DENIAL = ('Process exited with code 2\nOutput:\nagent-teammate: Error: Os { code: 1, '
          'kind: PermissionDenied, message: "Operation not permitted" }\n')


class ControlPlaneBlockedTests(unittest.TestCase):
    def test_a_denied_launcher_exec_is_the_evidence(self):
        # The 2026-07-29T05:26 post-injection root, in order: PATH miss, then the denial.
        lines = [
            exec_call("c1", 'agent-teammate codex artifact-writer-052612 --cwd "$PWD" --'),
            exec_output("c1", "Process exited with code 127\nOutput:\nzsh:1: command not "
                              "found: agent-teammate\n"),
            exec_call("c2", '/Users/me/.claude/scripts/agent-teammate.py codex w --cwd .'),
            exec_output("c2", DENIAL),
        ]
        self.assertTrue(canary.control_plane_blocked(lines))

    def test_a_denied_herdr_cli_exec_also_counts(self):
        lines = [
            exec_call("c1", "herdr status\nherdr pane list"),
            exec_output("c1", 'Error: Os { code: 1, kind: PermissionDenied, message: '
                              '"Operation not permitted" }'),
        ]
        self.assertTrue(canary.control_plane_blocked(lines))

    def test_a_missing_launcher_alone_is_not_a_denial(self):
        lines = [
            exec_call("c1", "agent-teammate codex worker --cwd ."),
            exec_output("c1", "Process exited with code 127\nOutput:\ncommand not found\n"),
        ]
        self.assertFalse(canary.control_plane_blocked(lines))

    def test_the_models_prose_is_never_the_evidence(self):
        lines = [rollout_line("response_item", {"type": "message", "role": "assistant",
                                                "content": [{"type": "output_text", "text":
                                                             "agent-teammate was blocked: "
                                                             "operation not permitted"}]})]
        self.assertFalse(canary.control_plane_blocked(lines))

    def test_a_denial_from_an_unrelated_command_does_not_count(self):
        lines = [
            exec_call("c1", "cat /etc/sudoers"),
            exec_output("c1", "cat: /etc/sudoers: Operation not permitted"),
            exec_call("c2", "herdr pane list"),
            exec_output("c2", '{"result": {"panes": []}}'),
        ]
        self.assertFalse(canary.control_plane_blocked(lines))

    def test_an_attempt_with_no_recorded_output_is_not_a_denial(self):
        self.assertFalse(canary.control_plane_blocked(
            [exec_call("c1", "agent-teammate codex worker --cwd .")]))


class RolloutLookupTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.day = os.path.join(self.root, "2026", "07", "29")
        os.makedirs(self.day)

    def write(self, name, cwd, age=0):
        path = os.path.join(self.day, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(rollout_line("session_meta", {"session_id": "x", "cwd": cwd}))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def test_session_id_wins_over_recency(self):
        self.write("rollout-2026-07-29T01-00-00-aaa.jsonl", "/tmp/other")
        wanted = self.write("rollout-2026-07-29T00-00-00-bbb.jsonl", "/tmp/x", age=600)
        self.assertEqual(canary.find_rollout(self.root, "bbb", "/tmp/x"), wanted)

    def test_falls_back_to_the_scratch_cwd_when_no_session_id_is_known(self):
        self.write("rollout-2026-07-29T01-00-00-aaa.jsonl", "/tmp/other")
        wanted = self.write("rollout-2026-07-29T02-00-00-ccc.jsonl", "/tmp/canary", age=1)
        self.assertEqual(canary.find_rollout(self.root, None, "/tmp/canary"), wanted)

    def test_reports_nothing_when_no_rollout_matches(self):
        self.write("rollout-2026-07-29T01-00-00-aaa.jsonl", "/tmp/other")
        self.assertIsNone(canary.find_rollout(self.root, "zzz", "/tmp/canary"))

    def test_the_cwd_fallback_matches_across_path_spellings(self):
        # Codex records the canonical cwd; the canary passed the symlinked spelling.
        real = tempfile.mkdtemp()
        link = os.path.join(tempfile.mkdtemp(), "scratch")
        os.symlink(real, link)
        wanted = self.write("rollout-2026-07-29T02-00-00-ccc.jsonl", real)
        self.assertEqual(canary.find_rollout(self.root, None, link), wanted)

    def test_await_rollout_gives_up_at_the_deadline(self):
        deadline = canary.Deadline(0, clock=lambda: 0)
        self.assertIsNone(canary.await_rollout(self.root, "zzz", "/tmp/canary", deadline))

    def test_await_rollout_returns_a_rollout_that_is_already_there(self):
        wanted = self.write("rollout-2026-07-29T02-00-00-ddd.jsonl", "/tmp/canary")
        deadline = canary.Deadline(30, clock=lambda: 0)
        self.assertEqual(canary.await_rollout(self.root, "ddd", "/tmp/canary", deadline), wanted)


class ClassificationTests(unittest.TestCase):
    def verdict(self, **overrides):
        state = {"cli_ok": True, "rollout_ok": True, "pane_appeared": False,
                 "artifact_ok": False, "native_spawns": 0, "instructions_empty": None,
                 "blocked_cannot": False}
        state.update(overrides)
        return canary.classify(**state)

    def test_a_blocked_launcher_is_cannot_not_a_choice(self):
        # The 05:26 post-injection run: contract obeyed, launcher denied by the sandbox.
        self.assertEqual(self.verdict(blocked_cannot=True), (canary.FAIL, "blocked-cannot"))
        self.assertEqual(self.verdict(), (canary.FAIL, "no-delegation"))

    def test_blocked_cannot_outranks_the_instruction_check(self):
        self.assertEqual(
            self.verdict(blocked_cannot=True, instructions_empty=True),
            (canary.FAIL, "blocked-cannot"))

    def test_work_that_landed_invisibly_stays_no_pane_even_with_a_denial(self):
        # A denied `herdr status` probe does not excuse an artifact written with no pane.
        self.assertEqual(
            self.verdict(blocked_cannot=True, artifact_ok=True), (canary.FAIL, "no-pane"))
        self.assertEqual(
            self.verdict(blocked_cannot=True, native_spawns=2), (canary.FAIL, "no-pane"))

    def test_a_pane_that_appeared_is_never_blocked_cannot(self):
        self.assertEqual(
            self.verdict(blocked_cannot=True, pane_appeared=True), (canary.FAIL, "no-artifact"))

    def test_a_missing_rollout_is_never_a_behavioural_reason(self):
        # The 2026-07-29T05:04 run: no rollout, no artifact, no pane — the prompt never
        # reached the model, which `no-delegation` would have misreported as a choice.
        self.assertEqual(self.verdict(rollout_ok=False), (canary.FAIL, "delivery-failed"))

    def test_a_missing_rollout_with_observed_work_is_an_observation_failure(self):
        self.assertEqual(
            self.verdict(rollout_ok=False, artifact_ok=True), (canary.FAIL, "observation-failed"))
        self.assertEqual(
            self.verdict(rollout_ok=False, pane_appeared=True),
            (canary.FAIL, "observation-failed"))

    def test_nothing_passes_without_the_rollout_to_verify_it(self):
        self.assertEqual(
            self.verdict(rollout_ok=False, pane_appeared=True, artifact_ok=True),
            (canary.FAIL, "observation-failed"))

    def test_a_missing_rollout_outranks_the_instruction_check(self):
        self.assertEqual(
            self.verdict(rollout_ok=False, instructions_empty=True),
            (canary.FAIL, "delivery-failed"))

    def test_a_visible_worker_that_delivered_passes(self):
        self.assertEqual(self.verdict(pane_appeared=True, artifact_ok=True), (canary.PASS, "ok"))

    def test_cli_failure_outranks_everything(self):
        self.assertEqual(
            self.verdict(cli_ok=False, pane_appeared=True, artifact_ok=True),
            (canary.FAIL, "cli-unreachable"))

    def test_invisible_native_delegation_is_the_expected_baseline_red(self):
        self.assertEqual(
            self.verdict(artifact_ok=True, native_spawns=17), (canary.FAIL, "no-pane"))

    def test_work_landing_with_no_pane_is_no_pane_even_without_a_rollout_count(self):
        self.assertEqual(self.verdict(artifact_ok=True), (canary.FAIL, "no-pane"))

    def test_a_visible_pane_that_never_delivered_is_no_artifact(self):
        self.assertEqual(self.verdict(pane_appeared=True), (canary.FAIL, "no-artifact"))

    def test_pane_plus_artifact_plus_native_spawns_is_not_a_pass(self):
        self.assertEqual(
            self.verdict(pane_appeared=True, artifact_ok=True, native_spawns=1),
            (canary.FAIL, "native-spawn"))

    def test_no_instructions_only_when_the_channel_was_checked_and_null(self):
        self.assertEqual(self.verdict(instructions_empty=True), (canary.FAIL, "no-instructions"))
        self.assertEqual(self.verdict(instructions_empty=False), (canary.FAIL, "no-delegation"))
        self.assertEqual(self.verdict(), (canary.FAIL, "no-delegation"))

    def test_a_null_instruction_channel_outranks_the_visibility_reasons(self):
        self.assertEqual(
            self.verdict(instructions_empty=True, artifact_ok=True, native_spawns=3),
            (canary.FAIL, "no-instructions"))
        self.assertEqual(
            self.verdict(instructions_empty=True, pane_appeared=True),
            (canary.FAIL, "no-instructions"))

    def test_a_delivered_run_still_passes_with_the_channel_checked(self):
        self.assertEqual(
            self.verdict(pane_appeared=True, artifact_ok=True, instructions_empty=True),
            (canary.PASS, "ok"))


class Completed:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def pane_list(*entries):
    return json.dumps({"result": {"type": "pane_list",
                                  "panes": [{"pane_id": p, "cwd": c} for p, c in entries]}})


def agent_list(*entries):
    return json.dumps({"result": {"agents": [
        {"pane_id": p, "agent_session": {"value": s}, "cwd": c} for p, s, c in entries]}})


def reply(item):
    return item if isinstance(item, Completed) else Completed(item)


class FakeHerdr:
    """Stands in for the `run` helper, replaying scripted `list` output per poll.

    The last scripted reply repeats, so a watch may poll more times than the script names.
    """

    def __init__(self, panes, agents=None, prompt=None):
        self.panes = [reply(item) for item in panes]
        self.agents = [reply(item) for item in (agents or [])]
        self.prompt = prompt or (lambda command: Completed())
        self.commands = []

    def _next(self, replies):
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def __call__(self, command):
        self.commands.append(command)
        if command[1:3] == ["pane", "list"]:
            return self._next(self.panes)
        if command[1:3] == ["agent", "list"]:
            return self._next(self.agents) if self.agents else Completed(agent_list())
        return self.prompt(command)


class FakeRun(unittest.TestCase):
    def patch(self, fake):
        original = canary.run
        canary.run = fake
        self.addCleanup(lambda: setattr(canary, "run", original))
        return fake


class SightingsTests(unittest.TestCase):
    def test_the_first_sample_is_the_baseline(self):
        sightings = canary.Sightings(SCRATCH)
        sightings.observe({"wK:p1": SCRATCH})
        self.assertEqual(sightings.appearances(), [])

    def test_a_worker_that_opens_and_closes_mid_turn_still_counts(self):
        sightings = canary.Sightings(SCRATCH)
        sightings.observe({"wK:p1": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH, "wK:p9": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH})
        self.assertEqual(sightings.appearances(), ["wK:p9"])

    def test_appearances_are_a_union_and_deduped(self):
        sightings = canary.Sightings(SCRATCH)
        sightings.observe({"wK:p1": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH, "wK:p2": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH, "wK:p2": SCRATCH, "wK:p3": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH, "wK:p2": SCRATCH})
        self.assertEqual(sightings.appearances(), ["wK:p2", "wK:p3"])

    def test_a_worker_living_entirely_between_two_samples_is_the_accepted_blind_spot(self):
        """Documents the residual: unseen is unseen, whatever the interval.

        Accepted because a contract-compliant worker (pane split, agent start, herdr
        readiness detection, a prompted turn) cannot complete inside one 500ms interval,
        so this cannot produce a false RED against a compliant implementation.
        """
        sightings = canary.Sightings(SCRATCH)
        sightings.observe({"wK:p1": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH})
        self.assertEqual(sightings.appearances(), [])

    def test_the_run_window_polls_faster_than_a_worker_can_be_born_and_die(self):
        self.assertLessEqual(canary.OBSERVE_INTERVAL_SECONDS, 0.5)

    def test_newcomers_outside_the_scratch_dir_are_never_folded_in(self):
        sightings = canary.Sightings(SCRATCH)
        sightings.observe({"wK:p1": SCRATCH})
        sightings.observe({"wK:p1": SCRATCH, "wK:p7": "/Users/me"})
        self.assertEqual(sightings.appearances(), [])


class WatchTests(FakeRun):
    def test_polling_spans_the_blocking_prompt(self):
        fake = self.patch(FakeHerdr(
            panes=[
                pane_list(("wK:p1", SCRATCH)),
                pane_list(("wK:p1", SCRATCH), ("wK:p9", SCRATCH)),
                pane_list(("wK:p1", SCRATCH)),
            ],
            agents=[
                agent_list(("wK:p1", "root", SCRATCH)),
                agent_list(("wK:p1", "root", SCRATCH), ("wK:p9", "worker", SCRATCH)),
                agent_list(("wK:p1", "root", SCRATCH)),
            ],
        ))
        watch = canary.Watch("herdr", SCRATCH, interval=0.01)
        watch.start()
        time.sleep(0.05)
        watch.stop()
        self.assertEqual(watch.panes.appearances(), ["wK:p9"])
        self.assertEqual(watch.agents.appearances(), ["wK:p9:worker"])
        polls = [c for c in fake.commands if c[1:3] == ["pane", "list"]]
        self.assertGreaterEqual(len(polls), 3)  # baseline, mid-turn, final

    def test_a_failed_list_call_mid_window_costs_one_sample_not_the_watch(self):
        self.patch(FakeHerdr(panes=[
            pane_list(("wK:p1", SCRATCH)),
            Completed("herdr: socket busy", returncode=1),
            pane_list(("wK:p1", SCRATCH), ("wK:p9", SCRATCH)),
        ]))
        watch = canary.Watch("herdr", SCRATCH, interval=0.01)
        watch.start()
        time.sleep(0.05)
        watch.stop()
        self.assertEqual(watch.panes.appearances(), ["wK:p9"])

    def test_a_failed_baseline_aborts_rather_than_absorbing_the_worker(self):
        fake = self.patch(FakeHerdr(panes=[
            Completed("herdr: socket busy", returncode=1),
            pane_list(("wK:p1", SCRATCH), ("wK:p9", SCRATCH)),
        ]))
        watch = canary.Watch("herdr", SCRATCH, interval=0.01)
        with self.assertRaises(canary.CanaryError):
            watch.start()
        self.assertIsNone(watch.panes.baseline)
        self.assertEqual([c for c in fake.commands if c[1:3] == ["pane", "list"]],
                         [["herdr", "pane", "list"]])

    def test_a_failed_agent_baseline_also_aborts(self):
        self.patch(FakeHerdr(
            panes=[pane_list(("wK:p1", SCRATCH))],
            agents=[Completed("herdr: socket busy", returncode=1)],
        ))
        watch = canary.Watch("herdr", SCRATCH, interval=0.01)
        with self.assertRaises(canary.CanaryError):
            watch.start()
        self.assertIsNone(watch.agents.baseline)


class DeadlineTests(unittest.TestCase):
    def clock(self, values):
        ticks = iter(values)
        return lambda: next(ticks)

    def test_remaining_counts_down_and_floors_at_zero(self):
        deadline = canary.Deadline(10, clock=self.clock([100, 104, 115]))
        self.assertEqual(deadline.remaining(), 6)
        self.assertEqual(deadline.remaining(), 0)

    def test_milliseconds_never_returns_a_non_positive_timeout(self):
        deadline = canary.Deadline(0, clock=self.clock([0, 5, 5]))
        self.assertTrue(deadline.expired())
        self.assertEqual(deadline.milliseconds(), 1)

    def test_milliseconds_are_capped_by_the_slice_but_not_raised(self):
        deadline = canary.Deadline(300, clock=self.clock([0, 0, 0]))
        self.assertEqual(deadline.milliseconds(canary.WAIT_SLICE_MS), canary.WAIT_SLICE_MS)
        self.assertEqual(deadline.milliseconds(), 300000)


class SubmissionTests(FakeRun):
    def test_a_clean_prompt_is_delivered(self):
        self.assertEqual(canary.submission_outcome(0, "", False), (canary.DELIVERED, ""))

    def test_a_stall_is_a_delivery_problem_even_when_the_budget_is_gone(self):
        status, message = canary.submission_outcome(1, "agent_prompt_stalled", True)
        self.assertEqual(status, canary.STALLED)
        self.assertIn("stalled", message)

    def test_an_exhausted_budget_is_expired_not_failed(self):
        self.assertEqual(
            canary.submission_outcome(1, "agent_wait_timeout", False)[0], canary.EXPIRED)
        self.assertEqual(canary.submission_outcome(1, "some error", True)[0], canary.EXPIRED)

    def test_any_other_error_is_a_failure(self):
        self.assertEqual(canary.submission_outcome(1, "unknown agent", False)[0], canary.FAILED)

    def test_submit_spends_the_remaining_global_budget_not_a_fixed_one(self):
        fake = self.patch(FakeHerdr(panes=[pane_list()]))
        deadline = canary.Deadline(7, clock=lambda: 0)
        canary.submit("herdr", "canary", "task", deadline)
        self.assertEqual(fake.commands[0][-2:], ["--timeout", "7000"])


class DeliveryTests(FakeRun):
    def herdr(self, prompts, screen=""):
        replies = list(prompts)

        def prompt(command):
            if command[1:3] == ["agent", "prompt"]:
                return replies.pop(0)
            if command[1:3] == ["pane", "read"]:
                return Completed(screen)
            return Completed()

        return self.patch(FakeHerdr(panes=[pane_list()], prompt=prompt))

    def deliver(self, seconds=60):
        return canary.deliver("herdr", "canary", "wK:p1", "task",
                              canary.Deadline(seconds, clock=lambda: 0))

    def test_a_delivered_prompt_needs_no_recovery(self):
        fake = self.herdr([Completed()])
        self.assertEqual(self.deliver()[0], canary.DELIVERED)
        self.assertNotIn(["herdr", "agent", "send-keys", "canary", "enter"], fake.commands)

    def test_a_stall_on_a_trust_dialog_sends_exactly_one_key_then_resends(self):
        fake = self.herdr(
            [Completed("agent_prompt_stalled", returncode=1), Completed()],
            screen="Do you trust the files in /tmp/herdr-canary/x?")
        status, note = self.deliver()
        self.assertEqual(status, canary.DELIVERED)
        self.assertIn("trust dialog", note)
        keystrokes = [c for c in fake.commands if c[1:3] == ["agent", "send-keys"]]
        self.assertEqual(keystrokes, [["herdr", "agent", "send-keys", "canary", "enter"]])

    def test_a_stall_without_a_dialog_is_never_answered_blindly(self):
        fake = self.herdr([Completed("agent_prompt_stalled", returncode=1)],
                          screen="â  Working (30s)")
        with self.assertRaises(canary.CanaryError):
            self.deliver()
        self.assertEqual([c for c in fake.commands if c[1:3] == ["agent", "send-keys"]], [])

    def test_an_expired_budget_is_reported_rather_than_raised(self):
        self.herdr([Completed("agent_wait_timeout", returncode=1)])
        self.assertEqual(self.deliver()[0], canary.EXPIRED)

    def test_a_reported_delivery_onto_a_trust_dialog_is_not_believed(self):
        # The 05:04 failure: `--wait` saw the dialog react, called it delivered, and the
        # prompt was consumed by the dialog instead of reaching the model.
        fake = self.herdr([Completed(), Completed()],
                          screen="Do you trust the files in /private/tmp/herdr-canary/x?")
        status, note = self.deliver()
        self.assertEqual(status, canary.DELIVERED)
        self.assertIn("still on screen after a reported delivery", note)
        self.assertEqual([c for c in fake.commands if c[1:3] == ["agent", "send-keys"]],
                         [["herdr", "agent", "send-keys", "canary", "enter"]])
        self.assertEqual(len([c for c in fake.commands if c[1:3] == ["agent", "prompt"]]), 2)


class AgentNameTests(unittest.TestCase):
    STAMP = "20260729T045653"

    def test_the_generated_names_satisfy_herdrs_pattern(self):
        for prefix in ("canary-codex", "canary-probe"):
            name = canary.agent_name(prefix, self.STAMP)
            self.assertRegex(name, canary.AGENT_NAME_PATTERN)

    def test_the_iso_timestamp_separator_is_lowercased_not_dropped(self):
        self.assertEqual(canary.agent_name("canary-codex", self.STAMP),
                         "canary-codex-20260729t045653")

    def test_the_unique_timestamp_survives_the_length_cap(self):
        name = canary.agent_name("a-very-long-canary-prefix-indeed", self.STAMP)
        self.assertLessEqual(len(name), 32)
        self.assertTrue(name.endswith("20260729t045653"))
        self.assertRegex(name, canary.AGENT_NAME_PATTERN)

    def test_illegal_characters_are_replaced(self):
        self.assertRegex(canary.agent_name("Canary Probe!", "2026/07/29 04:56"),
                         canary.AGENT_NAME_PATTERN)


class PhaseReasonTests(unittest.TestCase):
    def test_only_a_boot_failure_is_cli_unreachable(self):
        self.assertEqual(canary.failure_reason(canary.BOOT), "cli-unreachable")
        self.assertEqual(canary.failure_reason(canary.DELIVERY), "delivery-failed")
        self.assertEqual(canary.failure_reason(canary.OBSERVATION), "observation-failed")


class TrustSeedingTests(unittest.TestCase):
    CONFIG = (
        'model = "gpt-5.6-sol"\n\n'
        '[projects]\n'
        '  [projects."/Users/me"]\n'
        '    trust_level = "trusted"\n'
    )

    def test_appends_a_trusted_subtable_for_a_new_scratch_dir(self):
        updated = canary.trust_entry(self.CONFIG, SCRATCH)
        self.assertTrue(canary.trusted(updated, SCRATCH))
        self.assertTrue(canary.trusted(updated, "/Users/me"))

    def test_an_already_trusted_project_is_left_alone(self):
        self.assertIsNone(canary.trust_entry(self.CONFIG, "/Users/me"))

    def test_a_config_without_a_projects_table_still_works(self):
        updated = canary.trust_entry('model = "gpt-5.6-sol"', SCRATCH)
        self.assertTrue(canary.trusted(updated, SCRATCH))

    def test_malformed_config_raises_rather_than_being_rewritten(self):
        with self.assertRaises(canary.tomllib.TOMLDecodeError):
            canary.trust_entry("[projects\n", SCRATCH)

    def test_seed_trust_reports_and_leaves_an_unreadable_config_alone(self):
        missing = os.path.join(tempfile.mkdtemp(), "config.toml")
        os.environ["CODEX_CONFIG_PATH"] = missing
        try:
            self.assertIn("unreadable", canary.seed_trust(SCRATCH))
        finally:
            del os.environ["CODEX_CONFIG_PATH"]
        self.assertFalse(os.path.exists(missing))

    def test_every_path_spelling_is_seeded(self):
        real = tempfile.mkdtemp()
        link = os.path.join(tempfile.mkdtemp(), "scratch")
        os.symlink(real, link)
        self.assertEqual(canary.trust_spellings(link), [link, os.path.realpath(real)])
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.CONFIG)
        os.environ["CODEX_CONFIG_PATH"] = path
        try:
            self.assertIn("2 spellings", canary.seed_trust(link))
            with open(path, encoding="utf-8") as handle:
                written = handle.read()
        finally:
            del os.environ["CODEX_CONFIG_PATH"]
        self.assertTrue(canary.trusted(written, link))
        self.assertTrue(canary.trusted(written, os.path.realpath(real)))

    def test_seed_trust_rewrites_the_config_in_place(self):
        path = os.path.join(tempfile.mkdtemp(), "config.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.CONFIG)
        os.environ["CODEX_CONFIG_PATH"] = path
        try:
            self.assertIn("pre-trusted", canary.seed_trust(SCRATCH))
            with open(path, encoding="utf-8") as handle:
                self.assertTrue(canary.trusted(handle.read(), SCRATCH))
            self.assertEqual(canary.seed_trust(SCRATCH), "")
        finally:
            del os.environ["CODEX_CONFIG_PATH"]


class TrustDialogTests(unittest.TestCase):
    def test_detects_the_codex_first_run_prompt(self):
        self.assertTrue(canary.is_trust_dialog(
            "  You are running Codex in /tmp/herdr-canary/x\n  Do you trust the files here?"))
        self.assertTrue(canary.is_trust_dialog("Allow Codex to work in this folder?"))

    def test_an_ordinary_screen_is_not_a_dialog(self):
        self.assertFalse(canary.is_trust_dialog("â  Working (12s ago)"))
        self.assertFalse(canary.is_trust_dialog(""))


STATUS_OUTPUT = (
    "client:\n  version: 0.7.5\n  protocol: 17\n\n"
    "server:\n  status: running\n  protocol: 17\n  socket: /Users/me/herdr.sock\n"
)
PANE_LIST_OUTPUT = json.dumps({"id": "cli:pane:list",
                               "result": {"panes": [{"pane_id": "wK:p1"}], "type": "pane_list"}})


class ProbeClassificationTests(unittest.TestCase):
    def test_both_halves_of_the_probe_make_it_reachable(self):
        self.assertEqual(
            canary.classify_probe(STATUS_OUTPUT + PANE_LIST_OUTPUT), canary.REACHABLE)

    def test_a_pane_list_alone_is_not_reachable(self):
        self.assertEqual(canary.classify_probe(PANE_LIST_OUTPUT), canary.BLOCKED)

    def test_a_status_report_alone_is_not_reachable(self):
        self.assertEqual(canary.classify_probe(STATUS_OUTPUT), canary.BLOCKED)

    def test_text_that_merely_mentions_the_markers_is_not_an_envelope(self):
        text = STATUS_OUTPUT + 'herdr: refused to emit {"result": "panes"} for this sandbox'
        self.assertEqual(canary.classify_probe(text), canary.BLOCKED)

    def test_an_envelope_surrounded_by_noise_still_parses(self):
        text = f'warning: slow socket\n{STATUS_OUTPUT}\n{PANE_LIST_OUTPUT}\ndone\n'
        self.assertEqual(canary.classify_probe(text), canary.REACHABLE)

    def test_a_sandbox_denial_is_blocked(self):
        self.assertEqual(
            canary.classify_probe("herdr: connect /tmp/herdr.sock: operation not permitted"),
            canary.BLOCKED)

    def test_a_missing_binary_is_blocked(self):
        self.assertEqual(canary.classify_probe("zsh: command not found: herdr"), canary.BLOCKED)

    def test_an_unwritten_probe_is_blocked(self):
        self.assertEqual(canary.classify_probe(""), canary.BLOCKED)
        self.assertEqual(canary.classify_probe(None), canary.BLOCKED)

    def test_the_probe_command_short_circuits(self):
        self.assertIn("herdr status && herdr pane list",
                      canary.PROBE_TEMPLATE.format(probe="/tmp/x/probe.txt"))


if __name__ == "__main__":
    unittest.main()


class RegistryModeTests(FakeRun):
    """`--registry-root` provisions the throwaway root THROUGH the registry.

    Without it a canary run cannot exercise the registry at all, so HL-046's Observable —
    post-injection PASS with the root still sandboxed — has nothing to observe.
    """

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.registry = os.path.join(self.base, "registry")
        self.scratch = os.path.join(self.base, "scratch", "canary-1")
        os.makedirs(os.path.join(self.registry, "panes"))
        os.makedirs(self.scratch)
        self.write_manifest([os.path.join(self.base, "scratch")])

    def write_manifest(self, allow_cwd):
        with open(os.path.join(self.registry, "registry.json"), "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "root_id": "canary", "session": "0" * 16,
                       "allow_cwd": allow_cwd}, handle)

    def write_pane(self, key, pane_id, cwd, agent_name="rcanary-w1"):
        with open(os.path.join(self.registry, "panes", f"{key}.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"idempotency_key": key, "pane_id": pane_id, "cwd": cwd,
                       "agent_name": agent_name,
                       "provenance": {"request_digest": "d" * 64}}, handle)

    # -- allowlist pre-check

    def test_a_registry_that_allows_the_scratch_dir_passes(self):
        canary.check_registry_allows(self.registry, self.scratch)

    def test_a_registry_that_would_refuse_the_worker_fails_before_the_run(self):
        self.write_manifest(["/somewhere/else"])
        with self.assertRaises(canary.CanaryError) as caught:
            canary.check_registry_allows(self.registry, self.scratch)
        # Otherwise the refusal arrives mid-run and reads as the root declining to delegate.
        self.assertIn("does not allow", str(caught.exception))
        self.assertIn("--allow-cwd", str(caught.exception))

    def test_a_missing_registry_root_is_named_plainly(self):
        with self.assertRaises(canary.CanaryError) as caught:
            canary.check_registry_allows(os.path.join(self.base, "absent"), self.scratch)
        self.assertIn("is unreadable", str(caught.exception))

    # -- launch path

    def test_launch_asks_the_registry_for_a_codex_root_in_the_scratch_dir(self):
        seen = []

        def fake(command):
            seen.append(command)
            return Completed(json.dumps({"pane": "wT:p9", "cap_id": "cap-" + "a" * 16}))

        self.patch(fake)
        pane, capability = canary.launch_via_registry(self.registry, None, self.scratch, "c1")
        self.assertEqual((pane, capability), ("wT:p9", "cap-" + "a" * 16))
        command = seen[0]
        self.assertIn("launch", command)
        self.assertEqual(command[command.index("launch") + 1:command.index("launch") + 3],
                         ["codex", "c1"])
        self.assertEqual(command[command.index("--root") + 1], self.registry)
        self.assertEqual(command[command.index("--cwd") + 1], self.scratch)
        self.assertEqual(command[command.index("--") + 1:],
                         ["--sandbox", "workspace-write", "--ask-for-approval", "never"])

    def test_launch_passes_a_store_when_one_is_given(self):
        seen = []

        def fake(command):
            seen.append(command)
            return Completed(json.dumps({"pane": "wT:p9"}))

        self.patch(fake)
        canary.launch_via_registry(self.registry, "/tmp/store", self.scratch, "c1")
        self.assertEqual(seen[0][seen[0].index("--store") + 1], "/tmp/store")

    def test_a_failed_launch_surfaces_the_registry_error(self):
        self.patch(lambda command: Completed("", 2, "herdr-registry: capability_expired"))
        with self.assertRaises(canary.CanaryError) as caught:
            canary.launch_via_registry(self.registry, None, self.scratch, "c1")
        self.assertIn("capability_expired", str(caught.exception))

    def test_a_launch_without_a_pane_id_is_a_boot_failure(self):
        self.patch(lambda command: Completed(json.dumps({"cap_id": "cap-x"})))
        with self.assertRaises(canary.CanaryError):
            canary.launch_via_registry(self.registry, None, self.scratch, "c1")

    # -- boot routing

    def test_boot_without_a_registry_root_still_splits_and_starts_directly(self):
        calls = []

        def fake(command):
            calls.append(command[1:3])
            if command[1:3] == ["pane", "split"]:
                return Completed(json.dumps({"result": {"pane": {"pane_id": "wK:p2"}}}))
            if command[1:3] == ["pane", "process-info"]:
                return Completed("codex")
            return Completed()

        self.patch(fake)
        pane, capability = canary.boot("herdr", self.scratch, "c1")
        self.assertEqual(pane, "wK:p2")
        self.assertIsNone(capability)
        self.assertIn(["pane", "split"], calls)
        self.assertIn(["agent", "start"], calls)

    def test_boot_with_a_registry_root_never_splits_or_starts_directly(self):
        calls = []

        def fake(command):
            calls.append(command[1:3])
            if command[1:3] == ["pane", "process-info"]:
                return Completed("codex")
            return Completed(json.dumps({"pane": "wT:p9", "cap_id": "cap-" + "b" * 16}))

        self.patch(fake)
        pane, capability = canary.boot("herdr", self.scratch, "c1", self.registry)
        self.assertEqual(pane, "wT:p9")
        self.assertEqual(capability, "cap-" + "b" * 16)
        self.assertNotIn(["pane", "split"], calls)
        self.assertNotIn(["agent", "start"], calls)

    def test_a_registry_boot_with_a_dead_occupant_closes_the_pane(self):
        closed = []

        def fake(command):
            if command[1:3] == ["pane", "process-info"]:
                return Completed("bash")
            if command[1:3] == ["pane", "close"]:
                closed.append(command[3])
            return Completed(json.dumps({"pane": "wT:p9"}))

        self.patch(fake)
        with self.assertRaises(canary.CanaryError):
            canary.boot("herdr", self.scratch, "c1", self.registry)
        self.assertEqual(closed, ["wT:p9"])

    # -- provenance

    def test_registry_panes_reports_the_request_that_produced_each_worker(self):
        self.chain_result(True)
        self.write_pane("w1", "wT:p10", self.scratch)
        found = canary.registry_panes(self.registry, self.scratch)
        self.assertEqual(found, [{"key": "w1", "pane_id": "wT:p10",
                                  "agent_name": "rcanary-w1",
                                  "request_digest": "d" * 64,
                                  "chain_intact": True}])

    def test_registry_panes_ignores_workers_outside_the_scratch_dir(self):
        self.chain_result(True)
        self.write_pane("elsewhere", "wT:p11", os.path.join(self.base, "other"))
        self.assertEqual(canary.registry_panes(self.registry, self.scratch), [])

    def test_registry_panes_tolerates_a_registry_with_no_panes_yet(self):
        self.assertEqual(canary.registry_panes(os.path.join(self.base, "absent"),
                                               self.scratch), [])

    # -- scoring: a registry worker must count exactly as a direct one does

    def chain_result(self, intact):
        def fake(command):
            if "chain" in command:
                return Completed("{}", 0 if intact else 1)
            return Completed()
        return self.patch(fake)

    def test_a_symlinked_scratch_root_still_contains_its_panes(self):
        # The acceptance run scored pane_appeared=false because herdr reported the pane's
        # cwd as /private/tmp/... while the scratch string said /tmp/... — same directory,
        # different spelling, and a bare string compare said "elsewhere".
        real = os.path.realpath(self.scratch)
        self.assertNotEqual(real, self.scratch) if real != self.scratch else None
        self.assertTrue(canary.under(real, self.scratch))
        self.assertTrue(canary.under(self.scratch, real))

    def test_tmp_and_private_tmp_are_the_same_directory(self):
        if not os.path.isdir("/private/tmp"):
            self.skipTest("no /private/tmp on this platform")
        self.assertTrue(canary.under("/private/tmp/herdr-canary/x", "/tmp/herdr-canary/x"))

    def test_an_attested_registry_pane_counts_as_a_pane(self):
        self.chain_result(True)
        self.write_pane("w1", "wT:p10", self.scratch)
        found = canary.registry_panes(self.registry, self.scratch)
        self.assertTrue(found[0]["chain_intact"])
        self.assertTrue(canary.registry_pane_appeared(found))

    def test_a_registry_pane_with_a_broken_chain_does_not_count(self):
        self.chain_result(False)
        self.write_pane("w1", "wT:p10", self.scratch)
        found = canary.registry_panes(self.registry, self.scratch)
        self.assertFalse(found[0]["chain_intact"], "the break is reported, not hidden")
        self.assertFalse(canary.registry_pane_appeared(found))

    def test_a_registry_pane_record_keyed_the_new_way_is_read(self):
        self.chain_result(True)
        path = os.path.join(self.registry, "panes", "w2.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"key": "w2", "pane_id": "wT:p11", "cwd": self.scratch,
                       "agent_name": "rr-w2", "provenance": {"request_digest": "e" * 64}},
                      handle)
        found = canary.registry_panes(self.registry, self.scratch)
        self.assertEqual([entry["key"] for entry in found], ["w2"])

    def test_the_acceptance_run_shape_passes(self):
        """Exactly 20260729T144516: registry pane, intact chain, artifact, no native spawns."""
        verdict, reason = canary.classify(
            cli_ok=True, rollout_ok=True,
            pane_appeared=canary.registry_pane_appeared([
                {"key": "artifact-write-3d9c8244856b97d5", "pane_id": "wK:pT",
                 "agent_name": "rrun1-artifact-writer", "chain_intact": True},
            ]),
            artifact_ok=True, native_spawns=0, instructions_empty=False,
            blocked_cannot=True,
        )
        self.assertEqual((verdict, reason), (canary.PASS, "ok"))

    def test_the_same_shape_with_a_broken_chain_does_not_pass(self):
        verdict, reason = canary.classify(
            cli_ok=True, rollout_ok=True,
            pane_appeared=canary.registry_pane_appeared([
                {"key": "k", "pane_id": "wK:pT", "chain_intact": False},
            ]),
            artifact_ok=True, native_spawns=0, instructions_empty=False,
            blocked_cannot=True,
        )
        self.assertEqual(verdict, canary.FAIL)
        self.assertEqual(reason, "no-pane", "an unaccountable pane is not delegation")

    def test_a_registry_pane_plus_a_native_spawn_still_fails(self):
        verdict, reason = canary.classify(
            cli_ok=True, rollout_ok=True,
            pane_appeared=canary.registry_pane_appeared([{"key": "k", "chain_intact": True}]),
            artifact_ok=True, native_spawns=2, instructions_empty=False,
        )
        self.assertEqual((verdict, reason), (canary.FAIL, "native-spawn"))

    # -- teardown

    def test_a_cancel_goes_through_the_registrys_control_channel(self):
        path = canary.cancel_registry_worker(self.registry, "w1")
        self.assertEqual(path, os.path.join(self.registry, "control", "w1", "1.json"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"action": "cancel"})
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".")]
        self.assertEqual(leftovers, [], "no temp left behind")

    def test_cancelling_twice_does_not_queue_a_second_message(self):
        canary.cancel_registry_worker(self.registry, "w1")
        canary.cancel_registry_worker(self.registry, "w1")
        directory = os.path.join(self.registry, "control", "w1")
        self.assertEqual(sorted(os.listdir(directory)), ["1.json"])

    def test_an_unconfirmed_cancel_is_reported_not_swallowed(self):
        outstanding = canary.release_registry_workers(
            self.registry, [{"key": "w1", "pane_id": "wT:p10"}], budget=0.0
        )
        self.assertEqual(outstanding, ["w1"])

    def test_a_confirmed_cancel_leaves_nothing_outstanding(self):
        directory = os.path.join(self.registry, "control", "w1")
        os.makedirs(directory)
        with open(os.path.join(directory, "1.result.json"), "w", encoding="utf-8") as handle:
            json.dump({"ok": True, "outcome": "cancel"}, handle)
        self.assertEqual(
            canary.release_registry_workers(self.registry, [{"key": "w1"}], budget=0.0), []
        )

    def test_the_canary_never_closes_a_pane_the_registry_created(self):
        source = pathlib.Path(SCRIPT_DIR / "herdr-codex-canary.py").read_text()
        run_body = source[source.index("def command_run("):source.index("def command_probe_sandbox(")]
        self.assertIn("borrowed = {entry.get(\"pane_id\") for entry in attested}", run_body)
        self.assertIn("pane not in borrowed", run_body)

    def test_the_probe_subcommand_is_untouched_by_registry_mode(self):
        # `probe-sandbox` is a separate diagnostic with no registry flags; a stray
        # `args.registry_root` there would raise AttributeError on every probe run.
        source = pathlib.Path(SCRIPT_DIR / "herdr-codex-canary.py").read_text()
        probe = source[source.index("def command_probe_sandbox("):]
        self.assertIn("pane, _ = boot(binary, scratch, name)", probe)
        self.assertNotIn("registry_root", probe)

    def test_a_registry_worker_is_sighted_exactly_like_a_direct_one(self):
        # The sighting rule is cwd-rooted, so it cannot tell the two paths apart — which is
        # the point: a registry worker must count as pane_appeared like any other.
        sightings = canary.Sightings(self.scratch)
        sightings.observe({"wK:p1": self.scratch})
        sightings.observe({"wK:p1": self.scratch, "wT:p10": self.scratch})
        self.assertEqual(sightings.appearances(), ["wT:p10"])
