mod common;

use common::{
    Env, TestHome, codex_auth, fixture_env, mkdir, mtime_ns, run_yelo, set_mode, stderr, stdout,
    write, write_json,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{
    Arc, Mutex,
    atomic::{AtomicBool, Ordering},
};
use std::thread::JoinHandle;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const TOKEN: &str = "sk-ant-oat-FIXTURE-TOKEN-3f9c1d";
const SEVEN_DAY_ISO: &str = "2026-09-05T10:00:00.123456+00:00";
const SEVEN_DAY_EPOCH: i64 = 1788602400;
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}

fn service(home: &Path) -> String {
    let identity = home.join(".claude-pri");
    let hash = format!(
        "{:x}",
        Sha256::digest(identity.display().to_string().as_bytes())
    );
    format!("Claude Code-credentials-{}", &hash[..8])
}

fn usage_payload(now: i64, fable: bool) -> Value {
    let mut payload = json!({
        "five_hour": {"used_percentage": 43, "resets_at": now + common::FIVE_HOUR_RESET},
        "seven_day": {"utilization": 12, "resets_at": SEVEN_DAY_ISO}
    });
    if fable {
        payload["limits"] = json!([
            {"kind": "weekly", "used_percentage": 99},
            {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}},
                "used_percentage": 5, "resets_at": SEVEN_DAY_EPOCH}
        ]);
    }
    payload
}

struct Reply {
    status: u16,
    body: Vec<u8>,
    headers: Vec<(String, String)>,
}
impl Reply {
    fn json(status: u16, body: Value) -> Self {
        Self {
            status,
            body: serde_json::to_vec(&body).unwrap(),
            headers: Vec::new(),
        }
    }
    fn raw(status: u16, body: Vec<u8>) -> Self {
        Self {
            status,
            body,
            headers: Vec::new(),
        }
    }
    fn header(mut self, key: &str, value: &str) -> Self {
        self.headers.push((key.to_string(), value.to_string()));
        self
    }
}

#[derive(Default)]
struct Calls {
    count: usize,
    requests: Vec<String>,
}

struct Endpoint {
    url: String,
    calls: Arc<Mutex<Calls>>,
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}

impl Endpoint {
    fn new(script: Vec<Reply>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let port = listener.local_addr().unwrap().port();
        let calls = Arc::new(Mutex::new(Calls::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let thread_calls = calls.clone();
        let thread_stop = stop.clone();
        let thread = std::thread::spawn(move || {
            while !thread_stop.load(Ordering::Relaxed) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let mut state = thread_calls.lock().unwrap();
                        let reply = &script[state.count.min(script.len() - 1)];
                        state.count += 1;
                        handle(stream, reply, &mut state.requests);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("endpoint accept failed: {error}"),
                }
            }
        });
        Self {
            url: format!("http://127.0.0.1:{port}/api/oauth/usage"),
            calls,
            stop,
            thread: Some(thread),
        }
    }
    fn calls(&self) -> usize {
        self.calls.lock().unwrap().count
    }
    fn requests(&self) -> Vec<String> {
        self.calls.lock().unwrap().requests.clone()
    }
    fn close(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        self.thread.take().unwrap().join().unwrap();
    }
}
impl Drop for Endpoint {
    fn drop(&mut self) {
        if self.thread.is_some() {
            self.close();
        }
    }
}

