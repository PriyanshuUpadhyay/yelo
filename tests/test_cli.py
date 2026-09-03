"""C01 (list matches the reference goldens), C02 (resolve exit codes), C20 (stdlib and
public APIs only), C12 and C13 (the usage rows are read in process), and law L3 (the row
count is the layout census)."""

import ast
import glob
import json
import os
import pathlib
import re
import subprocess
import sys
import time

import pytest

import jello as jello_pkg
from conftest import (build_census_home, build_usage_home, fixture_env, run_jello,
                      usage_env)

ROOT = pathlib.Path(__file__).parent.parent
GOLDEN = ROOT / "tests" / "golden"
SOURCE = ROOT / "src" / "jello"
CLIS = ("claude", "codex", "prime")
# The signed label rule (R9), written out here rather than imported, so the census counts
# by the rule instead of by the same function the code under test uses.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
# Every entry the census layout must leave out, whatever the reason.
CENSUS_EXCLUDED = {"bad name", "-lead", ".hidden", ".session-map", "notes.txt",
                   ".aliases", "", "file"}


@pytest.mark.parametrize("cli", CLIS)
def test_list_matches_golden(jello, cli):
    """The goldens were rendered once from the reference script against this same fixture
    HOME; see tests/golden/README.md."""
    result = jello("profile", "list", "--cli", cli)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (GOLDEN / f"list-{cli}.txt").read_text()


def census(home, cli):
    """Law L3 counted straight off the filesystem: one row per layout directory whose
    label passes the rule. The codex `sessions/` marker is not part of discovery — it is
    the usage HUD's marker — and the reference script counts a codex home without it, so
    the census does too."""
    if cli == "claude":
        root = os.path.join(home, ".claude", ".profiles")
        return sum(1 for name in os.listdir(root)
                   if os.path.isdir(os.path.join(root, name)) and NAME_RE.match(name))
    if cli == "codex":
        prefix, root = ".codex-", home
    else:
        prefix, root = "agent-", os.path.join(home, ".prime")
    base = os.path.join(root, ".codex" if cli == "codex" else "agent")
    total = 1 if os.path.isdir(base) else 0
    for directory in glob.glob(os.path.join(root, prefix + "*")):
        name = os.path.basename(directory)[len(prefix):]
        if os.path.isdir(directory) and NAME_RE.match(name):
            total += 1
    return total


@pytest.mark.parametrize("cli", CLIS)
def test_profile_census_matches_layout(tmp_path, cli):
    """L3: no phantom row and no missed home. The layout carries a valid name, an invalid
    one, a hidden dot directory, a plain file, and a codex home with no sessions/ marker."""
    home = build_census_home(tmp_path / "census")
    result = run_jello(["profile", "list", "--cli", cli, "--json"], fixture_env(home))
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    expected = census(home, cli)
    assert expected > 1, "the layout must hold more than the base home"
    assert len(rows) == expected
    assert [row["name"] for row in rows if row["name"] in CENSUS_EXCLUDED] == []


def test_resolve_exit_codes(jello):
    home = jello.home
    exact = jello("profile", "resolve", "--cli", "claude", "--", "pri")
    assert exact.returncode == 0
    assert exact.stdout == f"pri\t{os.path.join(home, '.claude', '.profiles', 'pri')}\n"

    substring = jello("profile", "resolve", "--cli", "claude", "--", "wor")
    assert substring.returncode == 0
    assert substring.stdout.split("\t")[0] == "work"

    email = jello("profile", "resolve", "--cli", "codex", "--", "alt@example.test")
    assert email.returncode == 0
    assert email.stdout.split("\t")[0] == "alt"

    ambiguous = jello("profile", "resolve", "--cli", "claude", "--", "e")
    assert ambiguous.returncode == 2
    assert ambiguous.stderr.startswith("claude: 'e' matches several profiles\n")
    assert "pri" in ambiguous.stderr and "work" in ambiguous.stderr

    missing = jello("profile", "resolve", "--cli", "claude", "--", "zzz")
    assert missing.returncode == 1
    assert missing.stderr == "claude: no profile matches 'zzz'\n"
    assert missing.stdout == ""


