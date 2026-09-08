#!/usr/bin/env python3

import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.dont_write_bytecode = True
SCRIPT_DIR = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolves deferred annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


registry = load("herdr_registry", "herdr-registry.py")
teammate = load("agent_teammate", "agent-teammate.py")

ROOT_ID = "run40fb"
PROFILE = "endu"
ANCHOR = "wK:p1"
CODEX_HOME = "/tmp/fixture-codex-alt"
# The fixture's roles.json, so a spawn resolves the same seat on every machine. The real
# table lives in ~/dotfiles and moves; these rows only have to be self-consistent with what
# the tests assert reaches the CLI. A route holds one runner per provider, which is what
# `agent-routing.mjs get <route> --provider <p>` picks from.
FIXTURE_ROUTES = {
    "code.routine": {
        "claude": {"provider": "claude", "model": "opus", "effort": "high"},
        "codex": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "high",
                  "sandbox": "workspace-write", "approval": "never"},
        "agy": {"provider": "agy", "model": "gemini-3-pro", "effort": "high"},
    },
    "review.deep": {"claude": {"provider": "claude", "model": "opus", "effort": "high"}},
    # A second claude seat, routed somewhere else on purpose: it is how a test tells which
    # of the three sources the spawn actually took its role from.
    "code.light": {"claude": {"provider": "claude", "model": "sonnet", "effort": "low"}},
}
CLAUDE_ROUTE_FLAGS = ["--model", "opus", "--effort", "high"]
REVIEWER_ROUTE_FLAGS = ["--model", "sonnet", "--effort", "low"]

FAKE_ROUTING = '''#!/usr/bin/env python3
import json, os, sys

routes = json.load(open(os.environ["FIXTURE_ROUTES"]))
role = sys.argv[2] if len(sys.argv) > 2 else ""
provider = sys.argv[sys.argv.index("--provider") + 1] if "--provider" in sys.argv else ""
runner = (routes.get(role) or {}).get(provider)
print(json.dumps(runner or {"error": "no %s runner in route %s" % (provider, role)}))
'''

# Stands in for ensure-cwd-trust.sh and ensure-agent-cwd-trust.py: the real ones write
# ~/.claude.json and the Codex/AGY settings under the caller's own HOME.
FAKE_TRUST = '''#!/usr/bin/env python3
import sys
print("{}")
sys.exit(0)
'''

# Runs the REAL agent-teammate, with the three couplings that would otherwise reach outside
# this fixture pinned first: the roles.json lookup (a live table that moves) and the two
# trust preflights (they write the caller's HOME). The worker markers go too, because this
# suite is itself normally executed inside an agent pane and agent-teammate refuses to spawn
# from one -- which is the launch path under test.
TEAMMATE_SHIM = '''#!/usr/bin/env python3
import importlib.util, os, sys

for marker in ("HERDR_AGENT_PANE", "AGENT_TEAMMATE_CHILD"):
    os.environ.pop(marker, None)
spec = importlib.util.spec_from_file_location("agent_teammate", os.environ["TEAMMATE_SCRIPT"])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROUTING_SCRIPT = os.environ["FIXTURE_ROUTING"]
module.TRUST_SCRIPT = os.environ["FIXTURE_TRUST"]
module.PROVIDER_TRUST_SCRIPT = os.environ["FIXTURE_TRUST"]
sys.exit(module.main())
'''


class AgentTeammateProfileTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.routes = os.path.join(self.base, "routes.json")
        pathlib.Path(self.routes).write_text(json.dumps(FIXTURE_ROUTES))
        self.routing = os.path.join(self.base, "fake-routing")
        pathlib.Path(self.routing).write_text(FAKE_ROUTING)
        os.chmod(self.routing, 0o755)
        self.trust = os.path.join(self.base, "fake-trust")
        pathlib.Path(self.trust).write_text(FAKE_TRUST)
        os.chmod(self.trust, 0o755)

    def run_main(self, provider, role, environment, *agent_args):
        """`--role` has been required since dotfiles dd804cc, so every spawn resolves a
        seat. The table and the trust preflights are the fixture's, because this test is
        about which account crosses the pane and nothing else."""
        invoked = []

        def invoke(args):
            invoked.append(args)
            return {"host": "herdr", "provider": provider, "name": args.name, "pane": "wT:p1"}

        stderr = io.StringIO()
        with mock.patch.object(teammate, "ROUTING_SCRIPT", self.routing), \
                mock.patch.object(teammate, "TRUST_SCRIPT", self.trust), \
                mock.patch.object(teammate, "PROVIDER_TRUST_SCRIPT", self.trust), \
                mock.patch.object(sys, "argv", ["agent-teammate.py", provider, "worker",
                                                 "--role", role, "--", *agent_args]), \
                mock.patch.dict(os.environ, dict(environment, FIXTURE_ROUTES=self.routes),
                                clear=True), \
                mock.patch.object(teammate, "invoke_herdr", side_effect=invoke), \
                mock.patch("sys.stdout", new=io.StringIO()), \
                mock.patch("sys.stderr", new=stderr):
            status = teammate.main()
        return status, invoked, stderr.getvalue()

    def test_account_context_crosses_a_non_claude_pane(self):
        status, invoked, stderr = self.run_main(
            "codex", "code.routine", {"HERDR_ENV": "1", "AGENT_PROFILE_LABEL": PROFILE,
                                     "HOME": "/h", "CODEX_HOME": "/h/.codex-alt"}
        )

        self.assertEqual(status, 0, stderr)
        # The label so a downstream claude spawn inherits it, the home so this pane's own
        # binary has an account: a herdr pane inherits neither from the caller.
        self.assertEqual(invoked[0].env, [f"AGENT_PROFILE_LABEL={PROFILE}",
                                          "CODEX_HOME=/h/.codex-alt",
                                          "CODEX_CONFIG_PATH=/h/.codex-alt/config.toml"])

    def test_a_profileless_claude_spawn_runs_the_vendor_default(self):
        """ADR 0005: no account is not a refusal any more. The pane starts the account a
        person gets by typing `claude`."""
        status, invoked, stderr = self.run_main("claude", "code.routine",
                                                {"HERDR_ENV": "1", "HOME": "/h"})

        self.assertEqual(status, 0, stderr)
        self.assertEqual(invoked[0].env, [])

    def test_a_role_owns_the_model_flag(self):
        """The deferred F13 gap, pinned: a request that names both is refused rather than
        letting a caller-chosen model overrule the seat's (ADR 0009)."""
        status, invoked, stderr = self.run_main(
            "claude", "code.routine", {"HERDR_ENV": "1", "HOME": "/h",
                                          "AGENT_PROFILE_LABEL": PROFILE},
            "--model", "claude-opus-5",
        )

        self.assertEqual(status, 2)
        self.assertEqual(invoked, [])
        self.assertIn("--role owns claude model, effort", stderr)


# Stands in for the herdr server: records every call, keeps an agent roster, and lets a
# test force a spawn failure or a stalled prompt. `pane edges` fails on purpose so
# agent-teammate skips its geometry pass — the real one needs a real window.
FAKE_HERDR = '''#!/usr/bin/env python3
import json, os, sys

path = os.environ["FAKE_HERDR_STATE"]

try:
    with open(path) as handle:
        state = json.load(handle)
except FileNotFoundError:
    state = {"agents": {}, "calls": [], "next_pane": 1, "panes": {}}

args = sys.argv[1:]
state["calls"].append(args)
group, action = (args + ["", ""])[:2]


def flag(name, default=None):
    return args[args.index(name) + 1] if name in args else default


def save():
    with open(path, "w") as handle:
        json.dump(state, handle)


if (group, action) == ("agent", "list"):
    save()
    print(json.dumps({"result": {"agents": list(state["agents"].values())}}))
elif (group, action) == ("pane", "split"):
    requested = flag("--cwd")
    pane = "wT:p%d" % state["next_pane"]
    state["next_pane"] += 1
    state.setdefault("panes", {})[pane] = os.path.realpath(requested) if requested else None
    save()
    print(json.dumps({"result": {"pane": {"pane_id": pane}}}))
elif (group, action) == ("agent", "start"):
    if state.get("fail_start"):
        save()
        print("agent_start_denied", file=sys.stderr)
        sys.exit(1)
    name = args[2]
    pane = flag("--pane")
    entry = {
        "name": name, "pane_id": pane,
        "agent_status": state.get("status", "idle"), "interactive_ready": True,
    }
    entry["cwd"] = state.get("panes", {}).get(pane)
    if not state.get("no_session"):
        entry["agent_session"] = {"value": "sess-" + name}
    state["agents"][name] = entry
    save()
    print(json.dumps({"result": {"ok": True}}))
elif (group, action) == ("pane", "process-info"):
    save()
    if state.get("fail_process_info"):
        print("process_info_unavailable", file=sys.stderr)
        sys.exit(1)
    info = {
        "foreground_processes": [{"pid": 4242, "argv0": "claude", "cmdline": "claude"}],
    }
    if not state.get("no_process_group"):
        info["foreground_process_group_id"] = 4242
    print(json.dumps({"result": {"process_info": info}}))
elif (group, action) == ("pane", "edges"):
    save()
    sys.exit(1)
elif (group, action) == ("pane", "close"):
    if state.get("fail_close"):
        save()
        print("pane_close_failed", file=sys.stderr)
        sys.exit(1)
    for name, entry in list(state["agents"].items()):
        if entry["pane_id"] == args[2]:
            del state["agents"][name]
    save()
    print(json.dumps({"result": {"ok": True}}))
elif (group, action) == ("agent", "prompt"):
    save()
    outcome = state.get("prompt_outcome")
    if outcome:
        print(outcome, file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"result": {"ok": True}}))
elif (group, action) == ("agent", "send-keys"):
    save()
    print(json.dumps({"result": {"ok": True}}))
else:
    save()
    print("unsupported: %s" % args, file=sys.stderr)
    sys.exit(2)
'''


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


# --- pure validation -------------------------------------------------------------------


CAP_ID = "cap-" + "a" * 16


def request_document(**overrides):
    document = {
        "schema_version": 1,
        "idempotency_key": "k1",
        "capability": {"cap_id": CAP_ID, "proof": "b" * 64},
        "family": "claude",
        "name": "reviewer-1",
        "cwd": "/tmp",
        "argv": ["--permission-mode", "acceptEdits"],
        "role": "review.deep",
    }
    document.update(overrides)
    return document


def signed(document, secret):
    """Re-sign a request the way the client does. The proof ignores any existing proof."""
    document = json.loads(json.dumps(document))
    document["capability"] = {
        "cap_id": document["capability"]["cap_id"],
        "proof": registry.request_proof(secret, document),
    }
    return document


def raw(document):
    return json.dumps(document).encode("utf-8")


class ParseRequestTest(unittest.TestCase):
    def parse(self, key="k1", **overrides):
        return registry.parse_request(raw(request_document(**overrides)), key)

    def reason(self, key="k1", **overrides):
        with self.assertRaises(registry.RequestError) as caught:
            self.parse(key, **overrides)
        return caught.exception.reason

    def test_accepts_a_well_formed_request(self):
        request = self.parse()
        self.assertEqual(request.family, "claude")
        self.assertEqual(request.argv, ("--permission-mode", "acceptEdits"))

    def test_rejects_unparsable_bytes(self):
        with self.assertRaises(registry.RequestError) as caught:
            registry.parse_request(b"{not json", "k1")
        self.assertEqual(caught.exception.reason, "unparsable")

    def test_rejects_a_json_array(self):
        with self.assertRaises(registry.RequestError) as caught:
            registry.parse_request(b"[]", "k1")
        self.assertEqual(caught.exception.reason, "not_an_object")

    def test_names_every_missing_field(self):
        document = request_document()
        del document["family"]
        del document["role"]
        with self.assertRaises(registry.RequestError) as caught:
            registry.parse_request(raw(document), "k1")
        self.assertEqual(caught.exception.reason, "missing:family,role")

    def test_rejects_a_boolean_schema_version(self):
        # True == 1 in Python, so a naive equality check would accept this.
        self.assertEqual(self.reason(schema_version=True), "schema_version")

    def test_rejects_a_key_the_filename_does_not_match(self):
        self.assertEqual(self.reason(key="other", idempotency_key="k1"), "key_mismatch")

    def test_rejects_an_unknown_family(self):
        self.assertEqual(self.reason(family="gemini"), "bad_family")

    def test_rejects_path_traversal_in_the_name(self):
        self.assertEqual(self.reason(name="../../escape"), "bad_name")

    def test_rejects_a_non_string_cwd(self):
        self.assertEqual(self.reason(cwd=["/tmp"]), "bad_cwd:type")

    def test_rejects_a_malformed_capability(self):
        self.assertEqual(self.reason(capability="cap-x:secret"), "bad_capability:shape")
        self.assertEqual(self.reason(capability={"cap_id": "x", "proof": "b" * 64}),
                         "bad_capability:cap_id")
        self.assertEqual(self.reason(capability={"cap_id": CAP_ID, "proof": "no"}),
                         "bad_capability:proof")

    def test_rejects_a_request_that_carries_a_bearer_secret_at_all(self):
        self.assertEqual(
            self.reason(capability={"cap_id": CAP_ID, "proof": "b" * 64, "secret": "c" * 64}),
            "bad_capability:secret_present",
        )

    def test_the_proof_covers_every_field_including_unknown_ones(self):
        base = request_document()
        material = registry.proof_material(base)
        self.assertNotIn("proof", material)
        for field, value in (("name", "other"), ("cwd", "/etc"), ("idempotency_key", "k2"),
                             ("argv", []), ("family", "codex"), ("smuggled", {"x": 1})):
            self.assertNotEqual(
                registry.proof_material(request_document(**{field: value})), material,
                f"{field} must be covered by the proof",
            )

    def test_the_proof_covers_nested_capability_fields(self):
        # Reconstructing only cap_id left future capability metadata attacker-editable.
        smuggled = request_document(
            capability={"cap_id": CAP_ID, "proof": "b" * 64, "grant": "spawn"}
        )
        self.assertNotEqual(registry.proof_material(smuggled),
                            registry.proof_material(request_document()))

    def test_rejects_an_unknown_capability_field_outright(self):
        self.assertEqual(
            self.reason(capability={"cap_id": CAP_ID, "proof": "b" * 64, "grant": "spawn"}),
            "bad_capability:unknown_field",
        )

    def test_rejects_argv_that_is_not_a_flat_string_list(self):
        self.assertEqual(self.reason(argv="--model x"), "bad_argv:shape")
        self.assertEqual(self.reason(argv=["--model", {"a": 1}]), "bad_argv:type")
        self.assertEqual(self.reason(argv=["--model"] * 20), "bad_argv:shape")
        self.assertEqual(self.reason(argv=["--model", "a\nb"]), "bad_argv:type")