fn handle(mut stream: TcpStream, reply: &Reply, requests: &mut Vec<String>) {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let mut request = Vec::new();
    let mut chunk = [0_u8; 4096];
    loop {
        let n = stream.read(&mut chunk).unwrap();
        if n == 0 {
            break;
        }
        request.extend_from_slice(&chunk[..n]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    requests.push(String::from_utf8_lossy(&request).into_owned());
    let reason = match reply.status {
        200 => "OK",
        302 => "Found",
        401 => "Unauthorized",
        403 => "Forbidden",
        500 => "Internal Server Error",
        _ => "Test",
    };
    let mut header = format!(
        "HTTP/1.1 {} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n",
        reply.status,
        reply.body.len()
    );
    for (key, value) in &reply.headers {
        header.push_str(&format!("{key}: {value}\r\n"));
    }
    header.push_str("\r\n");
    stream.write_all(header.as_bytes()).unwrap();
    stream.write_all(&reply.body).unwrap();
}

struct FetchBench {
    temp: TestHome,
    env: Env,
    claude_log: PathBuf,
    security_log: PathBuf,
}
impl FetchBench {
    fn new() -> Self {
        let temp = TestHome::new();
        write(
            &temp.home.join(".claude/.profiles/pri/email"),
            "p@example.test\n",
        );
        let stubs = temp.root.join("stubs");
        mkdir(&stubs);
        let blob = temp.root.join("keychain-blob.json");
        write(
            &blob,
            &serde_json::to_string(&json!({"claudeAiOauth": {"accessToken": TOKEN}})).unwrap(),
        );
        let security_log = temp.root.join("security.log");
        let security = stubs.join("security");
        write(
            &security,
            "#!/bin/sh\nprintf '%s\\t' \"$@\" >> \"$FAKE_SECURITY_LOG\"\nprintf '\\n' >> \"$FAKE_SECURITY_LOG\"\nprev=\nfound=\nwant_blob=\nfor arg in \"$@\"; do\n  if [ \"$prev\" = -s ] && [ \"$arg\" = \"$FAKE_SERVICE\" ]; then found=1; fi\n  if [ \"$arg\" = -w ]; then want_blob=1; fi\n  prev=$arg\ndone\n[ -n \"$found\" ] || exit 1\nif [ -n \"$want_blob\" ]; then cat \"$FAKE_BLOB\"; fi\n",
        );
        set_mode(&security, 0o755);
        let claude_log = temp.root.join("claude.log");
        let claude = stubs.join("claude");
        write(
            &claude,
            "#!/bin/sh\nprintf '%s ' \"$@\" >> \"$FAKE_CLAUDE_LOG\"\nprintf '\\n%s\\n' \"$CLAUDE_SECURESTORAGE_CONFIG_DIR\" >> \"$FAKE_CLAUDE_LOG\"\nexit \"${FAKE_CLAUDE_EXIT:-0}\"\n",
        );
        set_mode(&claude, 0o755);
        let mut env = fixture_env(&temp.home, &security.display().to_string());
        env.insert("FAKE_SERVICE".into(), service(&temp.home));
        env.insert("FAKE_BLOB".into(), blob.display().to_string());
        env.insert("FAKE_CLAUDE_LOG".into(), claude_log.display().to_string());
        env.insert(
            "FAKE_SECURITY_LOG".into(),
            security_log.display().to_string(),
        );
        env.insert("CLAUDE_BIN".into(), claude.display().to_string());
        Self {
            temp,
            env,
            claude_log,
            security_log,
        }
    }
    fn fetch(
        &self,
        endpoint: Option<&Endpoint>,
        overrides: &[(&str, &str)],
    ) -> std::process::Output {
        let mut env = self.env.clone();
        if let Some(endpoint) = endpoint {
            env.insert("YELO_USAGE_API_URL".into(), endpoint.url.clone());
        }
        for (key, value) in overrides {
            env.insert((*key).into(), (*value).into());
        }
        run_yelo(&["usage", "fetch"], &env, None, "")
    }
    fn cache(&self, name: &str) -> PathBuf {
        self.temp.home.join(".claude/.profiles/pri").join(name)
    }
    fn with_codex(&self) -> PathBuf {
        let dir = self.temp.home.join(".codex");
        mkdir(&dir.join("sessions"));
        write_json(
            &dir.join("auth.json"),
            &codex_auth("c@example.test", "pro", "acc-1"),
        );
        write(&dir.join("profile-label"), "\n");
        let binary = self.temp.root.join("stubs/codex");
        let response = serde_json::to_string(&json!({"id":2,"result":{"rateLimits":{
            "primary":{"usedPercent":30.4,"windowDurationMins":10080,"resetsAt":4102444800_i64},
            "secondary":null}}}))
        .unwrap();
        write(
            &binary,
            &format!(
                "#!/bin/sh\nn=0\nwhile IFS= read -r line; do\n  n=$((n + 1))\n  if [ \"$n\" -eq 1 ]; then printf '%s\\n' '{{\"id\":1,\"result\":{{}}}}';\n  elif [ \"$n\" -eq 2 ]; then printf '%s\\n' '{response}'; fi\ndone\n"
            ),
        );
        set_mode(&binary, 0o755);
        binary
    }
}

fn tree(home: &Path) -> BTreeMap<PathBuf, (u128, u64)> {
    fn visit(dir: &Path, found: &mut BTreeMap<PathBuf, (u128, u64)>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(&path, found);
            } else {
                found.insert(
                    path.clone(),
                    (mtime_ns(&path), fs::metadata(&path).unwrap().len()),
                );
            }
        }
    }
    let mut found = BTreeMap::new();
    visit(home, &mut found);
    found
}

