#!/usr/bin/env python3

import importlib.util
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import unittest
from unittest.mock import patch


sys.dont_write_bytecode = True
SCRIPT_DIR = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


context = load("agent_host_context", "agent-host-context.py")

HERDR = {"HERDR_ENV": "1", "HERDR_PANE_ID": "wK:p1"}
WORKER = {"HERDR_AGENT_PANE": "1"}
REGISTRY = {
    "HERDR_REGISTRY_ROOT": "/tmp/herdr-registry/run-abc",
    "HERDR_REGISTRY_CAPABILITY": "cap-0123456789abcdef:" + "a" * 64,
}
HOST_KEYS = (
    "HERDR_ENV", "HERDR_PANE_ID",
    "HERDR_REGISTRY_ROOT", "HERDR_REGISTRY_CAPABILITY", "HERDR_REGISTRY_KEY",
    "HERDR_REGISTRY_RESULTS", "HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD",
)


class HostContextTest(unittest.TestCase):
    """The hook is the only channel a codex root reads, so its gating is load-bearing."""

    def emit(self, provider="codex", **environment):
        env = {key: value for key, value in os.environ.items() if key not in HOST_KEYS}
        env.update(environment)
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "agent-host-context.py"), "--provider", provider],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def codex_context(self, **environment):
        payload = json.loads(self.emit(**environment))
        return payload.get("hookSpecificOutput", {}).get("additionalContext", "")

    # -- gating

    def test_a_codex_session_outside_any_host_gets_exactly_nothing(self):
        self.assertEqual(json.loads(self.emit()), {})

    def test_registry_environment_alone_does_not_manufacture_a_host(self):
        # A stray variable must not turn a non-herdr codex session into a contracted one.
        self.assertEqual(json.loads(self.emit(**REGISTRY)), {})

    def test_a_half_configured_registry_is_ignored(self):
        for partial in ({"HERDR_REGISTRY_ROOT": REGISTRY["HERDR_REGISTRY_ROOT"]},
                        {"HERDR_REGISTRY_CAPABILITY": REGISTRY["HERDR_REGISTRY_CAPABILITY"]}):
            body = self.codex_context(**dict(HERDR, **partial))
            self.assertNotIn("herdr-registry.py request", body)
            self.assertIn("spawn_agent", body)

    def test_a_herdr_codex_session_without_a_registry_keeps_its_old_contract(self):
        body = self.codex_context(**HERDR)
        self.assertIn("[agent-host: herdr]", body)
        self.assertIn("spawn_agent", body)
        self.assertIn("agent-teammate.py", body)
        self.assertNotIn("herdr-registry.py request", body)

    def test_each_hosted_provider_receives_only_its_runtime_adapter(self):
        expected = {
            "claude": "~/.claude/skills/orchestrate-claude/SKILL.md",
            "codex": "~/.agents/skills/orchestrate-codex/SKILL.md",
            "agy": "~/.gemini/config/skills/orchestrate-agy/SKILL.md",
        }
        for provider, adapter in expected.items():
            with self.subTest(provider=provider):
                emitted = self.emit(provider=provider, **HERDR)
                body = emitted if provider == "claude" else json.dumps(
                    json.loads(emitted)
                )
                self.assertIn(f"[agent-runtime: {provider}]", body)
                self.assertIn(adapter, body)
                for other in set(expected.values()) - {adapter}:
                    self.assertNotIn(other, body)

    def test_host_contract_precedes_runtime_binding(self):
        body = self.codex_context(**HERDR)
        self.assertLess(body.index("[agent-host: herdr]"),
                        body.index("[agent-runtime: codex]"))

    # -- the registry clause

    def test_a_registry_session_is_told_how_to_delegate(self):
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertIn("herdr-registry.py request", body)
        self.assertIn(REGISTRY["HERDR_REGISTRY_ROOT"], body)
        self.assertIn("--key", body)
        self.assertIn("--cwd", body)
        self.assertIn("--wait", body)

    def test_the_registry_clause_forbids_the_socket_path_outright(self):
        # Was "conditions agent-teammate on the socket". A provisioned session no longer
        # probes at all: its spawn path is already known.
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertIn("control socket", body)
        self.assertIn("Do not probe", body)
        self.assertIn("no `herdr status`", body)

    def test_a_provisioned_session_is_never_told_to_probe_the_socket(self):
        """HL-051's Observable forbids PermissionDenied events on spawn OR status calls.
        A `herdr status` probe from a sandboxed root IS such an event, and registry
        presence already proves the spawn path, so the probe branch must not reach it."""
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertNotIn("you are the unsandboxed driver", body)
        self.assertNotIn("Settle your spawn path ONCE", body)

    def test_a_session_without_a_drop_box_still_gets_the_probe(self):
        body = self.codex_context(**HERDR)
        self.assertIn("Settle your spawn path ONCE", body)
        self.assertIn("you are the unsandboxed driver", body)
        self.assertIn("You have no spawn path", body)

    def test_exactly_one_spawn_path_is_described_either_way(self):
        # The round-1 defect was two live paths in one contract; a root that reads
        # top-down must never find a second one to fall back to.
        probing = self.codex_context(**HERDR)
        provisioned = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertIn("agent-teammate.py", probing)
        self.assertNotIn("herdr-registry.py request", probing)
        self.assertIn("herdr-registry.py request", provisioned)
        self.assertNotIn("Spawn with `python3", provisioned)

    def test_the_no_native_spawn_clause_survives_the_registry_clause(self):
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertIn("`spawn_agent`, `wait_agent`", body)
        self.assertIn("not a delegation path here", body)

    def test_the_registry_clause_forbids_a_fallback_on_every_failure_code(self):
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        for code in ("0", "3", "4", "5"):
            self.assertIn(f"{code} means", body)
        self.assertIn("never substitute an in-process agent", body)

    def test_the_registry_clause_states_the_argv_and_name_limits(self):
        # A root that learns these from the contract asks for things that can be granted,
        # instead of discovering the allowlist through refusals.
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertIn("[a-z0-9][a-z0-9_-]{0,31}", body)
        self.assertIn("--model", body)
        self.assertIn("allowlist", body)

    def test_every_placeholder_is_concrete_or_explained(self):
        """The acceptance run failed on `--role <r>`: a placeholder the root could not fill
        from the contract, so it sent an empty one and the host refused it. A placeholder
        nobody can resolve from the text is a defect, not a formatting choice."""
        clause = context.CODEX_REGISTRY_DELEGATION.format(root="/tmp/reg")
        markers = ("one of", "matches", "for example", "|")
        for placeholder in sorted(set(re.findall(r"<[^>\s]+>", clause))):
            explained = [
                line for line in clause.splitlines()
                if placeholder in line and any(marker in line for marker in markers)
            ]
            self.assertTrue(
                explained,
                f"{placeholder} is never enumerated, patterned or exemplified in the clause",
            )

    def test_the_clause_no_longer_asks_for_a_role(self):
        # Smallest fix for the acceptance failure: role is audit metadata, so the contract
        # simply does not ask for it and the client defaults it.
        clause = context.CODEX_REGISTRY_DELEGATION
        self.assertNotIn("--role", clause)

    def test_the_command_the_clause_shows_parses_against_the_real_client(self):
        """Whatever the contract prints has to be a command the client actually accepts."""
        clause = context.CODEX_REGISTRY_DELEGATION.format(root="/tmp/reg")
        line = next(part for part in clause.splitlines() if "herdr-registry.py request" in part)
        shown = shlex.split(line.strip().strip("`"))
        argv = shown[shown.index("request") + 1:]
        filled = {
            "<family>": "codex",
            "<worker-name>": "reviewer-1",
            "<idempotency-key>": "review-auth-1",
        }
        argv = [filled.get(token, token) for token in argv]
        self.assertNotIn("<", " ".join(argv), "every placeholder must be fillable")
        parser_check = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "request", *argv[:2],
             "--key", "k1", "--cwd", "/tmp", "--wait", "1"],
            capture_output=True, text=True,
            env={key: value for key, value in os.environ.items() if key not in HOST_KEYS},
        )
        # No registry configured here, so it must fail on the missing root — never on argv.
        self.assertIn("HERDR_REGISTRY_ROOT", parser_check.stderr)
        self.assertNotIn("unrecognized arguments", parser_check.stderr)

    def test_the_clause_carries_no_capability_token(self):
        body = self.codex_context(**dict(HERDR, **REGISTRY))
        self.assertNotIn("a" * 64, body, "the contract names the root, never the secret")

    # -- worker panes

    def test_a_herdr_worker_is_given_the_worker_contract_instead(self):
        body = self.emit(provider="claude", **dict(HERDR, **WORKER))
        self.assertIn("[agent-host: herdr — worker]", body)
        self.assertNotIn("[agent-host: herdr]", body)
        self.assertIn("Never spawn visible panes", body)
        self.assertIn("[agent-runtime: claude]", body)

    def test_either_marker_alone_makes_a_session_a_worker(self):
        for marker in ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD"):
            with self.subTest(marker=marker):
                body = self.emit(provider="claude", **dict(HERDR, **{marker: "1"}))
                self.assertIn("[agent-host: herdr — worker]", body)

    def test_a_codex_worker_is_told_to_remain_a_leaf(self):
        body = self.codex_context(**dict(HERDR, **WORKER))
        self.assertIn("[agent-host: herdr — worker]", body)
        self.assertIn("Remain a leaf", body)
        self.assertNotIn("spawn_agent", body)

    def test_a_codex_worker_is_given_no_spawn_path_at_all(self):
        """Every spawn-path clause is orchestrator text. A worker that reads one has a
        visible-pane path described to it that both the helper and the guard refuse, which
        is the contradiction the three self-orchestrating leaves resolved by spawning."""
        body = self.codex_context(**dict(HERDR, **WORKER))
        self.assertNotIn("not a delegation path here", body)
        self.assertNotIn("Settle your spawn path ONCE", body)
        self.assertNotIn("herdr-registry.py request", body)
        self.assertNotIn("agent-teammate.py", body)

    def test_codex_and_agy_workers_invoke_history_containment(self):
        for provider in ("codex", "agy"):
            calls = []

            def run(command, **kwargs):
                calls.append((command, kwargs))

            with self.subTest(provider=provider), patch.dict(
                os.environ, {"HERDR_AGENT_PANE": "1"}, clear=True
            ):
                context.contain_worker_history(provider, '{"id":"worker"}', run)
            self.assertEqual(
                calls[0][0],
                [str(context.WORKER_HISTORY), "--provider", provider],
            )
            self.assertEqual(calls[0][1]["input"], '{"id":"worker"}')

    def test_root_sessions_do_not_invoke_history_containment(self):
        calls = []
        with patch.dict(os.environ, {}, clear=True):
            context.contain_worker_history("codex", "{}", lambda *args, **kwargs: calls.append(args))
        self.assertEqual(calls, [])

    def test_a_registry_root_outranks_the_worker_markers(self):
        """`herdr-registry.py launch` provisions its root THROUGH `agent-teammate`, so the
        root carries child markers while being the orchestrator of its own run. Reading it
        as a worker would strip the drop-box clause and leave it with no spawn path at all."""
        for marker in ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD"):
            with self.subTest(marker=marker):
                environment = dict(HERDR, **REGISTRY, **{marker: "1"})
                body = self.codex_context(**environment)
                self.assertIn("[agent-host: herdr]", body)
                self.assertNotIn("— worker]", body)
                self.assertIn("herdr-registry.py request", body)
                self.assertIn(REGISTRY["HERDR_REGISTRY_ROOT"], body)
                self.assertIn(
                    "[agent-host: herdr]",
                    self.emit(provider="claude", **environment),
                )

    def test_a_registry_LEAF_is_a_worker_despite_holding_a_capability(self):
        """`claim_and_spawn` mints every child a `grants=[]` capability and hands it a
        `HERDR_REGISTRY_KEY` work item, and the registry's own leafness rule refuses that
        capability's requests (`capability_no_spawn`). Reading the capability alone as a
        spawn grant promotes exactly the leaves the drop box exists to contain."""
        environment = dict(HERDR, **REGISTRY, **WORKER,
                           HERDR_REGISTRY_KEY="review-auth-1",
                           HERDR_REGISTRY_RESULTS="/tmp/herdr-registry/run-abc/results/k")
        body = self.codex_context(**environment)
        self.assertIn("[agent-host: herdr — worker]", body)
        self.assertNotIn("herdr-registry.py request", body)
        self.assertNotIn("Settle your spawn path ONCE", body)
        self.assertIn("Remain a leaf", body)
        self.assertIn("[agent-host: herdr — worker]",
                      self.emit(provider="claude", **environment))

    def test_a_registry_key_without_the_markers_does_not_manufacture_a_worker(self):
        # Role comes from the markers; the key only withholds the root exemption.
        body = self.emit(provider="claude", **dict(HERDR, **REGISTRY,
                                                   HERDR_REGISTRY_KEY="review-auth-1"))
        self.assertIn("[agent-host: herdr]", body)
        self.assertNotIn("— worker]", body)

    def test_a_half_configured_registry_does_not_promote_a_worker(self):
        for partial in ({"HERDR_REGISTRY_ROOT": REGISTRY["HERDR_REGISTRY_ROOT"]},
                        {"HERDR_REGISTRY_CAPABILITY": REGISTRY["HERDR_REGISTRY_CAPABILITY"]}):
            body = self.emit(provider="claude", **dict(HERDR, **WORKER, **partial))
            self.assertIn("[agent-host: herdr — worker]", body)

    # -- other providers are unaffected

    def test_a_claude_session_is_not_given_the_codex_registry_clause(self):
        body = self.emit(provider="claude", **dict(HERDR, **REGISTRY))
        self.assertIn("[agent-host: herdr]", body)
        self.assertNotIn("herdr-registry.py request", body)

    def test_an_agy_session_is_not_given_the_codex_registry_clause(self):
        payload = json.loads(self.emit(provider="agy", **dict(HERDR, **REGISTRY)))
        rendered = json.dumps(payload)
        self.assertIn("[agent-host: herdr]", rendered)
        self.assertNotIn("herdr-registry.py request", rendered)

    def test_registry_root_reads_both_variables(self):
        for environment, expected in (
            (REGISTRY, REGISTRY["HERDR_REGISTRY_ROOT"]),
            ({"HERDR_REGISTRY_ROOT": "/tmp/x"}, None),
            ({}, None),
        ):
            saved = {key: os.environ.get(key) for key in
                     ("HERDR_REGISTRY_ROOT", "HERDR_REGISTRY_CAPABILITY")}
            for key in saved:
                os.environ.pop(key, None)
            os.environ.update(environment)
            try:
                self.assertEqual(context.registry_root(), expected)
            finally:
                for key, value in saved.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