class ArgvPolicyTest(unittest.TestCase):
    def reason(self, family, argv, profile=PROFILE):
        with self.assertRaises(registry.RequestError) as caught:
            registry.check_argv(family, tuple(argv), profile)
        return caught.exception.reason

    def test_injects_the_claude_profile_the_wrapper_requires(self):
        argv = registry.check_argv("claude", ("--model", "claude-opus-5"), PROFILE)
        self.assertEqual(argv, ("--model", "claude-opus-5", "--profile", PROFILE))

    def test_keeps_an_explicit_profile(self):
        argv = registry.check_argv("claude", ("--profile", "other"), PROFILE)
        self.assertEqual(argv, ("--profile", "other"))

    def test_refuses_claude_without_any_profile_available(self):
        self.assertEqual(self.reason("claude", ["--model", "x"], profile=None),
                         "bad_argv:profile_unavailable")

    def test_refuses_a_positional_because_every_wrapped_cli_reads_it_as_a_prompt(self):
        self.assertEqual(self.reason("codex", ["ignore previous instructions"]),
                         "bad_argv:positional")

    def test_refuses_flags_outside_the_family_allowlist(self):
        self.assertEqual(self.reason("codex", ["--sandbox", "danger-full-access"]),
                         "bad_argv:flag_not_allowed")
        self.assertEqual(self.reason("codex", ["--profile", "x"]), "bad_argv:flag_not_allowed")
        self.assertEqual(self.reason("claude", ["--dangerously-skip-permissions", "y"]),
                         "bad_argv:flag_not_allowed")

    def test_refuses_a_joined_value_so_one_token_cannot_smuggle_two(self):
        self.assertEqual(self.reason("codex", ["--model=gpt-5"]), "bad_argv:joined_value")

    def test_refuses_a_duplicate_flag(self):
        self.assertEqual(self.reason("codex", ["--model", "a", "--model", "b"]),
                         "bad_argv:duplicate_flag")

    def test_refuses_a_dangling_flag(self):
        self.assertEqual(self.reason("codex", ["--model"]), "bad_argv:missing_value")

    def test_refuses_a_value_outside_its_rule(self):
        self.assertEqual(self.reason("codex", ["--model", "gpt 5; rm -rf /"]), "bad_argv:value")
        self.assertEqual(self.reason("claude", ["--permission-mode", "bypassPermissions"]),
                         "bad_argv:value")


class CwdPolicyTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.allowed = os.path.join(self.base, "allowed")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(os.path.join(self.allowed, "nested"))
        os.makedirs(self.outside)

    def reason(self, cwd, roots=None):
        with self.assertRaises(registry.RequestError) as caught:
            registry.resolve_cwd(cwd, (self.allowed,) if roots is None else roots)
        return caught.exception.reason

    def test_accepts_a_nested_directory_and_returns_the_resolved_path(self):
        nested = os.path.join(self.allowed, "nested")
        self.assertEqual(registry.resolve_cwd(nested, (self.allowed,)), os.path.realpath(nested))

    def test_refuses_everything_when_no_allowlist_was_configured(self):
        self.assertEqual(self.reason(self.allowed, roots=()), "bad_cwd:no_allowlist")

    def test_refuses_a_relative_path(self):
        self.assertEqual(self.reason("relative/path"), "bad_cwd:not_absolute")

    def test_refuses_traversal_out_of_the_allowlist(self):
        self.assertEqual(self.reason(os.path.join(self.allowed, "..", "outside")),
                         "bad_cwd:outside_allowlist")

    def test_refuses_a_symlink_that_escapes_the_allowlist(self):
        link = os.path.join(self.allowed, "escape")
        os.symlink(self.outside, link)
        self.assertEqual(self.reason(link), "bad_cwd:outside_allowlist")

    def test_refuses_a_prefix_neighbour_of_the_allowlist(self):
        sibling = self.allowed + "-evil"
        os.makedirs(sibling)
        self.assertEqual(self.reason(sibling), "bad_cwd:outside_allowlist")

    def test_refuses_a_path_that_is_not_a_directory(self):
        target = os.path.join(self.allowed, "file")
        with open(target, "w") as handle:
            handle.write("x")
        self.assertEqual(self.reason(target), "bad_cwd:not_a_directory")


class NameAndAdmissionTest(unittest.TestCase):
    def test_namespaces_the_requested_name_under_the_root(self):
        self.assertEqual(registry.agent_name_for(ROOT_ID, "reviewer-1"), "rrun40f-reviewer-1")

    def test_truncates_to_the_herdr_agent_name_limit(self):
        name = registry.agent_name_for(ROOT_ID, "a" * 31)
        self.assertEqual(len(name), registry.AGENT_NAME_LIMIT)

    def test_admits_below_the_quota(self):
        self.assertEqual(registry.admission(1, 4, None, 0, 60), (registry.ADMIT, None))

    def test_queues_at_the_quota(self):
        self.assertEqual(registry.admission(4, 4, None, 0, 60), (registry.QUEUE, None))

    def test_keeps_queueing_inside_the_queue_timeout(self):
        self.assertEqual(registry.admission(4, 4, 100.0, 130.0, 60), (registry.QUEUE, None))

    def test_refuses_loudly_once_starvation_outlasts_the_timeout(self):
        self.assertEqual(registry.admission(4, 4, 100.0, 161.0, 60),
                         (registry.REFUSE, "quota_timeout"))


SESSION = "0123456789abcdef"
OTHER_SESSION = "fedcba9876543210"


class CapabilityTest(unittest.TestCase):
    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.keyring = registry.Keyring.generate()
        self.record, self.secret = self.keyring.mint(
            self.store, ROOT_ID, SESSION, "orchestrator", [registry.SPAWN], ttl=60, now=100.0
        )
        self.document = signed(request_document(capability={"cap_id": self.record["cap_id"]}),
                               self.secret)

    def check(self, record=None, cap_id=None, document=None, root_id=ROOT_ID,
              session=SESSION, now=110.0, secret=None):
        document = self.document if document is None else document
        return registry.check_capability(
            self.record if record is None else record,
            cap_id or self.record["cap_id"], document["capability"]["proof"],
            registry.proof_material(document), root_id, session, registry.SPAWN, now,
            self.secret if secret is None else secret,
        )

    def test_authorizes_a_matching_capability(self):
        self.assertIsNone(self.check())

    def test_refuses_an_unknown_capability(self):
        self.assertEqual(self.check(record={}), "capability_unknown")
        self.assertEqual(self.check(record=None, cap_id=CAP_ID), "capability_unknown")

    def test_refuses_a_capability_minted_for_another_root(self):
        self.assertEqual(self.check(root_id="other-run"), "capability_unknown")

    def test_refuses_a_capability_minted_for_an_earlier_session_of_the_same_root_id(self):
        self.assertEqual(self.check(session=OTHER_SESSION), "capability_wrong_session")

    def test_refuses_a_proof_that_does_not_match_the_request(self):
        tampered = json.loads(json.dumps(self.document))
        tampered["name"] = "escalated"
        self.assertEqual(self.check(document=tampered), "capability_bad_proof")

    def test_refuses_an_expired_capability(self):
        self.assertEqual(self.check(now=200.0), "capability_expired")

    def test_refuses_a_capability_with_no_expiry_at_all(self):
        record = dict(self.record, expires_at=None)
        self.assertEqual(self.check(record=record), "capability_expired")

    def test_refuses_a_non_finite_expiry(self):
        for expiry in (float("inf"), float("nan")):
            self.assertEqual(self.check(record=dict(self.record, expires_at=expiry)),
                             "capability_expired")

    def test_defaults_to_a_finite_ttl(self):
        record, _ = self.keyring.mint(self.store, ROOT_ID, SESSION, "default", [], now=0.0)
        self.assertEqual(record["expires_at"], registry.DEFAULT_TTL_SECONDS)

    def test_scopes_the_store_directory_to_the_session(self):
        other, _ = self.keyring.mint(
            self.store, ROOT_ID, OTHER_SESSION, "orchestrator", [registry.SPAWN], now=100.0
        )
        self.assertNotEqual(
            os.path.dirname(registry.capability_path(self.store, ROOT_ID, SESSION,
                                                     self.record["cap_id"])),
            os.path.dirname(registry.capability_path(self.store, ROOT_ID, OTHER_SESSION,
                                                     other["cap_id"])),
        )
        self.assertFalse(os.path.exists(
            registry.capability_path(self.store, ROOT_ID, OTHER_SESSION, self.record["cap_id"])
        ))

    def test_refuses_a_worker_capability_which_carries_no_spawn_grant(self):
        worker, secret = self.keyring.mint(
            self.store, ROOT_ID, SESSION, "worker:k1", [], now=100.0
        )
        document = signed(request_document(capability={"cap_id": worker["cap_id"]}), secret)
        self.assertEqual(
            registry.check_capability(
                worker, worker["cap_id"], document["capability"]["proof"],
                registry.proof_material(document), ROOT_ID, SESSION, registry.SPAWN, 110.0,
                secret,
            ),
            "capability_no_spawn",
        )


class KeyringTest(unittest.TestCase):
    """No capability secret may exist on disk: a same-uid worker can read any file mode."""

    def setUp(self):
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, ignore_errors=True)
        self.keyring = registry.Keyring.generate()
        self.record, self.secret = self.keyring.mint(
            self.store, ROOT_ID, SESSION, "orchestrator", [registry.SPAWN], now=100.0
        )
        self.path = registry.capability_path(self.store, ROOT_ID, SESSION,
                                             self.record["cap_id"])

    def stored(self):
        return json.loads(pathlib.Path(self.path).read_text())

    def test_the_whole_store_contains_no_secret_material(self):
        body = pathlib.Path(self.path).read_text()
        self.assertNotIn(self.secret, body)
        self.assertNotIn("secret", self.stored())

    def test_reading_the_entire_store_yields_nothing_that_signs_a_request(self):
        # The reviewer's trigger: a worker recovers root_id/session/cap_id and the record.
        recovered = self.stored()
        forged = signed(
            request_document(capability={"cap_id": recovered["cap_id"]}, name="escalated"),
            json.dumps(recovered),
        )
        self.assertEqual(
            registry.check_capability(
                recovered, recovered["cap_id"], forged["capability"]["proof"],
                registry.proof_material(forged), ROOT_ID, SESSION, registry.SPAWN, 110.0,
                self.keyring.secret(ROOT_ID, SESSION, recovered["cap_id"]),
            ),
            "capability_bad_proof",
        )

    def test_a_forged_grant_written_into_the_store_fails_the_record_mac(self):
        record = self.stored()
        record["grants"] = ["spawn", "root"]
        pathlib.Path(self.path).write_text(json.dumps(record))
        self.assertEqual(
            self.keyring.open(self.store, ROOT_ID, SESSION, record["cap_id"]),
            (None, "capability_tampered"),
        )

    def test_an_extended_expiry_written_into_the_store_fails_the_record_mac(self):
        record = self.stored()
        record["expires_at"] = record["expires_at"] + 10_000_000
        pathlib.Path(self.path).write_text(json.dumps(record))
        self.assertEqual(
            self.keyring.open(self.store, ROOT_ID, SESSION, record["cap_id"])[1],
            "capability_tampered",
        )

    def test_a_wholly_invented_record_fails_the_record_mac(self):
        invented = dict(self.stored(), cap_id="cap-" + "9" * 16, subject="invented")
        invented["mac"] = "0" * 64
        path = registry.capability_path(self.store, ROOT_ID, SESSION, invented["cap_id"])
        pathlib.Path(path).write_text(json.dumps(invented))
        self.assertEqual(
            self.keyring.open(self.store, ROOT_ID, SESSION, invented["cap_id"])[1],
            "capability_tampered",
        )

    def test_an_intact_record_opens(self):
        self.assertEqual(self.keyring.open(self.store, ROOT_ID, SESSION,
                                           self.record["cap_id"])[1], None)

    def test_a_different_master_key_derives_a_different_secret(self):
        other = registry.Keyring.generate()
        self.assertNotEqual(other.secret(ROOT_ID, SESSION, self.record["cap_id"]), self.secret)
        self.assertEqual(
            other.open(self.store, ROOT_ID, SESSION, self.record["cap_id"])[1],
            "capability_tampered",
        )

    def test_the_same_master_key_reproduces_the_secret_across_processes(self):
        material = registry.Keyring.generate()._master.hex()
        self.assertEqual(
            registry.Keyring.from_hex(material).secret(ROOT_ID, SESSION, "cap-" + "1" * 16),
            registry.Keyring.from_hex(material).secret(ROOT_ID, SESSION, "cap-" + "1" * 16),
        )

    def test_derivation_is_scoped_to_root_session_and_capability(self):
        base = self.keyring.secret(ROOT_ID, SESSION, "cap-" + "1" * 16)
        self.assertNotEqual(base, self.keyring.secret("other", SESSION, "cap-" + "1" * 16))
        self.assertNotEqual(base, self.keyring.secret(ROOT_ID, OTHER_SESSION, "cap-" + "1" * 16))
        self.assertNotEqual(base, self.keyring.secret(ROOT_ID, SESSION, "cap-" + "2" * 16))

    def test_a_short_master_key_is_refused(self):
        with self.assertRaises(registry.RegistryError):
            registry.Keyring(b"tooshort")


class DropBoxIoTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.fd = registry.open_dir(self.base)
        self.addCleanup(os.close, self.fd)

    def test_reads_a_regular_file(self):
        pathlib.Path(self.base, "a.json").write_text("{}")
        self.assertEqual(registry.read_bounded(self.fd, "a.json", 100), b"{}")

    def test_refuses_to_follow_a_symlink_out_of_the_drop_dir(self):
        secret = os.path.join(self.base, "..", "secret")
        os.symlink(secret, os.path.join(self.base, "b.json"))
        with self.assertRaises(registry.RequestError) as caught:
            registry.read_bounded(self.fd, "b.json", 100)
        self.assertEqual(caught.exception.reason, "symlink")

    def test_refuses_an_oversize_file(self):
        pathlib.Path(self.base, "c.json").write_text("x" * 200)
        with self.assertRaises(registry.RequestError) as caught:
            registry.read_bounded(self.fd, "c.json", 100)
        self.assertEqual(caught.exception.reason, "oversize")

    def test_refuses_a_directory(self):
        os.makedirs(os.path.join(self.base, "d.json"))
        with self.assertRaises(registry.RequestError) as caught:
            registry.read_bounded(self.fd, "d.json", 100)
        self.assertEqual(caught.exception.reason, "not_a_regular_file")

    def test_publish_once_never_clobbers_an_existing_record(self):
        path = os.path.join(self.base, "record.json")
        registry.publish_once(path, {"first": True})
        with self.assertRaises(FileExistsError):
            registry.publish_once(path, {"first": False})
        self.assertEqual(json.loads(pathlib.Path(path).read_text()), {"first": True})