fn home_files_contain(home: &Path, needle: &str) -> bool {
    fn visit(dir: &Path, needle: &str) -> bool {
        fs::read_dir(dir).unwrap().any(|entry| {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(&path, needle)
            } else {
                String::from_utf8_lossy(&fs::read(path).unwrap()).contains(needle)
            }
        })
    }
    visit(home, needle)
}

#[test]
fn test_fetch_claude_writes_caches() {
    let bench = FetchBench::new();
    let now = now();
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now, true))]);
    let result = bench.fetch(Some(&endpoint), &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(stdout(&result), "cl·p@example.test: ok\n");
    let base: Value =
        serde_json::from_slice(&fs::read(bench.cache(".usage-api-cache.json")).unwrap()).unwrap();
    assert_eq!(
        base["five_hour"],
        json!({"used_percentage":43,"resets_at":now + common::FIVE_HOUR_RESET})
    );
    assert_eq!(
        base["seven_day"],
        json!({"used_percentage":12,"resets_at":SEVEN_DAY_EPOCH})
    );
    assert_eq!(base["source"], "api");
    assert_eq!(base["ts"], base["fetched_at"]);
    let fable: Value =
        serde_json::from_slice(&fs::read(bench.cache(".usage-api-cache-fable.json")).unwrap())
            .unwrap();
    assert_eq!(fable["five_hour"], base["five_hour"]);
    assert_eq!(
        fable["seven_day"],
        json!({"used_percentage":5,"resets_at":SEVEN_DAY_EPOCH})
    );
    let mut names: Vec<_> = fs::read_dir(bench.cache(""))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    assert_eq!(
        names,
        [
            ".usage-api-cache-fable.json",
            ".usage-api-cache.json",
            "email"
        ]
    );
}

#[test]
fn test_fetch_and_snapshot_share_email_label() {
    let temp = TestHome::new();
    write(
        &temp.home.join(".claude/.profiles/pri/email"),
        "p@example.test\n",
    );
    let env = fixture_env(&temp.home, common::FALSE_BIN);
    let fetched = run_yelo(&["usage", "fetch"], &env, None, "");
    assert_eq!(fetched.status.code(), Some(1));
    let fetch_label = stdout(&fetched).split_once(": ").unwrap().0.to_string();
    let shown = run_yelo(&["usage", "show", "--json"], &env, None, "");
    assert_eq!(shown.status.code(), Some(0), "{}", stderr(&shown));
    let rows: Value = serde_json::from_slice(&shown.stdout).unwrap();
    let labels: std::collections::BTreeSet<_> = rows
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["label"].as_str().unwrap())
        .collect();
    assert_eq!(labels, [fetch_label.as_str()].into());
    assert_eq!(fetch_label, "cl·p@example.test");
}

#[test]
fn test_fetch_without_a_fable_window_leaves_that_cache_alone() {
    let bench = FetchBench::new();
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now(), false))]);
    assert_eq!(
        stdout(&bench.fetch(Some(&endpoint), &[])),
        "cl·p@example.test: ok\n"
    );
    assert!(!bench.cache(".usage-api-cache-fable.json").exists());
}

#[test]
fn test_fable_write_failure_still_counts_as_a_run() {
    let bench = FetchBench::new();
    mkdir(&bench.cache(".usage-api-cache-fable.json"));
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now(), true))]);
    let result = bench.fetch(Some(&endpoint), &[]);
    assert_eq!(stdout(&result), "cl·p@example.test: fable-write-failed\n");
    assert_eq!(result.status.code(), Some(0));
}

