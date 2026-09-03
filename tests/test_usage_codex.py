"""C10: the codex app-server helper, ported from the reference's own unit tests.

`~/dotfiles/apps/usage-hud/Tests/UsageScriptsTests/test_codex_usage_fetch.py` as pytest.
The four cases and their assertions are the originals; only the way the module is reached
changed, from a SourceFileLoader over the script path to a plain import.
"""

import json
import os
import textwrap

import pytest

from jello.usage import codex


@pytest.fixture
def fake_codex(tmp_path):
    """A stand-in `codex` that answers JSON-RPC lines on stdin with a canned script."""
    def build(body):
        path = tmp_path / "codex"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
        path.chmod(0o755)
        return str(path)

    return build


ANSWERS = '''
    import json
    import sys
    for line in sys.stdin:
        message = json.loads(line)
        if message.get("id") == 1:
            print(json.dumps({"id": 1, "result": {"codexHome": "/tmp/codex"}}), flush=True)
        elif message.get("id") == 2:
            print(json.dumps({
                "id": 2,
                "result": {"rateLimits": {
                    "primary": {"usedPercent": 12.4, "windowDurationMins": 10080,
                                "resetsAt": 4102444800},
                    "secondary": None
                }}
            }), flush=True)
'''


def test_fetches_and_normalizes_primary_window(fake_codex, tmp_path):
    result = codex.fetch_rate_limits(fake_codex(ANSWERS), str(tmp_path), 2)
    assert result["rate_limits"]["primary"]["used_percent"] == 12
    assert result["rate_limits"]["primary"]["window_minutes"] == 10080
    assert result["rate_limits"]["secondary"] is None
    assert result["source"] == "api"


def test_rejects_incomplete_window(fake_codex, tmp_path):
    binary = fake_codex('''
        import json
        import sys
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == 1:
                print(json.dumps({"id": 1, "result": {}}), flush=True)
            elif message.get("id") == 2:
                print(json.dumps({"id": 2, "result": {"rateLimits": {
                    "primary": {"usedPercent": 12, "windowDurationMins": 10080},
                    "secondary": None
                }}}), flush=True)
    ''')
    with pytest.raises(ValueError, match="incomplete"):
        codex.fetch_rate_limits(binary, str(tmp_path), 2)


def test_surfaces_rate_limit_request_error(fake_codex, tmp_path):
    binary = fake_codex('''
        import json
        import sys
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == 1:
                print(json.dumps({"id": 1, "result": {}}), flush=True)
            elif message.get("id") == 2:
                print(json.dumps({"id": 2, "error": {"code": -32000, "message": "failed"}}),
                      flush=True)
    ''')
    with pytest.raises(RuntimeError, match="rateLimits/read"):
        codex.fetch_rate_limits(binary, str(tmp_path), 2)


def test_times_out_without_a_response(fake_codex, tmp_path):
    binary = fake_codex('''
        import time
        time.sleep(10)
    ''')
    with pytest.raises(TimeoutError):
        codex.fetch_rate_limits(binary, str(tmp_path), 0.1)


def test_codex_home_reaches_the_binary(fake_codex, tmp_path):
    """The helper takes one already-resolved home per call, and hands it over as
    CODEX_HOME -- that is the whole of how a per-account fetch is keyed."""
    recorder = tmp_path / "home-seen"
    binary = fake_codex(f'''
        import json
        import os
        import sys
        open({str(recorder)!r}, "w").write(os.environ["CODEX_HOME"])
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == 1:
                print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
            elif message.get("id") == 2:
                print(json.dumps({{"id": 2, "result": {{"rateLimits": {{
                    "primary": {{"usedPercent": 1, "windowDurationMins": 300,
                                 "resetsAt": 4102444800}},
                    "secondary": None}}}}}}), flush=True)
    ''')
    home = tmp_path / "codex-home"
    os.makedirs(home)
    codex.fetch_rate_limits(binary, str(home), 5)
    assert recorder.read_text() == str(home)


def test_timeout_default_reads_the_reference_variable(monkeypatch):
    monkeypatch.setenv("USAGE_HUD_CODEX_FETCH_TIMEOUT", "3.5")
    assert codex.timeout_seconds() == 3.5
    monkeypatch.setenv("USAGE_HUD_CODEX_FETCH_TIMEOUT", "not a number")
    assert codex.timeout_seconds() == codex.TIMEOUT_DEFAULT


@pytest.mark.parametrize("window,expected", [
    (None, None),
    ({"usedPercent": 140, "windowDurationMins": 10080, "resetsAt": 1.6},
     {"used_percent": 100, "window_minutes": 10080, "resets_at": 2}),
    ({"usedPercent": -5, "windowDurationMins": 300, "resetsAt": 10},
     {"used_percent": 0, "window_minutes": 300, "resets_at": 10}),
])
def test_normalize_window_clamps(window, expected):
    assert codex.normalize_window(window) == expected


def test_normalize_window_refuses_a_non_object():
    with pytest.raises(ValueError, match="not an object"):
        codex.normalize_window([1, 2])
    with pytest.raises(ValueError, match="non-finite"):
        codex.normalize_window({"usedPercent": float("inf"), "windowDurationMins": 300,
                                "resetsAt": 10})


def test_result_is_json(fake_codex, tmp_path):
    """The whole result is what `jello usage fetch` writes to the codex cache, so it has to
    survive a round trip unchanged."""
    result = codex.fetch_rate_limits(fake_codex(ANSWERS), str(tmp_path), 2)
    assert json.loads(json.dumps(result)) == result
