"""Installed profile selection works without Yelo or its checkout on the import path."""

import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from conftest import build_usage_home
from yelo import integration


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "home with spaces"
    build_usage_home(home, int(time.time()))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("ZDOTDIR", raising=False)
    integration.apply(str(home))
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys._base_executable)
    for cli in ("claude", "codex"):
        path = binaries / cli
        path.write_text('''#!/bin/sh
printf '%s\\n' "home=$CODEX_HOME" "claude=$CLAUDE_PROFILE_DIR" "identity=$CLAUDE_SECURESTORAGE_CONFIG_DIR"
printf 'arg=%s\\n' "$@"
exit 23
''')
        path.chmod(0o755)
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh is not installed")
    env = {"HOME": str(home), "PATH": str(binaries),
           "AGENT_PROFILES_SECURITY_BIN": "/usr/bin/true", "PYTHONPATH": "/missing/yelo"}
    shell = shlex.quote(str(home / ".config/yelo/shell.sh"))
    return home, zsh, env, f"source {shell}\n"


def run(installed, command, **env):
    home, zsh, environment, source = installed
    return subprocess.run([zsh, "-dfc", source + command], env={**environment, **env},
                          cwd=home, text=True, capture_output=True, timeout=10)


@pytest.mark.parametrize("cli,name,suffix", [("claude", "pri", ".claude/.profiles/pri"),
                                             ("codex", "alt", ".codex-alt")])
def test_named_selection_without_yelo(installed, cli, name, suffix):
    home, _, _, _ = installed
    result = run(installed, f"{cli} --profile {name} 'hello world' '$(touch forbidden)'")
    assert result.returncode == 23, result.stderr
    assert str(home / suffix) in result.stdout
    assert "arg=--profile" not in result.stdout
    assert "arg=hello world" in result.stdout
    assert "arg=$(touch forbidden)" in result.stdout
    assert not (home / "forbidden").exists()


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_auto_selection_uses_existing_usage_ranking(installed, cli):
    home, _, env, _ = installed
    archive = integration.runtime_path(str(home))
    expected = subprocess.run([str(Path(env["PATH"]) / "python3"), "-I", archive,
                               "pick", "--cli", cli, "--json"], env=env, text=True, capture_output=True)
    assert expected.returncode == 0, expected.stderr
    selected = json.loads(expected.stdout)
    result = run(installed, cli)
    assert result.returncode == 23, result.stderr
    assert selected["dir"] in result.stdout
    assert "expiring usage first" in result.stderr


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_bare_profile_uses_real_terminal_menu(installed, cli):
    _, zsh, env, source = installed
    master, slave = pty.openpty()
    process = subprocess.Popen([zsh, "-dfc", source + f"{cli} --profile"], env=env,
                               stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b""
    try:
        deadline = time.monotonic() + 10
        answered = False
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 65536)
                except OSError:
                    break
            if b"choice:" in output and not answered:
                os.write(master, b"2\n")
                answered = True
            if process.poll() is not None:
                break
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
    assert process.returncode == 23, output.decode()
    assert b"select an account" in output
    assert (b".profiles/work" if cli == "claude" else b".codex-alt") in output


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_piped_menu_fails_without_starting_vendor(installed, cli):
    result = run(installed, cli + " --profile")
    assert result.returncode == 2
    assert not result.stdout


@pytest.mark.parametrize("cli,env_name", [("claude", "CLAUDE_PROFILE_DIR"), ("codex", "CODEX_HOME")])
def test_inherited_account_is_not_auto_switched(installed, cli, env_name):
    result = run(installed, cli, **{env_name: "/explicit/account"})
    assert result.returncode == 23, result.stderr
    assert "/explicit/account" in result.stdout
    assert "using profile" not in result.stderr


def test_native_config_and_host_guard_are_preserved(installed):
    result = run(installed, '''
_codex_host_guard() { [[ "$1" != exec ]] || return 19; }
codex --profile alt -p deep-review -C '/tmp/a b' 'hello world'
''')
    assert result.returncode == 23, result.stderr
    assert "arg=-p\narg=deep-review\narg=-C\narg=/tmp/a b" in result.stdout
    result = run(installed, '''
_codex_host_guard() { [[ "$1" != exec ]] || return 19; }
codex --profile alt -C /tmp exec hello
''')
    assert result.returncode == 19
    assert not result.stdout


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_unknown_account_never_falls_back(installed, cli):
    result = run(installed, cli + " --profile missing")
    assert result.returncode == 2
    assert not result.stdout