#[test]
fn test_fetch_writes_only_api_caches() {
    let bench = FetchBench::new();
    let codex = bench.with_codex();
    let before = tree(&bench.temp.home);
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now(), true))]);
    let result = bench.fetch(
        Some(&endpoint),
        &[("CODEX_BIN", &codex.display().to_string())],
    );
    assert_eq!(
        stdout(&result),
        "cl·p@example.test: ok\ncx·c@example.test: ok\n"
    );
    assert_eq!(result.status.code(), Some(0));
    let after = tree(&bench.temp.home);
    let moved: std::collections::BTreeSet<_> = after
        .iter()
        .filter(|(path, value)| before.get(*path) != Some(*value))
        .map(|(path, _)| path.file_name().unwrap().to_str().unwrap())
        .collect();
    assert_eq!(
        moved,
        [
            ".usage-api-cache.json",
            ".usage-api-cache-fable.json",
            ".usage-hud-api-cache.json"
        ]
        .into()
    );
    assert!(before.keys().all(|path| after.contains_key(path)));
    assert!(
        after
            .keys()
            .all(|path| !path.display().to_string().contains(".tmp."))
    );
}

#[test]
fn test_server_error_is_fetch_failed() {
    let bench = FetchBench::new();
    let endpoint = Endpoint::new(vec![Reply::json(500, json!({"error":"nope"}))]);
    let result = bench.fetch(Some(&endpoint), &[]);
    assert_eq!(stdout(&result), "cl·p@example.test: fetch-failed\n");
    assert_eq!(result.status.code(), Some(1));
}

#[test]
fn test_unreachable_endpoint_is_fetch_failed() {
    let bench = FetchBench::new();
    let result = bench.fetch(None, &[("YELO_USAGE_API_URL", "http://127.0.0.1:1/usage")]);
    assert_eq!(stdout(&result), "cl·p@example.test: fetch-failed\n");
    assert_eq!(result.status.code(), Some(1));
}

#[test]
fn test_non_finite_numbers_are_not_a_window() {
    for literal in ["1e999", "NaN"] {
        let bench = FetchBench::new();
        let codex = bench.with_codex();
        let body = format!(
            "{{\"five_hour\": {{\"used_percentage\": {literal}, \"resets_at\": {literal}}}}}"
        )
        .into_bytes();
        let endpoint = Endpoint::new(vec![Reply::raw(200, body)]);
        let result = bench.fetch(
            Some(&endpoint),
            &[("CODEX_BIN", &codex.display().to_string())],
        );
        assert_eq!(
            stdout(&result),
            "cl·p@example.test: fetch-failed\ncx·c@example.test: ok\n",
            "{literal}"
        );
        assert_eq!(result.status.code(), Some(0), "{literal}");
        assert!(!stderr(&result).contains("Traceback"), "{literal}");
        assert!(!bench.cache(".usage-api-cache.json").exists(), "{literal}");
    }
}

#[test]
fn test_token_never_leaks() {
    let bench = FetchBench::new();
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now(), true))]);
    let result = bench.fetch(
        Some(&endpoint),
        &[
            ("USAGE_HUD_FETCH_DEBUG", "1"),
            ("USAGE_HUD_FETCH_DUMP", "1"),
        ],
    );
    assert_eq!(result.status.code(), Some(0));
    assert!(stderr(&result).contains("[dbg]") && stderr(&result).contains("[dump]"));
    assert!(!stdout(&result).contains(TOKEN));
    assert!(!stderr(&result).contains(TOKEN));
    assert!(!home_files_contain(&bench.temp.home, TOKEN));
    let calls = fs::read_to_string(&bench.security_log).unwrap();
    assert!(!calls.is_empty());
    assert!(
        calls
            .lines()
            .all(|line| line.starts_with("find-generic-password\t"))
    );
    assert!(
        calls
            .lines()
            .any(|line| line.split('\t').any(|arg| arg == "-w"))
    );
}

#[test]
fn test_a_redirect_is_never_followed() {
    let bench = FetchBench::new();
    let target = Endpoint::new(vec![Reply::json(200, usage_payload(now(), true))]);
    let hop = Endpoint::new(vec![
        Reply::json(302, json!({})).header("Location", &target.url),
    ]);
    let result = bench.fetch(Some(&hop), &[("USAGE_HUD_FETCH_DEBUG", "1")]);
    assert_eq!(hop.calls(), 1);
    assert_eq!(target.calls(), 0);
    assert!(target.requests().is_empty());
    assert_eq!(stdout(&result), "cl·p@example.test: fetch-failed\n");
    assert_eq!(result.status.code(), Some(1));
    assert!(!stdout(&result).contains(TOKEN));
    assert!(!stderr(&result).contains(TOKEN));
    assert!(!bench.cache(".usage-api-cache.json").exists());
}