# --- watcher ---------------------------------------------------------------------------


class WatcherFixture(unittest.TestCase):
    """A registry root, a fake herdr server, and the REAL agent-teammate spawn path."""

    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root_dir = os.path.join(self.base, "registry")
        self.store = os.path.join(self.base, "caps")
        self.workspace = os.path.join(self.base, "workspace")
        os.makedirs(self.workspace)
        registry.init_root(self.root_dir, ROOT_ID, [self.workspace])
        self.root = registry.Root(self.root_dir)

        self.state = os.path.join(self.base, "herdr-state.json")
        self.fake = os.path.join(self.base, "fake-herdr")
        pathlib.Path(self.fake).write_text(FAKE_HERDR)
        os.chmod(self.fake, 0o755)
        self.teammate_shim = self.script("teammate-shim", TEAMMATE_SHIM)
        routing = self.script("fake-routing", FAKE_ROUTING)
        trust = self.script("fake-trust", FAKE_TRUST)
        routes = os.path.join(self.base, "routes.json")
        pathlib.Path(routes).write_text(json.dumps(FIXTURE_ROUTES))
        self.env = {
            "FAKE_HERDR_STATE": self.state,
            "AGENT_HARNESS_HERDR_BIN": self.fake,
            "HERDR_ENV": "1",
            "HERDR_PANE_ID": ANCHOR,
            "AGENT_PROFILE_LABEL": PROFILE,
            # A codex spawn inherits the caller's account, so the caller has to have one:
            # the wrappers stopped guessing (ADR 0004) and a pane inherits no environment.
            "CODEX_HOME": CODEX_HOME,
            "CODEX_CONFIG_PATH": os.path.join(CODEX_HOME, "config.toml"),
            "AGENT_HARNESS_TEAMMATE": self.teammate_shim,
            "TEAMMATE_SCRIPT": str(SCRIPT_DIR / "agent-teammate.py"),
            "FIXTURE_ROUTING": routing,
            "FIXTURE_ROUTES": routes,
            "FIXTURE_TRUST": trust,
        }
        self.saved = {key: os.environ.get(key) for key in self.env}
        os.environ.update(self.env)
        self.addCleanup(self.restore)

        self.keyring = registry.Keyring.generate()
        self.saved[registry.MASTER_KEY_ENV] = os.environ.get(registry.MASTER_KEY_ENV)
        os.environ[registry.MASTER_KEY_ENV] = self.keyring._master.hex()
        self.record, self.secret = self.keyring.mint(
            self.store, ROOT_ID, self.root.session, "orchestrator", [registry.SPAWN]
        )
        self.clock = Clock()

    def client_environment(self):
        return dict(os.environ, HERDR_REGISTRY_ROOT=self.root_dir,
                    HERDR_REGISTRY_CAPABILITY=f"{self.record['cap_id']}:{self.secret}")

    def registry_command(self, *arguments, environment=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), *arguments],
            capture_output=True, text=True, env=environment or self.client_environment(),
        )

    def script(self, name, body):
        path = os.path.join(self.base, name)
        pathlib.Path(path).write_text(body)
        os.chmod(path, 0o755)
        return path

    def restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def watcher(self, **overrides):
        options = dict(store=self.store, max_panes=4, queue_timeout=900.0,
                       profile_label=PROFILE, clock=self.clock)
        options.update(overrides)
        host = registry.Host(binary=self.fake, teammate=self.teammate_shim, anchor=ANCHOR)
        watcher = registry.Watcher(self.root, host, self.keyring, **options)
        watcher.log = lambda event, **fields: watcher.events.append(dict(fields, event=event))
        return watcher

    def herdr_flag(self, **flags):
        state = self.herdr_state() if os.path.exists(self.state) else {
            "agents": {}, "calls": [], "next_pane": 1
        }
        state.update(flags)
        with open(self.state, "w") as handle:
            json.dump(state, handle)

    def drop(self, key="k1", cap_id=None, secret=None, sign=True, nonce="1-000000000000beef",
             **overrides):
        fields = dict(
            idempotency_key=key,
            capability={"cap_id": cap_id or self.record["cap_id"]},
            cwd=self.workspace,
        )
        fields.update(overrides)
        document = request_document(**fields)
        if sign:
            document = signed(document, secret or self.secret)
        return self.place(key, document, nonce)

    def place(self, key, document, nonce="1-000000000000beef"):
        """Publish the way a client does: a per-submission name, write plus rename."""
        path = os.path.join(self.root.dir(registry.REQUESTS), f"{key}.{nonce}.json")
        temporary = os.path.join(self.root.dir(registry.REQUESTS), f".{key}.tmp")
        pathlib.Path(temporary).write_text(json.dumps(document))
        os.rename(temporary, path)
        # Age is measured from the file's own mtime, so the fake clock has to drive it.
        os.utime(path, (self.clock.now, self.clock.now))
        return path

    def request_path(self, key="k1"):
        found = registry.submissions(self.root.dir(registry.REQUESTS), key)
        return found[0] if found else os.path.join(
            self.root.dir(registry.REQUESTS), f"{key}.missing.json"
        )

    def herdr_state(self):
        with open(self.state) as handle:
            return json.load(handle)

    def worker_environment(self):
        """The env herdr actually put in the worker's pane, read back off the split call."""
        split = self.calls("pane", "split")[0]
        return dict(
            split[index + 1].split("=", 1)
            for index, token in enumerate(split) if token == "--env"
        )

    def calls(self, group, action):
        return [call for call in self.herdr_state()["calls"] if call[:2] == [group, action]]

    def agent_flags(self, name):
        """The user flags of the spawn named `name`, minus the worker-pool preamble the
        helper adds: a session id in the reserved namespace, the pane name, and the
        `--add-dir` that restores tool access to the caller's tree.
        """
        start = next(call for call in self.calls("agent", "start") if call[2] == name)
        argv = start[start.index("--") + 1:]
        self.assertEqual(argv[0], "--session-id")
        self.assertTrue(argv[1].startswith(teammate.WORKER_SID_PREFIX + "-"), argv[1])
        self.assertEqual(argv[2:4], ["-n", name])
        self.assertEqual(argv[-2:], ["--add-dir", os.path.realpath(self.workspace)])
        return argv[4:-2]

    def pane_record(self, key="k1"):
        return registry.load_json(self.root.record(registry.PANES, key))

    def rejection(self, key="k1"):
        return registry.load_json(self.root.record(registry.REJECTED, key))

    def heartbeat_state(self, key="k1"):
        return (registry.load_json(self.root.record(registry.HEARTBEAT, key)) or {}).get("state")