def imported_roots(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield (node.module or "").split(".")[0]


def private_attributes(path):
    """Attribute reads that reach into another object's privates. `self._x` inside a class
    is a module's own business; anything else is a standard-library internal."""
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        if node.attr.startswith("__") and node.attr.endswith("__"):
            continue
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            continue
        yield node.attr


def test_public_apis_only():
    """R1: product code uses the standard library's public surface only, so an interpreter
    upgrade cannot take a private attribute out from under jello."""
    private = {str(path.relative_to(SOURCE)): sorted(set(private_attributes(path)))
               for path in sorted(SOURCE.rglob("*.py"))
               if list(private_attributes(path))}
    assert private == {}


def test_stdlib_only():
    """R1: jello runs on every prompt through a shell hook, so it may not import anything
    that is not already in the interpreter."""
    outside = {}
    for path in sorted(SOURCE.rglob("*.py")):
        extra = sorted(
            root for root in imported_roots(path)
            if root and root != "jello" and root not in sys.stdlib_module_names
        )
        if extra:
            outside[str(path.relative_to(SOURCE))] = extra
    assert outside == {}


def test_help_lists_every_group(jello):
    result = jello("--help")
    assert result.returncode == 0
    for group in ("profile", "usage", "resume", "shell-init", "setup", "doctor", "hud"):
        assert group in result.stdout


ENUMERATORS = {"glob", "listdir", "scandir", "iterdir"}
CENSUS_MARKERS = (".profiles", ".codex", ".prime")


def enumeration_calls(tree):
    """Every call to one of the four ways to enumerate a directory, whichever spelling."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else getattr(
            function, "id", None)
        if name in ENUMERATORS:
            yield node, name


def test_usage_enumerates_only_through_core():
    """Law L2: one census. jello.usage never walks the layout itself — it asks core for the
    rows — so no enumeration inside it may name .profiles, .codex, or .prime. The one
    exception is the rollout scan INSIDE a single codex home whose path core already handed
    over: a file scan under a known directory, not a discovery of accounts."""
    scans = []
    for path in sorted((SOURCE / "usage").glob("*.py")):
        tree = ast.parse(path.read_text())
        assigned = {target.id: ast.unparse(node.value)
                    for node in ast.walk(tree) if isinstance(node, ast.Assign)
                    for target in node.targets if isinstance(target, ast.Name)}
        for node, name in enumeration_calls(tree):
            # A pattern is usually built one line above the call, so the name it was bound
            # to is resolved back to the expression that built it.
            argument = " ".join(
                [ast.unparse(item) for item in node.args]
                + [assigned.get(item.id, "") for item in node.args
                   if isinstance(item, ast.Name)])
            where = f"{path.name}:{node.lineno} {name}({argument})"
            for marker in CENSUS_MARKERS:
                assert marker not in argument, f"{where} enumerates the {marker} layout"
            scans.append((where, argument))
    assert len(scans) == 1, scans
    assert scans[0][1].endswith("'rollout-*.jsonl')"), scans[0]


def test_import_direction(jello):
    """Law L2: core is the census, so it may not reach back into the modules that read it.
    Importing core must not drag in jello.usage or jello.hud."""
    result = run_jello(["--version"], fixture_env(jello.home))
    assert result.returncode == 0
    probe = subprocess.run(
        [sys.executable, "-c",
         "import sys, jello.profile.core; "
         "print(sorted(m for m in sys.modules "
         "if m.startswith('jello.usage') or m == 'jello.hud'))"],
        capture_output=True, text=True,
        env=os.environ | {"PYTHONPATH": str(ROOT / "src")},
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "[]"


def test_core_no_longer_shells_out_for_usage(jello):
    """R5: the subprocess stand-in is gone, name and knob together."""
    from jello.profile import core

    assert not hasattr(core, "usage_data_rows")
    for path in sorted(SOURCE.rglob("*.py")):
        assert "AGENT_PROFILES_USAGE_DATA" not in path.read_text(), path


def test_list_usage_in_process(tmp_path):
    """C13: the usage column and the picker read the cache files themselves, with no
    usage-hud-data anywhere on PATH and no override environment variable."""
    now = int(time.time())
    home = build_usage_home(tmp_path / "home", now)
    env = usage_env(home)
    env["PATH"] = "/nonexistent"
    listing = run_jello(["profile", "list", "--cli", "claude", "--usage"], env)
    assert listing.returncode == 0, listing.stderr
    assert "5h 57% left" in listing.stdout, listing.stdout
    assert "no data" in listing.stdout, "the profile with no cache has no usage rows"

    picked = run_jello(["profile", "pick", "--cli", "claude", "--json"], env)
    assert picked.returncode == 0, picked.stderr
    assert json.loads(picked.stdout)["name"] == "pri", (
        "pri is 21 minutes from wasting 57% of its 5h window; work has a whole one ahead")


def test_version_is_the_package_version(jello):
    result = jello("--version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"jello {jello_version()}"


def jello_version():
    """pyproject.toml and __init__.py both carry the version; the shell-init cache is keyed
    by it, so they may not drift apart."""
    text = (ROOT / "pyproject.toml").read_text()
    for line in text.splitlines():
        if line.startswith("version = "):
            declared = line.split("=", 1)[1].strip().strip('"')
            assert declared == jello_pkg.__version__
            return declared
    raise AssertionError("pyproject.toml has no version line")
