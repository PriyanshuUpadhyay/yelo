mod common;

use common::{
    TestHome, codex_auth, fixture_env, mkdir, run_yelo, set_mode, stderr, stdout, write, write_json,
};
use serde_json::{Value, json};
use std::fs;
use std::path::{Path, PathBuf};

fn fake_codex(path: &Path, first: &Value, second: &Value, prefix: &str) -> PathBuf {
    let first = serde_json::to_string(first).unwrap();
    let second = serde_json::to_string(second).unwrap();
    let script = format!(
        "#!/bin/sh\n{prefix}n=0\nwhile IFS= read -r line; do\n  n=$((n + 1))\n  if [ \"$n\" -eq 1 ]; then printf '%s\\n' '{first}';\n  elif [ \"$n\" -eq 2 ]; then printf '%s\\n' '{second}'; fi\ndone\n"
    );
    write(path, &script);
    set_mode(path, 0o755);
    path.to_path_buf()
}

fn primary() -> Value {
    json!({"usedPercent": 12.4, "windowDurationMins": 10080, "resetsAt": 4102444800_i64})
}
fn first() -> Value {
    json!({"id": 1, "result": {"codexHome": "/tmp/codex"}})
}
fn second() -> Value {
    json!({"id": 2, "result": {"rateLimits": {"primary": primary(), "secondary": null}}})
}

struct FetchResult {
    temp: TestHome,
    result: std::process::Output,
    cache: Option<Value>,
}

fn fetch(binary: &Path, overrides: &[(&str, &str)]) -> FetchResult {
    let temp = TestHome::new();
    let dir = temp.home.join(".codex");
    mkdir(&dir.join("sessions"));
    write_json(
        &dir.join("auth.json"),
        &codex_auth("a@example.test", "pro", "acc-1"),
    );
    let mut env = fixture_env(&temp.home, common::FALSE_BIN);
    env.insert("CODEX_BIN".into(), binary.display().to_string());
    for (key, value) in overrides {
        env.insert((*key).into(), (*value).into());
    }
    let result = run_yelo(&["usage", "fetch"], &env, None, "");
    let cache_path = dir.join(".usage-hud-api-cache.json");
    let cache = cache_path
        .exists()
        .then(|| serde_json::from_slice(&fs::read(cache_path).unwrap()).unwrap());
    FetchResult {
        temp,
        result,
        cache,
    }
}

fn stub_pair() -> (TestHome, PathBuf) {
    let temp = TestHome::new();
    let binary = fake_codex(&temp.root.join("codex"), &first(), &second(), "");
    (temp, binary)
}

#[test]
fn test_fetches_and_normalizes_primary_window() {
    let (_stub, binary) = stub_pair();
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(0), "{}", stderr(&run.result));
    let cache = run.cache.unwrap();
    assert_eq!(cache["rate_limits"]["primary"]["used_percent"], 12);
    assert_eq!(cache["rate_limits"]["primary"]["window_minutes"], 10080);
    assert!(cache["rate_limits"]["secondary"].is_null());
    assert_eq!(cache["source"], "api");
}

#[test]
fn test_rejects_incomplete_window() {
    let stub = TestHome::new();
    let response = json!({"id": 2, "result": {"rateLimits": {"primary": {
        "usedPercent": 12, "windowDurationMins": 10080}, "secondary": null}}});
    let binary = fake_codex(
        &stub.root.join("codex"),
        &json!({"id":1,"result":{}}),
        &response,
        "",
    );
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(1));
    assert_eq!(stdout(&run.result), "cx·a@example.test: fetch-failed\n");
    assert!(run.cache.is_none());
}

#[test]
fn test_surfaces_rate_limit_request_error() {
    let stub = TestHome::new();
    let binary = fake_codex(
        &stub.root.join("codex"),
        &json!({"id":1,"result":{}}),
        &json!({"id":2,"error":{"code":-32000,"message":"failed"}}),
        "",
    );
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(1));
    assert_eq!(stdout(&run.result), "cx·a@example.test: fetch-failed\n");
    assert!(run.cache.is_none());
}

#[test]
fn test_times_out_without_a_response() {
    let stub = TestHome::new();
    let binary = stub.root.join("codex");
    write(&binary, "#!/bin/sh\nexec sleep 10\n");
    set_mode(&binary, 0o755);
    let run = fetch(&binary, &[("USAGE_HUD_CODEX_FETCH_TIMEOUT", "0.1")]);
    assert_eq!(run.result.status.code(), Some(1));
    assert_eq!(stdout(&run.result), "cx·a@example.test: fetch-failed\n");
    assert!(run.cache.is_none());
}