class SpawnTest(WatcherFixture):
    def test_a_dropped_request_becomes_a_visible_pane_with_a_provenance_chain(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()

        splits = self.calls("pane", "split")
        starts = self.calls("agent", "start")
        self.assertEqual(len(splits), 1, "the host performed exactly one pane split")
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0][2], "rrun40f-reviewer-1")

        pane = self.pane_record()
        self.assertEqual(pane["agent_name"], "rrun40f-reviewer-1")
        self.assertEqual(pane["pane_id"], "wT:p1")
        self.assertEqual(pane["cwd"], os.path.realpath(self.workspace))
        self.assertEqual(pane["provenance"]["pane_process"]["foreground_process_group_id"], 4242)
        self.assertEqual(pane["provenance"]["issuer_capability"], self.record["cap_id"])
        self.assertFalse(pane["provenance"]["adopted"])

        report = registry.chain(self.root, "k1")
        self.assertTrue(report["intact"], report["breaks"])

    def test_the_worker_pane_carries_a_capability_with_no_spawn_rights(self):
        self.drop()
        self.watcher().pass_once()
        env = self.worker_environment()
        self.assertEqual(env["HERDR_REGISTRY_ROOT"], self.root.path)
        self.assertEqual(env["HERDR_REGISTRY_KEY"], "k1")
        cap_id, _, secret = env["HERDR_REGISTRY_CAPABILITY"].partition(":")

        # The leafness half of the contract: the worker's own capability cannot spawn, even
        # though it can sign a perfectly well-formed request with it.
        self.drop(key="k2", cap_id=cap_id, secret=secret)
        self.watcher().pass_once()
        self.assertIsNone(self.pane_record("k2"))
        self.assertEqual(self.rejection("k2")["reason"], "capability_no_spawn")
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def test_the_claude_profile_the_wrapper_needed_becomes_the_panes_account(self):
        self.drop()
        self.watcher().pass_once()
        # The role owns the model and effort (ADR 0009), so they arrive from the route
        # rather than from the request. `--profile` is what check_argv appended, and no
        # vendor binary has that flag: ADR 0005 turns it into the pane's environment.
        self.assertEqual(self.agent_flags("rrun40f-reviewer-1"),
                         [*CLAUDE_ROUTE_FLAGS, "--permission-mode", "acceptEdits"])
        environment = self.worker_environment()
        self.assertEqual(environment["AGENT_PROFILE_LABEL"], PROFILE)
        self.assertTrue(environment["CLAUDE_PROFILE_DIR"].endswith("/.profiles/" + PROFILE))

    def test_a_repeated_idempotency_key_never_produces_a_second_pane(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        watcher.pass_once()
        self.drop()
        watcher.pass_once()
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def test_a_name_already_live_is_refused_rather_than_shadowed(self):
        self.drop()
        self.watcher().pass_once()
        self.drop(key="k2")
        self.watcher().pass_once()
        self.assertEqual(self.rejection("k2")["reason"], "name_in_use")

    def test_a_failed_agent_start_is_terminal_and_forbids_a_native_fallback(self):
        state = self.herdr_state() if os.path.exists(self.state) else {"agents": {}, "calls": [],
                                                                       "next_pane": 1}
        state["fail_start"] = True
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.drop()
        self.watcher().pass_once()
        rejection = self.rejection()
        self.assertTrue(rejection["reason"].startswith("spawn_failed:"))
        self.assertTrue(rejection["no_native_fallback"])
        self.assertIn("Do NOT fall back", rejection["guidance"])

    def test_a_request_naming_a_cwd_outside_the_allowlist_never_spawns(self):
        self.drop(cwd="/etc")
        self.watcher().pass_once()
        self.assertEqual(self.rejection()["reason"], "bad_cwd:outside_allowlist")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_a_forged_proof_never_spawns(self):
        self.drop(secret="f" * 64)
        self.watcher().pass_once()
        self.assertEqual(self.rejection()["reason"], "capability_bad_proof")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_an_unsigned_request_never_spawns(self):
        self.drop(sign=False)
        self.watcher().pass_once()
        self.assertEqual(self.rejection()["reason"], "bad_capability:proof")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_a_file_whose_name_is_not_a_safe_key_is_ignored(self):
        pathlib.Path(self.root.dir(registry.REQUESTS), "../escape.json").write_text("{}")
        self.watcher().pass_once()
        self.assertEqual(self.calls("pane", "split"), [])


class WorkerReplayTest(WatcherFixture):
    """Walk the exact escalation path the review found: the worker reads its own request
    file and replays whatever authorization it finds there."""

    def recovered_request(self):
        """What a spawned worker can actually reach: $HERDR_REGISTRY_ROOT + KEY."""
        env = self.worker_environment()
        found = registry.submissions(
            os.path.join(env["HERDR_REGISTRY_ROOT"], registry.REQUESTS),
            env["HERDR_REGISTRY_KEY"],
        )
        return json.loads(pathlib.Path(found[0]).read_text())

    def test_the_request_file_a_worker_can_read_holds_no_bearer_secret(self):
        self.drop()
        self.watcher().pass_once()
        recovered = self.recovered_request()
        self.assertNotIn("secret", recovered["capability"])
        self.assertNotIn(self.secret, json.dumps(recovered))

    def test_a_worker_replaying_its_own_request_for_a_second_pane_is_refused(self):
        self.drop()
        self.watcher().pass_once()
        recovered = self.recovered_request()

        # Exactly the review's trigger: reuse the recovered capability for a new name.
        replay = dict(recovered, idempotency_key="k2", name="second-pane")
        self.place("k2", replay)
        self.watcher().pass_once()

        self.assertIsNone(self.pane_record("k2"))
        self.assertEqual(self.rejection("k2")["reason"], "capability_bad_proof")
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def test_a_worker_replaying_the_request_verbatim_under_a_new_key_is_refused(self):
        self.drop()
        self.watcher().pass_once()
        self.place("k2", self.recovered_request())
        self.watcher().pass_once()
        self.assertEqual(self.rejection("k2")["reason"], "key_mismatch")
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def test_a_worker_replaying_the_request_byte_for_byte_gets_no_second_pane(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        self.place("k1", self.recovered_request(), nonce="2-00000000cafe0001")
        watcher.pass_once()
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def test_a_worker_cannot_escalate_cwd_or_argv_on_a_recovered_request(self):
        self.drop()
        self.watcher().pass_once()
        recovered = self.recovered_request()
        for index, (field, value) in enumerate((
            ("cwd", "/etc"),
            ("argv", ["--permission-mode", "plan"]),
            ("family", "codex"),
            ("role", "orchestrator"),
        )):
            key = f"esc{index}"
            self.place(key, dict(recovered, idempotency_key=key, **{field: value}))
            self.watcher().pass_once()
            self.assertEqual(self.rejection(key)["reason"], "capability_bad_proof",
                             f"{field} must be covered by the proof")
        self.assertEqual(len(self.calls("pane", "split")), 1)


class SessionReplayTest(WatcherFixture):
    def test_a_token_from_an_earlier_session_of_the_same_root_id_is_refused(self):
        stale_root = os.path.join(self.base, "stale")
        registry.init_root(stale_root, ROOT_ID, [self.workspace])
        stale = registry.Root(stale_root)
        self.assertNotEqual(stale.session, self.root.session)
        record, secret = self.keyring.mint(
            self.store, ROOT_ID, stale.session, "orchestrator", [registry.SPAWN]
        )

        self.drop(cap_id=record["cap_id"], secret=secret)
        self.watcher().pass_once()
        self.assertEqual(self.rejection()["reason"], "capability_unknown")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_init_mints_its_own_session_nonce(self):
        first = registry.init_root(os.path.join(self.base, "a"), ROOT_ID, [self.workspace])
        second = registry.init_root(os.path.join(self.base, "b"), ROOT_ID, [self.workspace])
        self.assertNotEqual(first["session"], second["session"])
        self.assertRegex(first["session"], r"^[0-9a-f]{16}$")

    def test_an_expired_capability_never_spawns(self):
        record, secret = self.keyring.mint(
            self.store, ROOT_ID, self.root.session, "briefly", [registry.SPAWN],
            ttl=10.0, now=self.clock() - 100.0,
        )
        self.drop(cap_id=record["cap_id"], secret=secret)
        self.watcher().pass_once()
        self.assertEqual(self.rejection()["reason"], "capability_expired")
        self.assertEqual(self.calls("pane", "split"), [])


class BackpressureTest(WatcherFixture):
    def test_a_request_over_the_quota_queues_instead_of_spawning(self):
        self.drop(key="k1")
        self.drop(key="k2", name="reviewer-2")
        self.watcher(max_panes=1).pass_once()
        self.assertEqual(len(self.calls("pane", "split")), 1)
        self.assertEqual(
            registry.load_json(self.root.record(registry.HEARTBEAT, "k2"))["state"],
            registry.QUEUED,
        )
        self.assertIsNone(self.rejection("k2"))

    def test_a_queued_request_spawns_once_capacity_frees_up(self):
        self.drop(key="k1")
        self.drop(key="k2", name="reviewer-2")
        watcher = self.watcher(max_panes=1)
        watcher.pass_once()
        state = self.herdr_state()
        state["agents"] = {}
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        watcher.pass_once()
        self.assertEqual(len(self.calls("pane", "split")), 2)

    def test_starvation_past_the_queue_timeout_fails_loudly(self):
        self.drop()
        watcher = self.watcher(max_panes=0, queue_timeout=30.0)
        watcher.pass_once()
        self.assertIsNone(self.rejection())
        self.clock.now += 31.0
        watcher.pass_once()
        rejection = self.rejection()
        self.assertEqual(rejection["reason"], "quota_timeout")
        self.assertTrue(rejection["no_native_fallback"])

    def drop_many(self, count, prefix="flood"):
        for index in range(count):
            self.drop(key=f"{prefix}{index}", name=f"w{index}",
                      nonce=f"{index + 1:x}-{index:016x}")

    def saturation(self, watcher):
        return [event for event in watcher.events if event["event"] == "saturated"]

    def test_a_flood_of_queued_requests_stops_at_the_outstanding_cap(self):
        self.drop_many(6)
        watcher = self.watcher(max_panes=0, max_outstanding=3)
        watcher.pass_once()
        queued = [
            name for name in os.listdir(self.root.dir(registry.HEARTBEAT))
            if name.endswith(".json")
        ]
        self.assertEqual(len(queued), 3)
        self.assertEqual(self.saturation(watcher)[0]["reason"], "too_many_outstanding")

    def test_one_capability_cannot_hold_every_outstanding_slot(self):
        self.drop_many(4)
        watcher = self.watcher(max_panes=0, max_outstanding=100,
                               max_outstanding_per_capability=2)
        watcher.pass_once()
        queued = [
            name for name in os.listdir(self.root.dir(registry.HEARTBEAT))
            if name.endswith(".json")
        ]
        self.assertEqual(len(queued), 2)
        self.assertEqual(self.saturation(watcher)[0]["reason"], "capability_outstanding")

    def test_a_full_drop_dir_is_refused_without_writing_a_record_per_key(self):
        self.drop_many(5)
        watcher = self.watcher(max_request_files=3)
        watcher.pass_once()
        self.assertEqual(self.saturation(watcher)[0]["reason"], "too_many_request_files")
        # The host must not amplify a flood into host-side storage.
        self.assertEqual(os.listdir(self.root.dir(registry.REJECTED)), [])
        self.assertEqual(os.listdir(self.root.dir(registry.HEARTBEAT)), [])
        self.assertEqual(self.calls("pane", "split"), [])

    def test_an_oversized_drop_dir_is_refused_by_byte_budget(self):
        self.drop_many(3)
        watcher = self.watcher(max_request_dir_bytes=10)
        watcher.pass_once()
        self.assertEqual(self.saturation(watcher)[0]["reason"], "request_dir_too_large")
        self.assertEqual(os.listdir(self.root.dir(registry.REJECTED)), [])

    def test_a_capability_over_its_admission_rate_is_refused(self):
        self.drop_many(4)
        watcher = self.watcher(max_panes=0, rate_limit=2, rate_window=60.0)
        watcher.pass_once()
        reasons = sorted(
            (registry.load_json(self.root.record(registry.REJECTED, key[:-len(".json")]))
             or {}).get("reason")
            for key in os.listdir(self.root.dir(registry.REJECTED)) if key.endswith(".json")
        )
        self.assertEqual(reasons, ["rate_limited", "rate_limited"])

    def test_the_rate_window_rolls_forward(self):
        self.drop_many(2)
        watcher = self.watcher(max_panes=0, rate_limit=1, rate_window=60.0)
        watcher.pass_once()
        self.assertEqual(self.rejection("flood1")["reason"], "rate_limited")
        self.drop(key="later", name="later")
        self.clock.now += 61.0
        watcher.pass_once()
        self.assertIsNone(self.rejection("later"))

    def test_a_refused_request_input_is_reclaimed_once_it_ages_out(self):
        self.drop(cwd="/etc")
        watcher = self.watcher(retention=60.0)
        watcher.pass_once()
        self.assertTrue(os.path.exists(self.request_path("k1")))

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertFalse(os.path.exists(self.request_path("k1")))
        self.assertFalse(os.path.exists(self.root.record(registry.HEARTBEAT, "k1")))
        # The rejection stays: it is the durable trace of what the host decided.
        self.assertEqual(self.rejection()["reason"], "bad_cwd:outside_allowlist")
        self.assertTrue(registry.chain(self.root, "k1")["intact"])

    def test_reclaim_never_touches_a_live_pane_or_its_provenance(self):
        self.drop()
        watcher = self.watcher(retention=0.0)
        watcher.pass_once()
        self.clock.now += 1000.0
        watcher.pass_once()
        for kind in (registry.CLAIMS, registry.ATTESTATIONS, registry.PANES,
                     registry.HEARTBEAT):
            self.assertTrue(os.path.exists(self.root.record(kind, "k1")), kind)
        self.assertEqual(self.heartbeat_state("k1"), registry.RUNNING)

    def test_queued_work_still_times_out_while_the_outstanding_cap_is_full(self):
        # The reviewer's trace: max_panes=0, max_outstanding=1, one queued key, clock past
        # the queue timeout. The full cap must not stop it reaching admission().
        self.drop()
        watcher = self.watcher(max_panes=0, max_outstanding=1, queue_timeout=30.0)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.QUEUED)

        self.clock.now += 31.0
        watcher.pass_once()
        self.assertEqual(self.rejection()["reason"], "quota_timeout")

    def test_queued_work_still_spawns_when_capacity_frees_under_a_full_cap(self):
        self.drop()
        watcher = self.watcher(max_panes=0, max_outstanding=1)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.QUEUED)

        watcher.max_panes = 1
        watcher.pass_once()
        self.assertEqual(len(self.calls("pane", "split")), 1)
        self.assertIsNotNone(self.pane_record())

    def test_a_queued_request_becoming_a_pane_keeps_holding_its_outstanding_slot(self):
        # The reviewer's trace: queue k1 under max_panes=0/max_outstanding=1, leave k2
        # pending, then free two pane slots. Draining k1 must not release the one slot the
        # global cap is made of, or k2 sneaks in behind it.
        self.drop(key="k1")
        self.drop(key="k2", name="reviewer-2")
        watcher = self.watcher(max_panes=0, max_outstanding=1)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.QUEUED)
        self.assertIsNone(self.heartbeat_state("k2"))

        watcher.max_panes = 2
        watcher.pass_once()
        self.assertEqual(len(self.calls("agent", "start")), 1,
                         "a global cap of one may never yield two running panes")
        self.assertIsNotNone(self.pane_record("k1"))
        self.assertIsNone(self.pane_record("k2"))

    def test_deleting_an_in_tree_claim_cannot_release_a_live_pane_slot(self):
        # The reviewer's repro: spawn k1, delete claims/k1.json from inside the
        # sandbox-writable root, then submit k2 under a global cap of one.
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=1)
        watcher.pass_once()
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1)

        os.unlink(self.root.record(registry.CLAIMS, "k1"))
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1,
                         "the ledger is host-only; the in-tree claim is a mirror")

        self.drop(key="k2", name="reviewer-2")
        watcher.pass_once()
        self.assertIsNone(self.pane_record("k2"))
        self.assertEqual(len(self.calls("agent", "start")), 1,
                         "a global cap of one may never yield two live agents")

    def test_deleting_the_whole_registry_tree_cannot_release_slots(self):
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=1)
        watcher.pass_once()
        for kind in (registry.CLAIMS, registry.PANES, registry.HEARTBEAT,
                     registry.ATTESTATIONS):
            for name in os.listdir(self.root.dir(kind)):
                os.unlink(self.root.dir(kind, name))
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1)

    def test_the_ledger_lives_outside_the_sandbox_writable_root(self):
        self.drop(key="k1")
        watcher = self.watcher()
        watcher.pass_once()
        entries = watcher.ledger.entries()
        self.assertEqual([entry["key"] for entry in entries], ["k1"])
        self.assertFalse(watcher.ledger.directory.startswith(self.root.path + os.sep))
        self.assertTrue(all(entry["authentic"] for entry in entries))

    def test_a_tampered_reservation_still_counts_against_the_quota(self):
        self.drop(key="k1")
        watcher = self.watcher()
        watcher.pass_once()
        path = watcher.ledger.path("k1")
        record = json.loads(pathlib.Path(path).read_text())
        record["capability"] = "cap-" + "9" * 16
        pathlib.Path(path).write_text(json.dumps(record))

        outstanding, per_capability = watcher.outstanding_counts(watcher.host.agents())
        self.assertEqual(outstanding, 1, "failing closed on the quota is the safe direction")
        self.assertEqual(per_capability, {registry.Ledger.UNKNOWN_CAPABILITY: 1})

    def test_a_reservation_whose_heartbeat_was_lost_still_drains(self):
        # An ordinary crash between the two writes. Recovery used to consult the heartbeat,
        # so the key looked fresh, its own reservation filled the cap, and it never reached
        # admission again — no timeout, no agent, stuck across restarts.
        self.drop()
        watcher = self.watcher(max_panes=0, max_outstanding=1, queue_timeout=30.0)
        watcher.pass_once()
        self.assertEqual(watcher.ledger.get("k1")["state"], registry.QUEUED)
        os.unlink(self.root.record(registry.HEARTBEAT, "k1"))

        self.assertTrue(watcher.is_queued("k1"), "the ledger answers this, not the mirror")
        self.clock.now += 31.0
        watcher.pass_once()
        self.assertEqual(self.rejection()["reason"], "quota_timeout")

    def test_a_reservation_whose_heartbeat_was_lost_still_spawns(self):
        self.drop()
        watcher = self.watcher(max_panes=0, max_outstanding=1)
        watcher.pass_once()
        os.unlink(self.root.record(registry.HEARTBEAT, "k1"))

        watcher.max_panes = 1
        watcher.pass_once()
        self.assertIsNotNone(self.pane_record(), "a queued key must not be gated by its own slot")

    def test_a_reservation_with_no_claim_is_released_at_startup(self):
        # The other torn shape: reservation written, crash before the claim. Nothing was
        # published and nothing spawned, so the slot is not owed to anyone.
        watcher = self.watcher()
        watcher.ledger.reserve("torn", self.record["cap_id"], registry.RUNNING,
                               agent_name="rrun40f-torn")
        self.assertEqual(watcher.outstanding_counts({})[0], 1)

        watcher.reconcile_ledger({})
        self.assertIsNone(watcher.ledger.get("torn"))
        self.assertEqual(watcher.outstanding_counts({})[0], 0)

    def test_a_claim_with_no_reservation_is_terminally_refused(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        # Simulate the pre-fix ordering's leftover: a claim nothing reserved.
        watcher.ledger.release("k1")
        os.unlink(self.root.record(registry.PANES, "k1"))

        watcher.reconcile_claims(watcher.host.agents())
        self.assertEqual(self.rejection()["reason"], "watcher_restart_incomplete")
        self.assertEqual(self.rejection()["detail"], "no_reservation")

    def test_a_healthy_claim_is_never_refused_by_claim_reconciliation(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        watcher.reconcile_claims(watcher.host.agents())
        self.assertIsNone(self.rejection())
        self.assertIsNotNone(self.pane_record())

    def test_a_malformed_reservation_holds_its_slot_instead_of_crashing(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        # Truncated or upgrade-leftover file: the fail-closed branch used to raise
        # NameError and stop quota accounting entirely.
        pathlib.Path(watcher.ledger.path("k1")).write_text("{not json")

        entries = watcher.ledger.entries()
        self.assertEqual(entries[0]["state"], registry.Ledger.MALFORMED)
        self.assertFalse(entries[0]["authentic"])
        outstanding, per_capability = watcher.outstanding_counts(watcher.host.agents())
        self.assertEqual(outstanding, 1)
        self.assertEqual(per_capability, {registry.Ledger.UNKNOWN_CAPABILITY: 1})

    def test_a_non_object_reservation_holds_its_slot(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        pathlib.Path(watcher.ledger.path("k1")).write_text("[]")
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1)

    def test_abandoned_client_temps_are_swept_before_they_wedge_the_dir(self):
        # A retry loop that dies between the write and the rename leaves dot-temps. Request
        # processing skips them, but the drop-dir budget counts every entry.
        drop_dir = self.root.dir(registry.REQUESTS)
        for index in range(3):
            stale = pathlib.Path(drop_dir, f".stale{index}.999.tmp")
            stale.write_text("{}")
            os.utime(stale, (self.clock.now, self.clock.now))
        self.drop()
        watcher = self.watcher(max_request_files=2, temp_retention=60.0)
        watcher.pass_once()
        self.assertEqual(self.saturation(watcher)[0]["reason"], "too_many_request_files")

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertEqual(
            [name for name in os.listdir(drop_dir) if name.startswith(".")], []
        )
        self.assertIsNotNone(self.pane_record(), "the drop dir must recover on its own")

    def test_a_fresh_client_temp_is_left_alone(self):
        drop_dir = self.root.dir(registry.REQUESTS)
        inflight = pathlib.Path(drop_dir, ".inflight.999.tmp")
        inflight.write_text("{}")
        os.utime(inflight, (self.clock.now, self.clock.now))
        watcher = self.watcher(temp_retention=600.0)
        watcher.pass_once()
        self.assertTrue(os.path.exists(os.path.join(drop_dir, ".inflight.999.tmp")))

    def test_startup_reconciliation_releases_only_provably_finished_work(self):
        self.drop(key="k1")
        watcher = self.watcher()
        watcher.pass_once()
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1)

        watcher.reconcile_ledger(watcher.host.agents())
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1,
                         "a live agent keeps its slot")

        state = self.herdr_state()
        state["agents"] = {}
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        watcher.reconcile_ledger({})
        self.assertEqual(watcher.outstanding_counts({})[0], 0)

    def test_an_orphaned_live_pane_keeps_holding_its_outstanding_slot(self):
        # The reviewer's repro: force provenance and closure to fail so k1 ends up orphaned
        # with a live agent, then check k2 cannot slip past a global cap of one.
        self.herdr_flag(no_session=True, no_process_group=True, fail_close=True)
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=1)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)
        self.assertEqual(len(self.herdr_state()["agents"]), 1)

        self.drop(key="k2", name="reviewer-2")
        watcher.pass_once()
        self.assertIsNone(self.pane_record("k2"))
        self.assertEqual(len(self.herdr_state()["agents"]), 1,
                         "an orphan with a live agent must not free a slot")
        self.assertEqual(
            [event["reason"] for event in self.saturation(watcher)], ["too_many_outstanding"]
        )

    def test_an_orphaned_live_pane_counts_against_its_own_capability(self):
        self.herdr_flag(no_session=True, no_process_group=True, fail_close=True)
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=100, max_outstanding_per_capability=1)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)

        self.drop(key="k2", name="reviewer-2")
        watcher.pass_once()
        self.assertIsNone(self.pane_record("k2"))
        self.assertEqual(
            [event["reason"] for event in self.saturation(watcher)], ["capability_outstanding"]
        )

    def test_an_unsettled_claim_mid_flight_holds_a_slot(self):
        self.herdr_flag(no_session=True, no_process_group=True, fail_close=True)
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=1)
        watcher.pass_once()
        outstanding, per_capability = watcher.outstanding_counts(watcher.host.agents())
        self.assertEqual(outstanding, 1)
        self.assertEqual(per_capability[self.record["cap_id"]], 1)

    def test_a_resolved_orphan_releases_its_slot(self):
        self.herdr_flag(no_session=True, no_process_group=True, fail_close=True)
        self.drop(key="k1")
        watcher = self.watcher(max_outstanding=1)
        watcher.pass_once()

        # An operator resolving the orphan is the only thing that frees it.
        self.herdr_flag(fail_close=False)
        watcher.pass_once()
        self.assertEqual(self.rejection("k1")["reason"], "provenance_unprovable")

        self.herdr_flag(no_session=False, no_process_group=False)
        self.drop(key="k2", name="reviewer-2")
        watcher.pass_once()
        self.assertIsNotNone(self.pane_record("k2"))

    def test_queued_work_still_drains_under_a_full_per_capability_cap(self):
        self.drop()
        watcher = self.watcher(max_panes=0, max_outstanding_per_capability=1,
                               queue_timeout=30.0)
        watcher.pass_once()
        self.assertEqual(self.heartbeat_state("k1"), registry.QUEUED)

        self.clock.now += 31.0
        watcher.pass_once()
        self.assertEqual(self.rejection()["reason"], "quota_timeout")

    def test_successful_request_inputs_are_reclaimed_so_the_dir_cannot_wedge(self):
        self.drop()
        watcher = self.watcher(retention=60.0)
        watcher.pass_once()
        self.assertTrue(os.path.exists(self.request_path("k1")))

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertFalse(os.path.exists(self.request_path("k1")),
                         "a claimed input must not pin a drop-dir inode forever")
        # The pane, its heartbeat and the committed digest all survive.
        self.assertIsNotNone(self.pane_record())
        self.assertEqual(self.heartbeat_state("k1"), registry.RUNNING)
        self.assertTrue(registry.chain(self.root, "k1")["intact"])

    def test_a_long_lived_watcher_keeps_accepting_after_the_file_budget_of_successes(self):
        watcher = self.watcher(max_request_files=3, retention=0.0)
        for index in range(5):
            self.drop(key=f"run{index}", name=f"w{index}")
            watcher.pass_once()
            self.assertIsNotNone(self.pane_record(f"run{index}"), f"request {index} wedged")
            state = self.herdr_state()
            state["agents"] = {}
            with open(self.state, "w") as handle:
                json.dump(state, handle)
        self.assertEqual(len(self.calls("agent", "start")), 5)

    def test_saturation_is_reported_once_per_episode_not_once_per_pass(self):
        self.drop_many(5)
        watcher = self.watcher(max_request_files=3)
        for _ in range(3):
            watcher.pass_once()
        self.assertEqual(len(self.saturation(watcher)), 1)

    def test_saturation_clears_when_the_pressure_does(self):
        self.drop_many(5)
        watcher = self.watcher(max_request_files=3)
        watcher.pass_once()
        for index in range(3):
            os.unlink(self.request_path(f"flood{index}"))
        watcher.pass_once()
        self.assertEqual([event["event"] for event in watcher.events
                          if event["event"] in ("saturated", "drained")][:2],
                         ["saturated", "drained"])

    def test_an_unusable_filename_is_logged_once_not_once_per_pass(self):
        pathlib.Path(self.root.dir(registry.REQUESTS), "not a key!.json").write_text("{}")
        watcher = self.watcher()
        watcher.pass_once()
        watcher.pass_once()
        watcher.pass_once()
        ignored = [event for event in watcher.events if event["event"] == "ignored"]
        self.assertEqual(len(ignored), 1)