def test_unrelated_shell_definitions_survive(installed):
    _, zsh, env, source = installed
    script = "alias codex='print foreign-codex'\nclaude() { print foreign-claude; }\n" + source + "eval codex\nclaude"
    result = subprocess.run([zsh, "-dfc", script], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["foreign-codex", "foreign-claude"]


@pytest.mark.parametrize("command", ["claude --resume", "claude auth login", "codex resume", "codex login"])
def test_bound_commands_cannot_auto_switch_in_a_pipe(installed, command):
    result = run(installed, command)
    assert result.returncode == 2
    assert not result.stdout


def test_named_session_runs_in_the_account_that_owns_it(installed):
    home, _, _, _ = installed
    session = "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3"  # the rollout build_usage_home puts in alt
    result = run(installed, f"codex -C /tmp resume {session} 'carry on'")
    assert result.returncode == 23, result.stderr
    assert "home=" + str(home / ".codex-alt") in result.stdout
    assert f"arg=-C\narg=/tmp\narg=resume\narg={session}\narg=carry on" in result.stdout
    assert "owns the session" in result.stderr
    result = run(installed, "codex resume 019e08eb-0000-7000-8000-000000000000")
    assert result.returncode == 2
    assert not result.stdout


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_help_passes_through_without_account_selection(installed, cli):
    result = run(installed, cli + " --help")
    assert result.returncode == 23
    assert "using profile" not in result.stderr
    assert "arg=--help" in result.stdout


def test_current_shell_can_replace_the_obsolete_wrapper(installed):
    from yelo import legacy

    home, zsh, env, source = installed
    old = home / "old.zsh"
    old.write_text(legacy.STANDALONE_SHELL)
    script = '_codex_host_guard() { return 0; }\n'
    script += "source " + shlex.quote(str(old)) + "\n" + source + source
    script += "codex --profile alt"
    result = subprocess.run([zsh, "-dfc", script], env=env, capture_output=True, text=True)
    assert result.returncode == 23, result.stderr
    assert str(home / ".codex-alt") in result.stdout


def test_foreign_integration_file_is_not_overwritten(installed):
    from yelo import setup

    home, _, _, _ = installed
    path = home / ".config/yelo/shell.sh"
    path.write_text("# My custom shell integration\n")
    with pytest.raises(setup.SetupError, match="another owner"):
        integration.apply(str(home))
    assert path.read_text() == "# My custom shell integration\n"


def fable_cache(home, name, pct, *, expired=False, reset_seconds=86400):
    path = home / '.claude/.profiles' / name / '.usage-api-cache-fable.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    path.write_text(json.dumps({"seven_day": {"used_percentage": pct,
                                             "resets_at": now - 1 if expired else now + reset_seconds},
                                "ts": now, "fetched_at": now, "source": "api"}))


@pytest.mark.parametrize("model,expected", [("fable", "work"), ("claude-fable-5", "work"),
                                           ("best", "work"), ("sonnet", "pri")])
def test_claude_model_selects_usable_fable_account(installed, model, expected):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100)
    result = run(installed, 'claude --model ' + model)
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles' / expected) in result.stdout
    assert 'arg=--model\narg=' + model in result.stdout


@pytest.mark.parametrize("source", ["user", "project", "environment", "override", "alias"])
def test_startup_model_sources_reach_installed_selector(installed, source):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100)
    command, environment = 'claude', {}
    if source == 'user':
        (home / '.claude/settings.json').write_text('{"model":"fable"}')
    elif source == 'project':
        (home / '.git').mkdir()
        (home / '.claude/settings.local.json').write_text('{"model":"fable"}')
    elif source == 'environment':
        environment['ANTHROPIC_MODEL'] = 'fable'
    elif source == 'override':
        command += ' --settings \'{"model":"fable"}\''
    else:
        environment['ANTHROPIC_DEFAULT_SONNET_MODEL'] = 'claude-fable-5'
        command += ' --model sonnet'
    result = run(installed, command, **environment)
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/work') in result.stdout


@pytest.mark.parametrize("single", [False, True])
def test_all_fable_exhausted_stops_before_vendor_launch(installed, single):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100)
    if single:
        shutil.rmtree(home / '.claude/.profiles/work')
    else:
        fable_cache(home, 'work', 100)
    result = run(installed, 'claude --model fable')
    assert result.returncode == 2
    assert 'Fable usage is exhausted' in result.stderr
    assert not result.stdout


def test_fable_reset_restores_eligibility(installed):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100, expired=True)
    result = run(installed, 'claude --model=fable')
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/pri') in result.stdout


def test_explicit_profile_stays_in_control_with_fable(installed):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100)
    result = run(installed, 'claude --profile pri --model fable')
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/pri') in result.stdout


def test_fable_window_participates_in_ratio_ranking(installed):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 90)
    fable_cache(home, 'work', 20, reset_seconds=3600)
    result = run(installed, 'claude --model fable')
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/work') in result.stdout
    result = run(installed, 'claude --model sonnet')
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/pri') in result.stdout


def test_explicit_model_overrides_configured_fable(installed):
    home, _, _, _ = installed
    fable_cache(home, 'pri', 100)
    (home / '.claude/settings.json').write_text('{"model":"fable"}')
    result = run(installed, 'claude --model sonnet')
    assert result.returncode == 23, result.stderr
    assert 'claude=' + str(home / '.claude/.profiles/pri') in result.stdout