#[test]
fn test_a_body_that_quotes_the_token_never_prints_it() {
    let bench = FetchBench::new();
    let mut payload = usage_payload(now(), true);
    payload["echo"] = json!(format!("Authorization: Bearer {TOKEN}"));
    let endpoint = Endpoint::new(vec![Reply::json(200, payload)]);
    let result = bench.fetch(
        Some(&endpoint),
        &[
            ("USAGE_HUD_FETCH_DEBUG", "1"),
            ("USAGE_HUD_FETCH_DUMP", "1"),
        ],
    );
    assert_eq!(stdout(&result), "cl·p@example.test: ok\n");
    assert_eq!(result.status.code(), Some(0));
    assert!(stderr(&result).contains("<token>"));
    assert!(!stdout(&result).contains(TOKEN));
    assert!(!stderr(&result).contains(TOKEN));
    assert!(!home_files_contain(&bench.temp.home, TOKEN));
}

#[test]
fn test_a_failed_request_raises_nothing() {
    let bench = FetchBench::new();
    let mut endpoint = Endpoint::new(vec![Reply::json(200, json!({}))]);
    endpoint.close();
    let result = bench.fetch(Some(&endpoint), &[]);
    assert_eq!(stdout(&result), "cl·p@example.test: fetch-failed\n");
    assert_eq!(result.status.code(), Some(1));
    assert!(!stderr(&result).contains(TOKEN));
}

#[test]
fn test_fetch_codex() {
    let bench = FetchBench::new();
    let codex = bench.with_codex();
    let endpoint = Endpoint::new(vec![Reply::json(200, usage_payload(now(), true))]);
    let result = bench.fetch(
        Some(&endpoint),
        &[("CODEX_BIN", &codex.display().to_string())],
    );
    assert_eq!(result.status.code(), Some(0));
    assert_eq!(
        stdout(&result),
        "cl·p@example.test: ok\ncx·c@example.test: ok\n"
    );
    let written: Value = serde_json::from_slice(
        &fs::read(bench.temp.home.join(".codex/.usage-hud-api-cache.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        written["rate_limits"]["primary"],
        json!({"used_percent":30,
        "window_minutes":10080,"resets_at":4102444800_i64})
    );
    assert!(written["rate_limits"]["secondary"].is_null());
    assert_eq!(written["source"], "api");
}

#[test]
fn test_fetch_codex_without_a_binary() {
    let bench = FetchBench::new();
    let dir = bench.temp.home.join(".codex");
    mkdir(&dir.join("sessions"));
    write_json(
        &dir.join("auth.json"),
        &codex_auth("c@example.test", "pro", "acc-1"),
    );
    let endpoint = Endpoint::new(vec![Reply::json(500, json!({}))]);
    let result = bench.fetch(Some(&endpoint), &[]);
    assert_eq!(
        stdout(&result),
        "cl·p@example.test: fetch-failed\ncx·c@example.test: fetch-failed\n"
    );
    assert_eq!(result.status.code(), Some(1));
}

#[test]
fn test_fetch_no_profiles() {
    let temp = TestHome::new();
    let home = temp.root.join("empty");
    mkdir(&home);
    let result = run_yelo(
        &["usage", "fetch"],
        &fixture_env(&home, common::FALSE_BIN),
        None,
        "",
    );
    assert!(result.stdout.is_empty());
    assert_eq!(result.status.code(), Some(1));
}

#[test]
fn test_expired_auth_never_starts_an_agent() {
    for status in [401, 403] {
        let bench = FetchBench::new();
        let endpoint = Endpoint::new(vec![Reply::json(status, json!({}))]);
        let result = bench.fetch(Some(&endpoint), &[]);
        assert_eq!(
            stdout(&result),
            "cl·p@example.test: auth-stale\n",
            "{status}"
        );
        assert_eq!(result.status.code(), Some(1), "{status}");
        assert_eq!(endpoint.calls(), 1, "{status}");
        assert!(!bench.claude_log.exists(), "{status}");
        assert!(!bench.cache(".usage-api-cache.json").exists(), "{status}");
    }
}