class ReconciliationTest(WatcherFixture):
    def test_a_live_agent_is_adopted_after_a_watcher_restart(self):
        self.drop()
        first = self.watcher()
        first.pass_once()
        os.unlink(self.root.record(registry.PANES, "k1"))
        os.unlink(self.root.record(registry.HEARTBEAT, "k1"))

        second = self.watcher()
        second.reconcile(second.host.agents())
        pane = self.pane_record()
        self.assertTrue(pane["provenance"]["adopted"])
        self.assertEqual(pane["pane_id"], "wT:p1")
        self.assertEqual(len(self.calls("pane", "split")), 1, "adoption never re-spawns")

    def test_a_claim_with_no_live_agent_fails_closed_rather_than_respawning(self):
        self.drop()
        first = self.watcher()
        first.pass_once()
        os.unlink(self.root.record(registry.PANES, "k1"))
        state = self.herdr_state()
        state["agents"] = {}
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertEqual(self.rejection()["reason"], "watcher_restart_incomplete")
        self.assertEqual(len(self.calls("pane", "split")), 1)

    def lose_the_pane_record(self):
        os.unlink(self.root.record(registry.PANES, "k1"))
        for path in (self.root.record(registry.HEARTBEAT, "k1"),):
            if os.path.exists(path):
                os.unlink(path)

    def refusal_detail(self, key="k1"):
        return self.rejection(key).get("detail")

    def lose_the_attestation(self):
        """The crash-between-spawn-and-attestation state: a live pane nothing can bind."""
        self.lose_the_pane_record()
        os.unlink(self.root.record(registry.ATTESTATIONS, "k1"))

    def test_a_claim_with_no_attestation_is_never_adopted(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_attestation()

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertIsNone(self.pane_record())
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)

    def test_an_agent_that_squatted_the_predictable_name_is_not_adopted(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_pane_record()

        # Same name, different occupant: the whole point is that the name proves nothing.
        state = self.herdr_state()
        state["agents"]["rrun40f-reviewer-1"]["agent_session"] = {"value": "sess-impostor"}
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertIsNone(self.pane_record())
        self.assertEqual(self.refusal_detail(), "occupant_mismatch")

    def test_an_agent_that_moved_to_another_pane_is_not_adopted(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_pane_record()
        state = self.herdr_state()
        state["agents"]["rrun40f-reviewer-1"]["pane_id"] = "wT:p99"
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertEqual(self.refusal_detail(), "pane_mismatch")

    def test_an_unattested_claim_is_parked_for_a_human_never_closed(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_attestation()

        second = self.watcher()
        second.reconcile(second.host.agents())
        # Closing on the claim name alone is the same co-occurrence adoption refuses,
        # used for a kill. It must not happen.
        self.assertEqual(self.calls("pane", "close"), [])
        self.assertIn("rrun40f-reviewer-1", self.herdr_state()["agents"])
        self.assertEqual(
            [event["event"] for event in second.events if event["event"].startswith("orphan")],
            ["orphan_needs_attention"],
        )

    def test_an_unattested_claim_never_closes_an_impostor(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_attestation()
        # The reviewer's repro: someone else now holds the predictable name.
        state = self.herdr_state()
        state["agents"]["rrun40f-reviewer-1"]["agent_session"] = {"value": "sess-impostor"}
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertEqual(self.calls("pane", "close"), [], "an innocent pane must survive")
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)

    def test_an_orphaned_claim_stays_reconcilable_rather_than_terminally_rejected(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        self.lose_the_attestation()
        for _ in range(3):
            watcher.pass_once()
        self.assertIsNone(self.rejection(), "a terminal record would make it unreconcilable")
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)
        # Loud once, not once per pass.
        self.assertEqual(
            len([e for e in watcher.events if e["event"] == "orphan_needs_attention"]), 1
        )

    def test_a_failed_close_leaves_the_key_reconcilable_and_retries(self):
        self.herdr_flag(no_session=True, no_process_group=True, fail_close=True)
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        # Unprovable provenance, and the close did not work: settling here would strand a
        # live pane behind a terminal record forever.
        self.assertIsNone(self.rejection())
        self.assertIsNone(self.pane_record())
        self.assertEqual(self.heartbeat_state("k1"), registry.ORPHANED)

        self.herdr_flag(fail_close=False)
        watcher.pass_once()
        self.assertEqual(self.rejection()["reason"], "provenance_unprovable")
        self.assertEqual(self.rejection()["closed_pane"], "wT:p1")

    def test_an_attested_pane_belonging_to_someone_else_is_never_closed(self):
        self.drop()
        self.watcher().pass_once()
        self.lose_the_pane_record()
        state = self.herdr_state()
        state["agents"]["rrun40f-reviewer-1"]["agent_session"] = {"value": "sess-impostor"}
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertEqual(self.refusal_detail(), "occupant_mismatch")
        self.assertEqual(self.calls("pane", "close"), [], "an innocent pane must survive")

    def test_adoption_falls_back_to_the_process_group_when_there_is_no_session(self):
        self.herdr_flag(no_session=True)
        self.drop()
        self.watcher().pass_once()
        self.lose_the_pane_record()

        second = self.watcher()
        second.reconcile(second.host.agents())
        self.assertTrue(self.pane_record()["provenance"]["adopted"])

    def test_heartbeats_report_a_pane_that_disappeared(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        state = self.herdr_state()
        state["agents"] = {}
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        watcher.pass_once()
        beat = registry.load_json(self.root.record(registry.HEARTBEAT, "k1"))
        self.assertEqual(beat["state"], registry.GONE)


class ControlBusTest(WatcherFixture):
    def spawn(self):
        self.drop()
        watcher = self.watcher()
        watcher.pass_once()
        return watcher

    def message(self, kind, seq, document, key="k1"):
        path = os.path.join(self.root.dir(kind, key), f"{seq}.json")
        temporary = f"{path}.tmp"
        pathlib.Path(temporary).write_text(json.dumps(document))
        os.rename(temporary, path)
        # Age is measured from the file's own mtime, so the fake clock has to drive it.
        os.utime(path, (self.clock.now, self.clock.now))
        return path

    def result(self, kind, seq, key="k1"):
        return registry.load_json(os.path.join(self.root.dir(kind, key), f"{seq}.result.json"))

    def test_an_inbox_message_is_relayed_to_the_pane(self):
        watcher = self.spawn()
        self.message(registry.INBOX, "1", {"text": "do the work"})
        watcher.pass_once()
        prompts = self.calls("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2:4], ["rrun40f-reviewer-1", "do the work"])
        result = self.result(registry.INBOX, "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "settled")

    def test_a_relayed_message_is_never_delivered_twice(self):
        watcher = self.spawn()
        self.message(registry.INBOX, "1", {"text": "once"})
        watcher.pass_once()
        watcher.pass_once()
        self.assertEqual(len(self.calls("agent", "prompt")), 1)

    def test_a_stalled_submission_is_reported_rather_than_assumed_delivered(self):
        watcher = self.spawn()
        state = self.herdr_state()
        state["prompt_outcome"] = registry.STALL_MARKER
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.message(registry.INBOX, "1", {"text": "hello"})
        watcher.pass_once()
        result = self.result(registry.INBOX, "1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["outcome"], "stalled")

    def test_a_submission_timeout_means_the_turn_is_still_running(self):
        watcher = self.spawn()
        state = self.herdr_state()
        state["prompt_outcome"] = "timeout"
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.message(registry.INBOX, "1", {"text": "hello"})
        watcher.pass_once()
        result = self.result(registry.INBOX, "1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["outcome"], "submitted")

    def test_a_working_occupant_is_never_prompted(self):
        watcher = self.spawn()
        state = self.herdr_state()
        for entry in state["agents"].values():
            entry["agent_status"] = "working"
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.message(registry.INBOX, "1", {"text": "stack a turn"})
        watcher.pass_once()
        self.assertEqual(self.calls("agent", "prompt"), [])
        self.assertIsNone(self.result(registry.INBOX, "1"))

    def test_a_blocked_occupant_is_reported_instead_of_prompted(self):
        watcher = self.spawn()
        state = self.herdr_state()
        for entry in state["agents"].values():
            entry["agent_status"] = "blocked"
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.message(registry.INBOX, "1", {"text": "answer"})
        watcher.pass_once()
        self.assertEqual(self.result(registry.INBOX, "1")["outcome"], "agent_blocked")

    def test_a_malformed_message_is_refused_without_reaching_the_pane(self):
        watcher = self.spawn()
        self.message(registry.INBOX, "1", {"text": "   "})
        watcher.pass_once()
        self.assertEqual(self.result(registry.INBOX, "1")["reason"], "bad_text")
        self.assertEqual(self.calls("agent", "prompt"), [])

    def test_cancel_closes_the_pane(self):
        watcher = self.spawn()
        self.message(registry.CONTROL, "1", {"action": "cancel"})
        watcher.pass_once()
        self.assertEqual(self.calls("pane", "close")[0][2], "wT:p1")
        self.assertTrue(self.result(registry.CONTROL, "1")["ok"])
        self.assertEqual(
            registry.load_json(self.root.record(registry.HEARTBEAT, "k1"))["state"],
            registry.CANCELLED,
        )

    def test_interrupt_sends_ctrl_c(self):
        watcher = self.spawn()
        self.message(registry.CONTROL, "1", {"action": "interrupt"})
        watcher.pass_once()
        self.assertEqual(self.calls("agent", "send-keys")[0][2:], ["rrun40f-reviewer-1", "ctrl-c"])

    def test_an_unknown_action_is_refused(self):
        watcher = self.spawn()
        self.message(registry.CONTROL, "1", {"action": "rm -rf /"})
        watcher.pass_once()
        self.assertEqual(self.result(registry.CONTROL, "1")["reason"], "bad_action")


class ProvenanceTest(WatcherFixture):
    def test_a_request_rewritten_after_the_claim_breaks_the_chain(self):
        self.drop()
        self.watcher().pass_once()
        tampered = request_document(idempotency_key="k1", cwd=self.workspace, name="other")
        pathlib.Path(self.request_path("k1")).write_text(json.dumps(tampered))
        report = registry.chain(self.root, "k1")
        self.assertFalse(report["intact"])
        self.assertIn("request_rewritten_after_claim", report["breaks"])

    def test_a_pane_whose_agent_died_is_not_reported_as_intact_under_live_check(self):
        self.drop()
        self.watcher().pass_once()
        report = registry.chain(self.root, "k1", agents={})
        self.assertIn("agent_not_live", report["breaks"])

    def test_a_pane_with_no_process_evidence_is_not_intact(self):
        self.herdr_flag(no_process_group=True)
        self.drop()
        self.watcher().pass_once()
        report = registry.chain(self.root, "k1")
        self.assertFalse(report["intact"])
        self.assertIn("pane_process_missing", report["breaks"])

    def test_an_unprovable_spawn_is_refused_and_its_pane_closed_not_published(self):
        self.herdr_flag(no_session=True, no_process_group=True)
        self.drop()
        self.watcher().pass_once()

        # The reviewer's repro produced pane-published=True with a broken chain. It must
        # now produce no pane record at all, a refusal, and a closed pane.
        self.assertIsNone(self.pane_record())
        self.assertIsNone(registry.load_json(self.root.record(registry.ATTESTATIONS, "k1")))
        self.assertEqual(self.rejection()["reason"], "provenance_unprovable")
        self.assertEqual(self.calls("pane", "close")[0][2], "wT:p1")
        self.assertTrue(registry.chain(self.root, "k1")["intact"])

    def test_a_pane_with_no_attestation_at_all_is_not_intact(self):
        self.drop()
        self.watcher().pass_once()
        os.unlink(self.root.record(registry.ATTESTATIONS, "k1"))
        report = registry.chain(self.root, "k1")
        self.assertFalse(report["intact"])
        self.assertIn("attestation_missing", report["breaks"])

    def test_a_pane_whose_occupant_was_replaced_is_not_intact(self):
        self.drop()
        self.watcher().pass_once()
        agents = {"rrun40f-reviewer-1": {"name": "rrun40f-reviewer-1", "pane_id": "wT:p1",
                                         "agent_session": {"value": "sess-impostor"}}}
        report = registry.chain(self.root, "k1", agents=agents)
        self.assertIn("live_occupant_mismatch", report["breaks"])

    def test_a_pane_whose_attestation_carries_no_identity_is_not_intact(self):
        self.drop()
        self.watcher().pass_once()
        # A record shaped like one an older watcher could have written: attested, but with
        # nothing in it to check. The chain must not call that intact.
        path = self.root.record(registry.ATTESTATIONS, "k1")
        hollow = dict(registry.load_json(path), occupant=None, process_group=None)
        os.unlink(path)
        pathlib.Path(path).write_text(json.dumps(hollow))
        report = registry.chain(self.root, "k1")
        self.assertFalse(report["intact"])
        self.assertIn("worker_identity_unproven", report["breaks"])

    def test_a_rejected_request_has_no_pane_but_still_resolves(self):
        self.drop(cwd="/etc")
        self.watcher().pass_once()
        report = registry.chain(self.root, "k1")
        self.assertIsNone(report["pane"])
        self.assertEqual(report["rejected"]["reason"], "bad_cwd:outside_allowlist")
        # A clean refusal is a terminated chain, not a broken one: the chair must be able
        # to tell "the host said no" from "the host lost track of a worker".
        self.assertTrue(report["intact"], report["breaks"])

    def test_a_request_with_no_decision_yet_is_reported_as_undecided(self):
        self.drop()
        report = registry.chain(self.root, "k1")
        self.assertFalse(report["intact"])
        self.assertIn("undecided", report["breaks"])


class FamilyBlindnessTest(WatcherFixture):
    """The proof the drop-box is not a convention: no herdr CLI, no agent, just rename(2)."""

    def test_a_plain_shell_drop_produces_a_visible_pane(self):
        document = signed(request_document(
            idempotency_key="shellkey",
            capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace,
            name="from-shell",
        ), self.secret)
        script = (
            'set -e; '
            'printf "%s" "$REQUEST_JSON" > "$DROP/.staging"; '
            'mv "$DROP/.staging" "$DROP/shellkey.1-000000000000beef.json"'
        )
        # A shell with no herdr binary on PATH, no herdr environment, and no agent in the
        # loop: rename(2) is the entire client-side capability being exercised.
        shell_env = {
            "PATH": "/usr/bin:/bin",
            "REQUEST_JSON": json.dumps(document),
            "DROP": self.root.dir(registry.REQUESTS),
        }
        dropped = subprocess.run(["/bin/sh", "-c", script], env=shell_env,
                                 capture_output=True, text=True)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)

        watched = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "watch",
             "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
             "--master-key", "env", "--once"],
            capture_output=True, text=True, env=dict(os.environ),
        )
        self.assertEqual(watched.returncode, 0, watched.stderr)

        pane = self.pane_record("shellkey")
        self.assertIsNotNone(pane, watched.stdout + watched.stderr)
        self.assertEqual(pane["agent_name"], "rrun40f-from-shell")
        self.assertEqual(len(self.calls("pane", "split")), 1)
        self.assertEqual(len(self.calls("agent", "start")), 1)
        self.assertTrue(registry.chain(self.root, "shellkey")["intact"])

    def test_a_second_watcher_refuses_to_share_a_root(self):
        command = [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "watch",
                   "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
                   "--master-key", "env"]
        holder = subprocess.Popen(command + ["--interval", "5"], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=dict(os.environ))
        try:
            holder.stdout.readline()
            second = subprocess.run(command + ["--once"], capture_output=True, text=True,
                                    env=dict(os.environ))
            self.assertEqual(second.returncode, 2)
            self.assertIn("another watcher already owns", second.stderr)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()


class ClientTest(WatcherFixture):
    def test_request_wait_fails_on_a_pane_whose_chain_is_broken(self):
        self.drop(key="broken", name="broken")
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("broken"))
        # A pane FILE exists; its provenance does not check out.
        os.unlink(self.root.record(registry.ATTESTATIONS, "broken"))

        waited = self.registry_command("request", "claude", "broken", "--key", "broken",
                                       "--cwd", self.workspace, "--wait", "5")
        self.assertEqual(waited.returncode, 5)
        self.assertIn("attestation_missing", waited.stderr)
        self.assertIn("Do NOT fall back", waited.stderr)

    def test_request_wait_succeeds_only_on_an_intact_chain(self):
        self.drop(key="whole", name="whole")
        self.watcher().pass_once()
        waited = self.registry_command("request", "claude", "whole", "--key", "whole",
                                       "--cwd", self.workspace, "--wait", "5")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertIn("rrun40f-whole", waited.stdout)

    def test_two_clients_racing_one_key_never_clobber_each_other(self):
        # Both clients pass their existence checks and both rename. Unique per-submission
        # names mean neither can land on the other's file, so the bytes the host digested
        # stay exactly as they were and the chain survives an ordinary retry race.
        environment = self.client_environment()
        drop_dir = self.root.dir(registry.REQUESTS)
        first = self.registry_command("request", "claude", "racer", "--key", "race",
                                      "--cwd", self.workspace, environment=environment)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = pathlib.Path(registry.submissions(drop_dir, "race")[0]).read_text()
        self.watcher().pass_once()

        # A delayed client that never saw the first submission publishes its own.
        self.place("race", signed(request_document(
            idempotency_key="race", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="racer", family="codex",
        ), self.secret), nonce="3-00000000dddd0001")

        claimed = registry.load_json(self.root.record(registry.CLAIMS, "race"))
        self.assertEqual(pathlib.Path(claimed["request_path"]).read_text(), original)
        self.assertTrue(registry.chain(self.root, "race")["intact"])
        self.assertEqual(len(self.calls("agent", "start")), 1)

    def test_a_later_lower_named_sibling_cannot_inherit_a_queued_slot(self):
        """The reviewer's trace: pids are variable width, so a later retry can sort first."""
        other, other_secret = self.keyring.mint(
            self.store, ROOT_ID, self.root.session, "other", [registry.SPAWN]
        )
        # `ffff-...` sorts after `10000-...`, so the original is NOT the lexical winner.
        self.place("switch", signed(request_document(
            idempotency_key="switch", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="first",
        ), self.secret), nonce="ffff-000000000000aaaa")
        watcher = self.watcher(max_panes=0)
        watcher.pass_once()
        reservation = watcher.ledger.get("switch")
        self.assertEqual(reservation["state"], registry.QUEUED)
        self.assertEqual(reservation["capability"], self.record["cap_id"])

        # A confused retry under a different valid capability, lexically earlier.
        self.place("switch", signed(request_document(
            idempotency_key="switch", capability={"cap_id": other["cap_id"]},
            cwd=self.workspace, name="second",
        ), other_secret), nonce="10000-000000000000bbbb")

        watcher.max_panes = 1
        watcher.pass_once()
        claim = registry.load_json(self.root.record(registry.CLAIMS, "switch"))
        self.assertEqual(claim["requested_name"], "first",
                         "the slot belongs to the bytes that acquired it")
        self.assertEqual(claim["issuer_capability"], self.record["cap_id"])
        self.assertEqual(watcher.ledger.get("switch")["capability"], self.record["cap_id"])

    def test_a_bound_submission_is_never_reclaimed_out_from_under_its_slot(self):
        self.place("bound", signed(request_document(
            idempotency_key="bound", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="bound",
        ), self.secret), nonce="ffff-000000000000aaaa")
        watcher = self.watcher(max_panes=0, retention=60.0)
        watcher.pass_once()
        bound = watcher.ledger.get("bound")["request_path"]
        self.place("bound", signed(request_document(
            idempotency_key="bound", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="sibling",
        ), self.secret), nonce="10000-000000000000bbbb")

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertTrue(os.path.exists(bound), "the slot's own submission survives reclaim")
        self.assertEqual(
            len(registry.submissions(self.root.dir(registry.REQUESTS), "bound")), 1
        )

    def test_a_queued_slot_whose_submission_vanished_is_released(self):
        self.drop(key="ghost")
        watcher = self.watcher(max_panes=0)
        watcher.pass_once()
        self.assertEqual(watcher.outstanding_counts({})[0], 1)
        os.unlink(watcher.ledger.get("ghost")["request_path"])

        watcher.pass_once()
        self.assertIsNone(watcher.ledger.get("ghost"),
                          "a slot with nothing left to drain must not linger")

    def test_a_reservation_bound_to_another_capability_is_not_spent(self):
        self.drop(key="mine")
        watcher = self.watcher(max_panes=0)
        watcher.pass_once()
        # A legacy or hand-edited reservation naming a different owner.
        reservation = watcher.ledger.get("mine")
        watcher.ledger.reserve("mine", "cap-" + "9" * 16, registry.QUEUED,
                               queued_since=reservation["queued_since"],
                               request_path=reservation["request_path"])

        watcher.max_panes = 1
        watcher.pass_once()
        claim = registry.load_json(self.root.record(registry.CLAIMS, "mine"))
        self.assertEqual(claim["issuer_capability"], self.record["cap_id"])
        self.assertEqual(watcher.ledger.get("mine")["capability"], self.record["cap_id"])

    def test_identical_random_material_still_yields_distinct_names(self):
        # The reviewer forced two clients onto one nonce and the second rename replaced the
        # first. The pid is what makes concurrency non-colliding rather than merely
        # unlikely, so equal nonce material must still produce different files.
        first = registry.submission_name("k", 4242, "a" * 16)
        second = registry.submission_name("k", 4243, "a" * 16)
        self.assertNotEqual(first, second)
        self.assertEqual(registry.submission_key(first), "k")
        self.assertEqual(registry.submission_key(second), "k")

    def test_two_clients_with_pinned_nonces_do_not_clobber(self):
        shim = os.path.join(self.base, "fixednonce")
        os.makedirs(shim, exist_ok=True)
        pathlib.Path(shim, "sitecustomize.py").write_text(
            "import secrets\nsecrets.token_hex = lambda n=32: 'a' * (n * 2)\n"
        )
        environment = dict(self.client_environment(), PYTHONPATH=shim)
        for family in ("claude", "codex"):
            published = self.registry_command("request", family, "pinned", "--key", "pinned",
                                              "--cwd", self.workspace,
                                              environment=environment)
            self.assertEqual(published.returncode, 0, published.stderr)
            # Each run must publish; the second only skips if it saw the first.
            os.rename(registry.submissions(self.root.dir(registry.REQUESTS), "pinned")[0],
                      os.path.join(self.base, f"stash-{family}"))
        stashed = [pathlib.Path(self.base, f"stash-{f}").read_text()
                   for f in ("claude", "codex")]
        self.assertNotEqual(stashed[0], stashed[1], "both submissions survived independently")

    def test_a_second_submission_never_replaces_the_first(self):
        environment = self.client_environment()
        first = self.registry_command("request", "claude", "twin", "--key", "twin",
                                      "--cwd", self.workspace, environment=environment)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = pathlib.Path(self.request_path("twin")).read_text()

        # A different signed payload under the same key: the key is what is idempotent.
        second = self.registry_command("request", "codex", "twin", "--key", "twin",
                                       "--cwd", self.workspace, environment=environment)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(pathlib.Path(self.request_path("twin")).read_text(),
                         before, "first submission wins; the loser polls")

    def denying_environment(self):
        """A client environment where link/unlink/remove all fail with EPERM.

        Excluding a spelling from the source proves nothing about a capability; this runs
        the real client under a sandbox that permits exactly the two documented operations.
        """
        shim = os.path.join(self.base, "shim")
        os.makedirs(shim, exist_ok=True)
        pathlib.Path(shim, "sitecustomize.py").write_text(
            "import os\n"
            "def _denied(*a, **k):\n"
            "    raise PermissionError(1, 'Operation not permitted')\n"
            "os.link = _denied\n"
            "os.unlink = _denied\n"
            "os.remove = _denied\n"
        )
        return dict(self.client_environment(), PYTHONPATH=shim)

    def test_successful_publication_needs_no_permission_beyond_write_and_rename(self):
        environment = self.denying_environment()
        published = self.registry_command("request", "claude", "narrow", "--key", "narrow",
                                          "--cwd", self.workspace, environment=environment)
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(
            len(registry.submissions(self.root.dir(registry.REQUESTS), "narrow")), 1
        )
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("narrow"))

    def test_the_denial_shim_really_denies(self):
        # Guards the test above from silently passing because the shim did nothing.
        environment = self.denying_environment()
        probe = subprocess.run(
            [sys.executable, "-c",
             "import os,sys\n"
             "try:\n"
             "    os.unlink('/nonexistent')\n"
             "except PermissionError:\n"
             "    sys.exit(7)\n"
             "sys.exit(0)\n"],
            capture_output=True, text=True, env=environment,
        )
        self.assertEqual(probe.returncode, 7, "the shim must make unlink raise EPERM")

    def test_a_denied_failure_path_unlink_never_becomes_the_reported_error(self):
        # The contract says the failure path MAY attempt an unlink and that its denial is
        # harmless. Pin exactly that: a genuine failure surfaces its own error, never the
        # swallowed EPERM from the tidy-up.
        environment = self.denying_environment()
        environment["HERDR_REGISTRY_ROOT"] = os.path.join(self.base, "absent")
        failed = self.registry_command("request", "claude", "doomed", "--key", "doomed",
                                       "--cwd", self.workspace, environment=environment)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn("Operation not permitted", failed.stderr)

    def test_a_delayed_client_cannot_republish_a_reclaimed_key(self):
        # The reviewer's trace: A publishes and is claimed, retention removes A's input,
        # and B's prepared submission lands afterwards. Unique names mean B cannot occupy
        # A's path, and the claim points at the file it actually read.
        environment = self.client_environment()
        first = self.registry_command("request", "claude", "late", "--key", "late",
                                      "--cwd", self.workspace, environment=environment)
        self.assertEqual(first.returncode, 0, first.stderr)
        watcher = self.watcher(retention=0.0, temp_retention=60.0)
        watcher.pass_once()
        claimed = registry.load_json(self.root.record(registry.CLAIMS, "late"))

        self.place("late", signed(request_document(
            idempotency_key="late", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="late", family="codex",
        ), self.secret), nonce="4-00000000bbbb0002")
        self.clock.now += 1.0
        watcher.pass_once()

        self.assertNotIn(claimed["request_path"],
                         [None, os.path.join(self.root.dir(registry.REQUESTS), "late.json")])
        self.assertTrue(registry.chain(self.root, "late")["intact"],
                        registry.chain(self.root, "late")["breaks"])
        self.assertEqual(len(self.calls("agent", "start")), 1)

    def test_every_submission_for_a_settled_key_is_eventually_reclaimed(self):
        # Round 8 skipped reclaim whenever a sibling existed, on the premise that extras
        # would age out. Nothing aged them out, so both files stayed forever and the
        # drop-dir budget could never recover. Now both are collectable.
        self.drop(key="hold")
        watcher = self.watcher(retention=60.0)
        watcher.pass_once()
        self.place("hold", signed(request_document(
            idempotency_key="hold", capability={"cap_id": self.record["cap_id"]},
            cwd=self.workspace, name="hold",
        ), self.secret), nonce="5-00000000eeee0003")
        self.assertEqual(len(registry.submissions(self.root.dir(registry.REQUESTS), "hold")), 2)

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertEqual(registry.submissions(self.root.dir(registry.REQUESTS), "hold"), [])
        self.assertTrue(registry.chain(self.root, "hold")["intact"],
                        registry.chain(self.root, "hold")["breaks"])

    def test_a_same_key_fan_out_cannot_wedge_the_drop_box_forever(self):
        # The reviewer's 257-submissions-for-one-key trace: the physical budget refuses to
        # examine anything, and without an age-based collector no later pass can recover.
        for index in range(6):
            self.place("fan", signed(request_document(
                idempotency_key="fan", capability={"cap_id": self.record["cap_id"]},
                cwd=self.workspace, name="fan",
            ), self.secret), nonce=f"{index + 1:x}-{index:016x}")
        watcher = self.watcher(max_request_files=4, retention=60.0)
        watcher.pass_once()
        saturated = [event for event in watcher.events if event["event"] == "saturated"]
        self.assertEqual(saturated[0]["reason"], "too_many_request_files")
        self.assertIsNone(self.pane_record("fan"))

        self.clock.now += 61.0
        watcher.pass_once()
        self.assertEqual(len(registry.submissions(self.root.dir(registry.REQUESTS), "fan")), 1,
                         "the winner survives, the duplicates drain")
        self.assertIsNotNone(self.pane_record("fan"), "and the drop box recovers on its own")

    def test_an_unclaimed_winner_is_never_reclaimed(self):
        self.drop(key="waiting")
        watcher = self.watcher(max_panes=0, retention=0.0)
        watcher.pass_once()
        self.clock.now += 1000.0
        watcher.pass_once()
        self.assertEqual(len(registry.submissions(self.root.dir(registry.REQUESTS), "waiting")), 1,
                         "live work is not surplus")

    def test_status_reports_the_reservation_after_a_torn_heartbeat_write(self):
        self.drop()
        watcher = self.watcher(max_panes=0)
        watcher.pass_once()
        os.unlink(self.root.record(registry.HEARTBEAT, "k1"))

        reported = self.registry_command("status", "--root", self.root_dir,
                                         "--store", self.store)
        self.assertEqual(reported.returncode, 0, reported.stderr)
        row = next(entry for entry in json.loads(reported.stdout)["entries"]
                   if entry["key"] == "k1")
        self.assertEqual(row["reservation"], registry.QUEUED)
        self.assertEqual(row["state"], registry.QUEUED, "not `pending` while it holds a slot")

    def test_status_never_invents_a_key_from_a_submission_filename(self):
        self.drop(key="k1")
        reported = self.registry_command("status", "--root", self.root_dir,
                                         "--store", self.store)
        keys = [entry["key"] for entry in json.loads(reported.stdout)["entries"]]
        self.assertEqual(keys, ["k1"], "the nonce is part of the name, not the key")

    def test_status_shows_a_running_reservation(self):
        self.drop()
        self.watcher().pass_once()
        reported = self.registry_command("status", "--root", self.root_dir,
                                         "--store", self.store)
        row = next(entry for entry in json.loads(reported.stdout)["entries"]
                   if entry["key"] == "k1")
        self.assertEqual(row["reservation"], registry.RUNNING)

    def test_launch_wires_a_root_with_its_registry_environment(self):
        launched = self.registry_command(
            "launch", "codex", "root-1", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        report = json.loads(launched.stdout)
        self.assertEqual(report["family"], "codex")
        self.assertTrue(registry.CAP_ID_PATTERN.match(report["cap_id"]))

        env = self.worker_environment()
        self.assertEqual(env["HERDR_REGISTRY_ROOT"], self.root.path)
        cap_id, _, secret = env["HERDR_REGISTRY_CAPABILITY"].partition(":")
        self.assertEqual(cap_id, report["cap_id"])
        self.assertTrue(registry.SECRET_PATTERN.match(secret))

    def test_a_launched_roots_capability_can_actually_spawn(self):
        launched = self.registry_command(
            "launch", "codex", "root-2", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR,
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        cap_id, _, secret = self.worker_environment()[
            "HERDR_REGISTRY_CAPABILITY"].partition(":")

        self.drop(key="fromroot", name="fromroot", cap_id=cap_id, secret=secret)
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("fromroot"),
                             "the contract tells the root to request; the token must work")

    def test_launch_never_prints_the_capability_token(self):
        launched = self.registry_command(
            "launch", "codex", "root-3", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR,
        )
        secret = self.worker_environment()["HERDR_REGISTRY_CAPABILITY"].partition(":")[2]
        self.assertNotIn(secret, launched.stdout)
        self.assertNotIn(secret, launched.stderr)

    def test_launch_refuses_outside_a_herdr_session(self):
        environment = dict(os.environ)
        environment.pop("HERDR_ENV", None)
        refused = self.registry_command(
            "launch", "codex", "root-4", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR, environment=environment,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("requires a herdr session", refused.stderr)

    def test_launch_passes_agent_flags_through(self):
        launched = self.registry_command(
            "launch", "claude", "root-5", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR, "--", "--permission-mode", "plan",
        )
        self.assertEqual(launched.returncode, 0, launched.stderr)
        self.assertEqual(self.agent_flags("root-5"),
                         [*CLAUDE_ROUTE_FLAGS, "--permission-mode", "plan"])

    def test_one_command_creates_the_root_watches_it_and_issues(self):
        fresh = os.path.join(self.base, "fresh")
        path = os.path.join(self.base, "fresh-token")
        handle = open(path, "w")
        target = os.dup(handle.fileno())
        try:
            started = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "watch",
                 "--root", fresh, "--store", self.store, "--anchor", ANCHOR,
                 "--init-root-id", "wired", "--allow-cwd", self.workspace,
                 "--master-key", "env", "--issue", "orchestrator",
                 "--issue-fd", str(target), "--once"],
                capture_output=True, text=True, pass_fds=(target,), env=dict(os.environ),
            )
        finally:
            os.close(target)
            handle.close()
        self.assertEqual(started.returncode, 0, started.stderr)
        manifest = registry.load_json(os.path.join(fresh, registry.MANIFEST))
        self.assertEqual(manifest["root_id"], "wired")
        self.assertEqual(manifest["allow_cwd"], [os.path.realpath(self.workspace)])
        cap_id, _, secret = pathlib.Path(path).read_text().strip().partition(":")
        self.assertTrue(registry.SECRET_PATTERN.match(secret))
        self.assertNotIn(secret, started.stdout)

    def test_one_command_start_does_not_reinitialise_an_existing_root(self):
        before = registry.load_json(os.path.join(self.root_dir, registry.MANIFEST))
        started = self.registry_command(
            "watch", "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
            "--init-root-id", "different", "--allow-cwd", self.workspace,
            "--master-key", "env", "--once",
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        after = registry.load_json(os.path.join(self.root_dir, registry.MANIFEST))
        self.assertEqual(after["session"], before["session"], "an existing session survives")
        self.assertEqual(after["root_id"], before["root_id"])

    def test_two_consecutive_runs_can_reuse_one_worker_name(self):
        """The collision that broke run 20260729T145820.

        A registry-created worker keeps its herdr agent name until its pane closes, so a
        second run whose root asks for the same worker name is refused `name_in_use` — which
        reads as the registry rejecting the root rather than as the previous run failing to
        clean up. The canary now cancels through the registry, which is what frees the name.
        """
        watcher = self.watcher()
        self.drop(key="run1-artifact", name="artifact-writer")
        watcher.pass_once()
        first = self.pane_record("run1-artifact")
        self.assertIsNotNone(first)
        self.assertIn("rrun40f-artifact-writer", self.herdr_state()["agents"])

        # Exactly what the canary writes at the end of a registry-mode run.
        control = self.root.dir(registry.CONTROL, "run1-artifact")
        os.makedirs(control, exist_ok=True)
        staging = os.path.join(control, ".1.tmp")
        pathlib.Path(staging).write_text(json.dumps({"action": "cancel"}))
        os.rename(staging, os.path.join(control, "1.json"))

        watcher.pass_once()
        result = registry.load_json(os.path.join(control, "1.result.json"))
        self.assertTrue(result["ok"])
        self.assertNotIn("rrun40f-artifact-writer", self.herdr_state()["agents"],
                         "the name has to be free before the next run asks for it")

        # Second run: new key, same worker name.
        self.drop(key="run2-artifact", name="artifact-writer")
        watcher.pass_once()
        self.assertIsNone(self.rejection("run2-artifact"))
        self.assertIsNotNone(self.pane_record("run2-artifact"))
        self.assertEqual(len(self.calls("agent", "start")), 2)

    def test_without_the_cancel_the_second_run_still_collides(self):
        # Guards the test above from passing for the wrong reason.
        watcher = self.watcher()
        self.drop(key="run1-artifact", name="artifact-writer")
        watcher.pass_once()
        self.drop(key="run2-artifact", name="artifact-writer")
        watcher.pass_once()
        self.assertEqual(self.rejection("run2-artifact")["reason"], "name_in_use")

    def test_a_cancelled_worker_releases_its_quota_slot(self):
        watcher = self.watcher()
        self.drop(key="run1-artifact", name="artifact-writer")
        watcher.pass_once()
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 1)

        control = self.root.dir(registry.CONTROL, "run1-artifact")
        os.makedirs(control, exist_ok=True)
        pathlib.Path(control, "1.json").write_text(json.dumps({"action": "cancel"}))
        watcher.pass_once()
        watcher.pass_once()
        self.assertEqual(watcher.outstanding_counts(watcher.host.agents())[0], 0,
                         "cancelling through the registry frees the slot too")

    def test_an_omitted_role_defaults_instead_of_being_refused(self):
        environment = self.client_environment()
        published = self.registry_command("request", "claude", "noroles", "--key", "noroles",
                                          "--cwd", self.workspace, environment=environment)
        self.assertEqual(published.returncode, 0, published.stderr)
        document = json.loads(pathlib.Path(self.request_path("noroles")).read_text())
        self.assertEqual(document["role"], registry.DEFAULT_ROLE)
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("noroles"))

    def test_a_blank_role_is_filled_rather_than_refused(self):
        # The acceptance run's exact failure: the root read `--role <r>`, had nothing to put
        # there, and sent an empty one. Role is audit metadata, so it gets a default.
        environment = self.client_environment()
        published = self.registry_command("request", "claude", "blankrole", "--key", "blankrole",
                                          "--cwd", self.workspace, "--role", "",
                                          environment=environment)
        self.assertEqual(published.returncode, 0, published.stderr)
        document = json.loads(pathlib.Path(self.request_path("blankrole")).read_text())
        self.assertEqual(document["role"], registry.DEFAULT_ROLE)
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("blankrole"),
                             "a blank role must never cost a pane")

    def test_a_whitespace_role_is_filled_too(self):
        environment = self.client_environment()
        self.registry_command("request", "claude", "wsrole", "--key", "wsrole",
                              "--cwd", self.workspace, "--role", "   ",
                              environment=environment)
        document = json.loads(pathlib.Path(self.request_path("wsrole")).read_text())
        self.assertEqual(document["role"], registry.DEFAULT_ROLE)

    def test_an_explicit_role_is_preserved(self):
        environment = self.client_environment()
        self.registry_command("request", "claude", "hasrole", "--key", "hasrole",
                              "--cwd", self.workspace, "--role", "review.deep",
                              environment=environment)
        document = json.loads(pathlib.Path(self.request_path("hasrole")).read_text())
        self.assertEqual(document["role"], "review.deep")

    def test_a_hand_written_blank_role_is_still_refused_by_the_host(self):
        # The client fills it; the host keeps validating what it is handed.
        self.drop(key="rawblank", role="")
        self.watcher().pass_once()
        self.assertEqual(self.rejection("rawblank")["reason"], "bad_role")

    def test_a_client_leaves_no_temp_behind(self):
        environment = self.client_environment()
        self.registry_command("request", "claude", "tidy", "--key", "tidy",
                              "--cwd", self.workspace, environment=environment)
        leftovers = [name for name in os.listdir(self.root.dir(registry.REQUESTS))
                     if name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_a_re_run_of_request_never_rewrites_its_own_committed_bytes(self):
        self.drop(key="stable", name="stable")
        self.watcher().pass_once()
        digest = registry.load_json(self.root.record(registry.CLAIMS, "stable"))["request_digest"]

        again = self.registry_command("request", "claude", "stable", "--key", "stable",
                                      "--cwd", self.workspace)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertTrue(registry.chain(self.root, "stable")["intact"])
        self.assertEqual(
            registry.load_json(self.root.record(registry.CLAIMS, "stable"))["request_digest"],
            digest,
        )

    def test_a_re_run_after_reclaim_does_not_resurrect_the_input(self):
        self.drop(key="gone", name="gone")
        watcher = self.watcher(retention=0.0)
        watcher.pass_once()
        self.clock.now += 1.0
        watcher.pass_once()
        self.assertFalse(os.path.exists(self.request_path("gone")))

        waited = self.registry_command("request", "claude", "gone", "--key", "gone",
                                       "--cwd", self.workspace, "--wait", "5")
        self.assertEqual(waited.returncode, 0, waited.stderr)
        self.assertFalse(os.path.exists(self.request_path("gone")))

    def test_a_generated_master_key_without_issue_refuses_to_start(self):
        # It cannot share its key with a separate `serve-capability`, so it could never
        # authorize anyone: a dead mode must fail loudly, not run and refuse everything.
        environment = dict(os.environ)
        environment.pop(registry.MASTER_KEY_ENV, None)
        refused = self.registry_command(
            "watch", "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
            "--master-key", "generate", "--once", environment=environment,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("can authorize nobody", refused.stderr)

    def issue_watch(self, *extra, environment=None):
        """Run `watch --issue` with a private descriptor for the bearer token."""
        path = os.path.join(self.base, "token")
        handle = open(path, "w")
        target = os.dup(handle.fileno())
        try:
            started = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "watch",
                 "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
                 "--issue", "orchestrator", "--issue-fd", str(target), "--once", *extra],
                capture_output=True, text=True, pass_fds=(target,),
                env=environment or dict(os.environ),
            )
        finally:
            os.close(target)
            handle.close()
        return started, pathlib.Path(path).read_text()

    def test_a_generated_master_key_can_bootstrap_itself_with_issue(self):
        environment = dict(os.environ)
        environment.pop(registry.MASTER_KEY_ENV, None)
        started, token = self.issue_watch("--master-key", "generate", environment=environment)
        self.assertEqual(started.returncode, 0, started.stderr)
        cap_id, _, secret = token.strip().partition(":")
        self.assertTrue(registry.CAP_ID_PATTERN.match(cap_id))
        self.assertTrue(registry.SECRET_PATTERN.match(secret))

    def test_the_bootstrap_token_never_touches_the_log_stream(self):
        started, token = self.issue_watch()
        self.assertEqual(started.returncode, 0, started.stderr)
        secret = token.strip().partition(":")[2]
        self.assertTrue(secret)
        # A redirected stdout, a terminal scrollback or a log collector must not end up
        # holding a signing secret — that is the whole point of the keyless store.
        self.assertNotIn(secret, started.stdout)
        self.assertNotIn(secret, started.stderr)
        issued = [json.loads(line) for line in started.stdout.splitlines()
                  if '"issued"' in line]
        self.assertEqual(len(issued), 1)
        self.assertNotIn("token", issued[0])
        self.assertEqual(issued[0]["cap_id"], token.strip().partition(":")[0])

    def test_an_inherited_descriptor_is_not_taken_as_operator_consent(self):
        # The reviewer's repro: an unrelated writable fd inherited from a supervisor or
        # shell integration used to be silently accepted as the secret-delivery channel.
        path = os.path.join(self.base, "inherited")
        handle = open(path, "w")
        target = os.dup(handle.fileno())
        try:
            refused = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "watch",
                 "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
                 "--issue", "orchestrator", "--once"],
                capture_output=True, text=True, pass_fds=(target,),
                env=dict(os.environ),
            )
        finally:
            os.close(target)
            handle.close()
        self.assertEqual(refused.returncode, 2)
        self.assertIn("requires an explicit --issue-fd", refused.stderr)
        self.assertEqual(pathlib.Path(path).read_text(), "",
                         "no token may reach a descriptor nobody nominated")

    def test_an_operator_releases_a_reservation_explicitly(self):
        self.drop(key="k1")
        watcher = self.watcher()
        watcher.pass_once()
        state = self.herdr_state()
        state["agents"] = {}
        with open(self.state, "w") as handle:
            json.dump(state, handle)

        released = self.registry_command(
            "release", "--root", self.root_dir, "--key", "k1", "--store", self.store,
        )
        self.assertEqual(released.returncode, 0, released.stderr)
        self.assertEqual(watcher.outstanding_counts({})[0], 0)

    def test_releasing_a_reservation_with_a_live_agent_is_refused(self):
        self.drop(key="k1")
        self.watcher().pass_once()
        refused = self.registry_command(
            "release", "--root", self.root_dir, "--key", "k1", "--store", self.store,
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("still has a live agent", refused.stderr)

    def test_a_bootstrap_token_is_never_written_to_the_log_descriptors(self):
        for fd in ("1", "2"):
            refused = self.registry_command(
                "watch", "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
                "--issue", "orchestrator", "--issue-fd", fd, "--once",
            )
            self.assertEqual(refused.returncode, 2, fd)
            self.assertIn("may not be stdin, stdout or stderr", refused.stderr)

    def test_issue_without_an_explicit_descriptor_is_refused(self):
        # No default: an inherited fd is not operator consent, and an unredirected
        # default would otherwise have become the lock file with the token inside it.
        refused = self.registry_command(
            "watch", "--root", self.root_dir, "--store", self.store, "--anchor", ANCHOR,
            "--issue", "orchestrator", "--once",
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("requires an explicit --issue-fd", refused.stderr)
        lock = pathlib.Path(self.root_dir, registry.WATCHER_LOCK)
        self.assertFalse(lock.exists() and lock.read_text().strip(),
                         "no token may ever be written into the lock file")

    def test_a_watch_issued_token_actually_signs_a_request(self):
        started, token = self.issue_watch()
        self.assertEqual(started.returncode, 0, started.stderr)
        environment = dict(os.environ, HERDR_REGISTRY_ROOT=self.root_dir,
                           HERDR_REGISTRY_CAPABILITY=token.strip())
        dropped = self.registry_command("request", "claude", "boot", "--key", "boot",
                                        "--cwd", self.workspace, environment=environment)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        self.watcher().pass_once()
        self.assertIsNotNone(self.pane_record("boot"))

    def test_the_master_key_never_reaches_a_child_process(self):
        host = registry.Host(binary=self.fake, anchor=ANCHOR)
        self.assertIn(registry.MASTER_KEY_ENV, os.environ)
        self.assertNotIn(registry.MASTER_KEY_ENV, host.child_env())
        self.assertEqual(host.child_env(HERDR_PANE_ID="x")["HERDR_PANE_ID"], "x")

    def test_no_herdr_call_carries_the_master_key(self):
        self.drop()
        self.watcher().pass_once()
        # The fake records argv only, so assert on the env the host would hand a child.
        leaked = subprocess.run(
            [self.fake, "agent", "list"], capture_output=True, text=True,
            env=registry.Host(binary=self.fake, anchor=ANCHOR).child_env(
                FAKE_HERDR_STATE=self.state),
        )
        self.assertEqual(leaked.returncode, 0, leaked.stderr)

    def test_serve_capability_refuses_a_non_finite_ttl(self):
        for ttl in ("inf", "nan", "-1"):
            refused = self.registry_command(
                "serve-capability", "--root", self.root_dir, "--subject", "x",
                "--grants", "spawn", "--store", self.store, "--ttl", ttl,
                "--master-key", "env",
            )
            self.assertEqual(refused.returncode, 2, ttl)
            self.assertIn("finite positive", refused.stderr)

    def test_a_capability_does_not_survive_a_regenerated_master_key(self):
        self.drop()
        stranded = registry.Watcher(
            self.root,
            registry.Host(binary=self.fake, teammate=str(SCRIPT_DIR / "agent-teammate.py"),
                          anchor=ANCHOR),
            registry.Keyring.generate(), store=self.store, profile_label=PROFILE,
            clock=self.clock,
        )
        stranded.log = lambda event, **fields: None
        stranded.pass_once()
        # Loud, not silent: the operator re-supplies the key or re-issues capabilities.
        self.assertEqual(self.rejection()["reason"], "capability_tampered")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_request_wait_exits_non_zero_on_a_refusal(self):
        environment = dict(os.environ, HERDR_REGISTRY_ROOT=self.root_dir,
                           HERDR_REGISTRY_CAPABILITY=f"{self.record['cap_id']}:{self.secret}")
        dropped = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "request", "claude",
             "denied", "--key", "denied", "--cwd", "/etc"],
            capture_output=True, text=True, env=environment,
        )
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        self.watcher().pass_once()

        waited = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "herdr-registry.py"), "request", "claude",
             "denied", "--key", "denied", "--cwd", "/etc", "--wait", "5"],
            capture_output=True, text=True, env=environment,
        )
        self.assertEqual(waited.returncode, 3)
        self.assertIn("bad_cwd:outside_allowlist", waited.stderr)
        self.assertIn("Do NOT fall back", waited.stderr)