#[test]
fn test_codex_home_reaches_the_binary() {
    let stub = TestHome::new();
    let recorder = stub.root.join("home-seen");
    let response = json!({"id":2,"result":{"rateLimits":{"primary":{
        "usedPercent":1,"windowDurationMins":300,"resetsAt":4102444800_i64},"secondary":null}}});
    let binary = fake_codex(
        &stub.root.join("codex"),
        &json!({"id":1,"result":{}}),
        &response,
        &format!("printf '%s' \"$CODEX_HOME\" > '{}'\n", recorder.display()),
    );
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(0), "{}", stderr(&run.result));
    assert_eq!(
        fs::read_to_string(recorder).unwrap(),
        run.temp.home.join(".codex").display().to_string()
    );
}

#[test]
fn test_result_is_json() {
    let (_stub, binary) = stub_pair();
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(0), "{}", stderr(&run.result));
    let cache = run.cache.unwrap();
    assert_eq!(
        serde_json::from_str::<Value>(&serde_json::to_string(&cache).unwrap()).unwrap(),
        cache
    );
}

#[test]
fn test_reset_credits_keep_available_expiries_and_count() {
    let stub = TestHome::new();
    let response = json!({"id": 2, "result": {
    "rateLimits": {"primary": primary(), "secondary": null},
    "rateLimitResetCredits": {"availableCount": 3, "credits": [
        {"id": "c1", "status": "available", "expiresAt": 1759536000},
        {"id": "c2", "status": "available", "expiresAt": null},
        {"id": "c3", "status": "redeemed", "expiresAt": 1759190400}
    ]}}});
    let binary = fake_codex(&stub.root.join("codex"), &first(), &response, "");
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(0), "{}", stderr(&run.result));
    assert_eq!(
        run.cache.unwrap()["reset_credits"],
        json!({"available": 3, "expires_at": [1759536000, null]})
    );
}

#[test]
fn test_reset_credits_count_only_and_absent() {
    let stub = TestHome::new();
    let response = json!({"id": 2, "result": {
        "rateLimits": {"primary": primary(), "secondary": null},
        "rateLimitResetCredits": {"availableCount": 2, "credits": null}}});
    let binary = fake_codex(&stub.root.join("codex"), &first(), &response, "");
    assert_eq!(
        fetch(&binary, &[]).cache.unwrap()["reset_credits"],
        json!({"available": 2})
    );
    let (_stub, binary) = stub_pair();
    assert!(
        fetch(&binary, &[])
            .cache
            .unwrap()
            .get("reset_credits")
            .is_none()
    );
}

#[test]
fn test_named_limits_keep_model_limits_and_drop_the_rest() {
    let stub = TestHome::new();
    let window = json!({"usedPercent":5,"windowDurationMins":300,"resetsAt":4102444800_i64});
    let response = json!({"id":2,"result":{
    "rateLimits":{"primary":{"usedPercent":1,"windowDurationMins":300,"resetsAt":4102444800_i64},"secondary":null},
    "rateLimitsByLimitId":{
        "codex":{"limitName":null,"primary":window},
        "codex_bengalfox":{"limitName":"GPT-5.3-Codex-Spark","primary":window,"secondary":null},
        "premium":{"limitName":"Premium","primary":null},
        "broken":{"limitName":"Broken","primary":{"usedPercent":5}}
    }}});
    let binary = fake_codex(
        &stub.root.join("codex"),
        &json!({"id":1,"result":{}}),
        &response,
        "",
    );
    let run = fetch(&binary, &[]);
    assert_eq!(run.result.status.code(), Some(0), "{}", stderr(&run.result));
    assert_eq!(
        run.cache.unwrap()["limits_by_id"],
        json!({"codex_bengalfox":{
        "limit_name":"GPT-5.3-Codex-Spark","primary":{
            "used_percent":5,"window_minutes":300,"resets_at":4102444800_i64},"secondary":null}})
    );
}

#[test]
fn test_accounts_fetch_at_the_same_time_and_print_in_job_order() {
    let stub = TestHome::new();
    let binary = fake_codex(&stub.root.join("codex"), &first(), &second(), "sleep 1.5\n");
    let temp = TestHome::new();
    for (dir, email) in [
        (".codex", "a@example.test"),
        (".codex-work", "b@example.test"),
    ] {
        let dir = temp.home.join(dir);
        mkdir(&dir.join("sessions"));
        write_json(&dir.join("auth.json"), &codex_auth(email, "pro", email));
    }
    let mut env = fixture_env(&temp.home, common::FALSE_BIN);
    env.insert("CODEX_BIN".into(), binary.display().to_string());
    let started = std::time::Instant::now();
    let result = run_yelo(&["usage", "fetch"], &env, None, "");
    let elapsed = started.elapsed();
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        stdout(&result),
        "cx·a@example.test: ok\ncx·b@example.test: ok\n"
    );
    // Serial would take at least 3 s, two sleeps of 1.5 s.
    assert!(elapsed.as_secs_f64() < 2.7, "fetch took {elapsed:?}");
}
