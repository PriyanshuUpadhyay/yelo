"""The Codex app server is tested through `yelo usage fetch` and its cache."""

import json
import textwrap

import pytest

from conftest import codex_auth, fixture_env, run_yelo, write

@pytest.fixture
def fake_codex(tmp_path):
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

def fetch(binary, tmp_path, **overrides):
    home = tmp_path / "home"
    directory = home / ".codex"
    (directory / "sessions").mkdir(parents=True, exist_ok=True)
    write(str(directory / "auth.json"), json.dumps(codex_auth("a@example.test", "pro", "acc-1")))
    env = fixture_env(home)
    env.update(CODEX_BIN=binary, **overrides)
    result = run_yelo(["usage", "fetch"], env)
    cache = directory / ".usage-hud-api-cache.json"
    return result, json.loads(cache.read_text()) if cache.exists() else None

def test_fetches_and_normalizes_primary_window(fake_codex, tmp_path):
    result, cache = fetch(fake_codex(ANSWERS), tmp_path)
    assert result.returncode == 0, result.stderr
    assert cache["rate_limits"]["primary"]["used_percent"] == 12
    assert cache["rate_limits"]["primary"]["window_minutes"] == 10080
    assert cache["rate_limits"]["secondary"] is None
    assert cache["source"] == "api"

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
    result, cache = fetch(binary, tmp_path)
    assert result.returncode == 1
    assert result.stdout == "cx·a@example.test: fetch-failed\n"
    assert cache is None

def test_surfaces_rate_limit_request_error(fake_codex, tmp_path):
    binary = fake_codex('''
        import json
        import sys
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == 1:
                print(json.dumps({"id": 1, "result": {}}), flush=True)
            elif message.get("id") == 2:
                print(json.dumps({"id": 2, "error": {"code": -32000, "message": "failed"}}), flush=True)
    ''')
    result, cache = fetch(binary, tmp_path)
    assert result.returncode == 1
    assert result.stdout == "cx·a@example.test: fetch-failed\n"
    assert cache is None

def test_times_out_without_a_response(fake_codex, tmp_path):
    binary = fake_codex('''
        import time
        time.sleep(10)
    ''')
    result, cache = fetch(binary, tmp_path, USAGE_HUD_CODEX_FETCH_TIMEOUT="0.1")
    assert result.returncode == 1
    assert result.stdout == "cx·a@example.test: fetch-failed\n"
    assert cache is None

def test_codex_home_reaches_the_binary(fake_codex, tmp_path):
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
    result, _ = fetch(binary, tmp_path)
    assert result.returncode == 0, result.stderr
    assert recorder.read_text() == str(tmp_path / "home" / ".codex")

def test_result_is_json(fake_codex, tmp_path):
    result, cache = fetch(fake_codex(ANSWERS), tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(json.dumps(cache)) == cache

def test_named_limits_keep_model_limits_and_drop_the_rest(fake_codex, tmp_path):
    window = {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 4102444800}
    limits = {
        "codex": {"limitName": None, "primary": window},
        "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark", "primary": window, "secondary": None},
        "premium": {"limitName": "Premium", "primary": None},
        "broken": {"limitName": "Broken", "primary": {"usedPercent": 5}},
    }
    binary = fake_codex(f'''
        import json
        import sys
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("id") == 1:
                print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
            elif message.get("id") == 2:
                print(json.dumps({{"id": 2, "result": {{"rateLimits": {{
                    "primary": {{"usedPercent": 1, "windowDurationMins": 300,
                                 "resetsAt": 4102444800}}, "secondary": None}},
                    "rateLimitsByLimitId": {limits!r}}}}}), flush=True)
    ''')
    result, cache = fetch(binary, tmp_path)
    assert result.returncode == 0, result.stderr
    assert cache["limits_by_id"] == {
        "codex_bengalfox": {"limit_name": "GPT-5.3-Codex-Spark",
                            "primary": {"used_percent": 5, "window_minutes": 300,
                                        "resets_at": 4102444800}, "secondary": None}}