class SeatResolutionTest(unittest.TestCase):
    """The rule itself, without a pane: the request's own role, then the host override,
    then the family default, and a refusal when a family has none of the three."""

    def test_the_order_the_three_sources_are_taken_in(self):
        self.assertEqual(
            registry.spawn_role("claude", "review.deep", "code.routine"), "review.deep")
        self.assertEqual(
            registry.spawn_role("claude", registry.DEFAULT_ROLE, "review.deep"), "review.deep")
        self.assertEqual(registry.spawn_role("claude", "  ", None), "code.routine")
        self.assertEqual(registry.spawn_role("codex", None, None), "code.routine")
        self.assertEqual(registry.spawn_role("agy", None, None), "code.routine")
        self.assertEqual(registry.spawn_role("agy", None, "review.deep"), "review.deep")

    def test_a_family_with_nothing_to_spawn_with_is_a_request_refusal(self):
        # Every family carries a default today, so the refusal is reached by taking one away.
        with mock.patch.dict(registry.FAMILY_ROLES, {}, clear=True):
            for requested, override in ((None, None), (registry.DEFAULT_ROLE, ""),
                                        (registry.DEFAULT_ROLE, registry.DEFAULT_ROLE)):
                with self.assertRaises(registry.RequestError) as caught:
                    registry.spawn_role("agy", requested, override)
                self.assertEqual(caught.exception.reason, "no_role:agy")


class SpawnRoleTest(WatcherFixture):
    """Chair ruling family-default-roles. `agent-teammate` has required `--role` since
    dotfiles dd804cc and a role IS the launch policy, so the host decides the seat: the
    request's own role, then the host override, then the family default."""

    def claim(self, key="k1"):
        return registry.load_json(self.root.record(registry.CLAIMS, key))

    def test_the_requests_own_role_wins(self):
        self.drop(role="code.light")
        self.watcher().pass_once()

        self.assertEqual(self.claim()["spawn_role"], "code.light")
        # The pane record carries it too: it is what a chair reads back from `status`.
        self.assertEqual(self.pane_record()["spawn_role"], "code.light")
        self.assertEqual(self.agent_flags("rrun40f-reviewer-1")[:4], REVIEWER_ROUTE_FLAGS)

    def test_the_placeholder_falls_through_to_the_family_default(self):
        """`request --role` fills a blank one with `worker`, which is audit metadata and
        not a roles.json seat: spawning with it would fail at routing."""
        self.drop(role=registry.DEFAULT_ROLE)
        self.watcher().pass_once()

        self.assertEqual(self.claim()["role"], registry.DEFAULT_ROLE)
        self.assertEqual(self.claim()["spawn_role"], registry.FAMILY_ROLES["claude"])
        self.assertEqual(self.agent_flags("rrun40f-reviewer-1")[:4], CLAUDE_ROUTE_FLAGS)

    def test_the_watch_override_beats_the_family_default(self):
        self.drop(role=registry.DEFAULT_ROLE)
        self.watcher(default_role="code.light").pass_once()

        self.assertEqual(self.claim()["spawn_role"], "code.light")
        self.assertEqual(self.agent_flags("rrun40f-reviewer-1")[:4], REVIEWER_ROUTE_FLAGS)

    def test_a_named_role_still_beats_the_watch_override(self):
        self.drop(role="review.deep")
        self.watcher(default_role="code.light").pass_once()

        self.assertEqual(self.claim()["spawn_role"], "review.deep")

    def test_a_family_with_no_default_is_refused_before_a_pane_is_opened(self):
        """A family with no entry is refused here rather than spawned into
        `agent-teammate`'s own refusal a moment later. Every family carries a default
        today, so the entry is taken away to reach the guard."""
        self.drop(key="agy1", family="agy", argv=[], role=registry.DEFAULT_ROLE)
        with mock.patch.dict(registry.FAMILY_ROLES, {}, clear=True):
            self.watcher().pass_once()

        self.assertEqual(self.rejection("agy1")["reason"], "no_role:agy")
        self.assertEqual(self.calls("pane", "split"), [])

    def test_the_refusal_reaches_a_waiting_client_as_exit_3(self):
        environment = self.client_environment()
        dropped = self.registry_command("request", "agy", "noseat", "--key", "noseat",
                                        "--cwd", self.workspace, environment=environment)
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        with mock.patch.dict(registry.FAMILY_ROLES, {}, clear=True):
            self.watcher().pass_once()

        waited = self.registry_command("request", "agy", "noseat", "--key", "noseat",
                                       "--cwd", self.workspace, "--wait", "5",
                                       environment=environment)
        self.assertEqual(waited.returncode, 3)
        self.assertIn("no_role:agy", waited.stderr)

    def test_launch_takes_the_family_default_and_its_own_override(self):
        default = self.registry_command(
            "launch", "claude", "root-6", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(self.agent_flags("root-6"), CLAUDE_ROUTE_FLAGS)

        overridden = self.registry_command(
            "launch", "claude", "root-7", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR, "--role", "code.light",
        )
        self.assertEqual(overridden.returncode, 0, overridden.stderr)
        self.assertEqual(self.agent_flags("root-7"), REVIEWER_ROUTE_FLAGS)

    def test_launch_with_a_role_the_family_is_not_in_is_the_launch_error(self):
        """Every family carries a default now, so the launch that cannot spawn is the one
        whose role holds no runner for the family: `code.light` is a claude-only route."""
        refused = self.registry_command(
            "launch", "agy", "root-8", "--root", self.root_dir, "--store", self.store,
            "--cwd", self.workspace, "--anchor", ANCHOR, "--role", "code.light",
        )
        self.assertEqual(refused.returncode, 2)
        self.assertIn("launch failed: spawn_failed:", refused.stderr)
        # The fake herdr writes its state file on its first call, so its absence is the
        # assertion: the seat is resolved before the host is asked for anything.
        self.assertFalse(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
