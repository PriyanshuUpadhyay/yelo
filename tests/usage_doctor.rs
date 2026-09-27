mod common;

use common::{
    Env, TestHome, codex_auth, fixture_env, mtime_ns, run_yelo, set_age, set_mode, stderr, stdout,
    write, write_json,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const YELO_LABEL: &str = "io.github.priyanshuupadhyay.yelo-hud";
fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}
fn rollout_path(dir: &Path) -> PathBuf {
    dir.join("sessions/2026/09/01/rollout-2026-09-01T09-00-00-1.jsonl")
}

fn codex_account(
    home: &Path,
    name: &str,
    document: Option<Value>,
    rollout_age: Option<u64>,
) -> PathBuf {
    let dir = home.join(format!(".codex-{name}"));
    common::mkdir(&dir.join("sessions"));
    write_json(
        &dir.join("auth.json"),
        &codex_auth(
            &format!("{name}@example.test"),
            "pro",
            &format!("acc-{name}"),
        ),
    );
    if let Some(document) = document {
        write_json(&dir.join(".usage-hud-api-cache.json"), &document);
    }
    if let Some(age) = rollout_age {
        write_json(
            &rollout_path(&dir),
            &json!({"payload": {"rate_limits": {
            "primary": {"used_percent": 7, "window_minutes": 10080,
                "resets_at": now() + common::CODEX_RESET}, "secondary": null}}}),
        );
        set_age(&rollout_path(&dir), age);
    }
    dir
}

struct DoctorBench {
    temp: TestHome,
    env: Env,
    api_cache: PathBuf,
    rollout: PathBuf,
    logs: PathBuf,
}

impl DoctorBench {
    fn new() -> Self {
        let temp = TestHome::new();
        let now = now();
        let api = codex_account(
            &temp.home,
            "api",
            Some(json!({"rate_limits": {
            "primary": {"used_percent": 12, "window_minutes": 10080,
                "resets_at": now + common::CODEX_RESET}, "secondary": null},
            "fetched_at": now - 30, "source": "api"})),
            None,
        );
        let rollout = codex_account(&temp.home, "rollout", None, Some(4000));
        let stub = temp.root.join("stubs/launchctl");
        write(
            &stub,
            "#!/bin/sh\nfor arg in \"$@\"; do target=$arg; done\nlabel=${target##*/}\ncase ,$FAKE_LOADED, in\n  *,$label,*) printf '\\tpid = 4242\\n'; exit 0;;\nesac\nexit 113\n",
        );
        set_mode(&stub, 0o755);
        let mut env = fixture_env(&temp.home, common::FALSE_BIN);
        env.insert(
            "PATH".into(),
            format!(
                "{}:{}",
                stub.parent().unwrap().display(),
                common::sealed_path()
            ),
        );
        env.insert("FAKE_LOADED".into(), YELO_LABEL.into());
        Self {
            api_cache: api.join(".usage-hud-api-cache.json"),
            rollout: rollout_path(&rollout),
            logs: temp.home.join("Library/Logs"),
            temp,
            env,
        }
    }
    fn doctor(&self, overrides: &[(&str, &str)]) -> std::process::Output {
        let mut env = self.env.clone();
        for (key, value) in overrides {
            env.insert((*key).into(), (*value).into());
        }
        run_yelo(&["usage", "doctor"], &env, None, "")
    }
    fn restamp_api(&self, age: i64) {
        let mut document: Value =
            serde_json::from_slice(&fs::read(&self.api_cache).unwrap()).unwrap();
        document["fetched_at"] = json!(now() - age);
        write_json(&self.api_cache, &document);
    }
}

fn file_times(root: &Path) -> BTreeMap<PathBuf, u128> {
    fn visit(dir: &Path, found: &mut BTreeMap<PathBuf, u128>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(&path, found);
            } else {
                found.insert(path.clone(), mtime_ns(&path));
            }
        }
    }
    let mut found = BTreeMap::new();
    visit(root, &mut found);
    found
}

#[test]
fn test_doctor_matrix() {
    let bench = DoctorBench::new();
    let some = bench.doctor(&[]);
    assert_eq!(some.status.code(), Some(0), "{}", stderr(&some));
    let text = stdout(&some);
    assert!(text.contains("PASS: no row without usable data carries a pct"));
    assert!(text.contains("PASS: cx·api@example.test 7d: current from api"));
    assert!(text.contains("WARN: cx·rollout@example.test 7d: stale data from rollout"));
    assert!(!text.contains("row(s) without usable data carry a pct"));
    assert!(text.contains("WARN: 1 of 2 row(s) stale"));
    set_age(&bench.rollout, 30);
    let healthy = bench.doctor(&[]);
    assert!(stdout(&healthy).contains("PASS: all 2 row(s) current"));
    assert!(!stdout(&healthy).contains("FAIL"));
    assert_eq!(healthy.status.code(), Some(0));
    set_age(&bench.rollout, 9000);
    bench.restamp_api(9000);
    let every = bench.doctor(&[]);
    assert!(stdout(&every).contains("FAIL: every usage row is stale (2 of 2)"));
    assert_eq!(every.status.code(), Some(1));
    bench.restamp_api(30);
    let leaked = bench.logs.join("usage-hud.err.log");
    write(&leaked, "Authorization: Bearer eyJhbGciOi\n");
    let leak = bench.doctor(&[]);
    assert!(
        stdout(&leak).contains("FAIL: 1 pipeline-written file(s) hold possible token material")
    );
    assert!(!stdout(&leak).contains("eyJhbGciOi"));
    assert_eq!(leak.status.code(), Some(1));
    fs::remove_file(leaked).unwrap();
    let running = bench.doctor(&[]);
    assert!(stdout(&running).contains(&format!("PASS: {YELO_LABEL} running (pid 4242)")));
    assert_eq!(running.status.code(), Some(0));
    let neither = bench.doctor(&[("FAKE_LOADED", "")]);
    assert!(stdout(&neither).contains(&format!(
        "FAIL: {YELO_LABEL} not loaded — the usage HUD is not running"
    )));
    assert_eq!(neither.status.code(), Some(1));
}

#[test]
fn test_unparseable_cache_warns_without_failing() {
    let bench = DoctorBench::new();
    write(
        &bench.temp.home.join(".claude/.profiles/pri/email"),
        "p@e\n",
    );
    write(
        &bench
            .temp
            .home
            .join(".claude/.profiles/pri/.usage-api-cache.json"),
        "{not json",
    );
    let result = bench.doctor(&[]);
    assert!(stdout(&result).contains("WARN: pri/.usage-api-cache.json is not parseable JSON"));
    assert_eq!(result.status.code(), Some(0));
}

#[test]
fn test_non_numeric_window_warns() {
    let bench = DoctorBench::new();
    write(
        &bench.temp.home.join(".claude/.profiles/pri/email"),
        "p@e\n",
    );
    write_json(
        &bench
            .temp
            .home
            .join(".claude/.profiles/pri/.usage-cache.json"),
        &json!({"five_hour": {"used_percentage": "43", "resets_at": null}}),
    );
    let result = bench.doctor(&[]);
    assert!(
        stdout(&result).contains("WARN: pri/.usage-cache.json: 1 window(s) with a non-numeric")
    );
    assert_eq!(result.status.code(), Some(0));
}

#[test]
fn test_leak_scan_fails_on_token_material() {
    let bench = DoctorBench::new();
    let clean = bench.doctor(&[]);
    assert!(stdout(&clean).contains("PASS: no token material in"));
    assert_eq!(clean.status.code(), Some(0));
    write(
        &bench.logs.join("usage-hud.err.log"),
        "Authorization: Bearer eyJhbGciOi\n",
    );
    let leaked = bench.doctor(&[]);
    assert!(
        stdout(&leaked).contains("FAIL: 1 pipeline-written file(s) hold possible token material")
    );
    assert!(!stdout(&leaked).contains("eyJhbGciOi"));
    assert_eq!(leaked.status.code(), Some(1));
    write(&bench.logs.join("yelo-hud.out.log"), "accessToken\n");
    assert!(stdout(&bench.doctor(&[])).contains("FAIL: 2 pipeline-written file(s)"));
}

#[test]
fn test_credentials_report_per_profile() {
    let bench = DoctorBench::new();
    let profile = bench.temp.home.join(".claude/.profiles/pri");
    write(&profile.join("email"), "p@e\n");
    assert!(stdout(&bench.doctor(&[])).contains("WARN: pri: no OAuth credential source"));
    write(&profile.join(".credentials.json"), "{}\n");
    assert!(
        stdout(&bench.doctor(&[]))
            .contains("WARN: pri: .credentials.json present but no Keychain item")
    );
    assert!(
        !stdout(&bench.doctor(&[("AGENT_PROFILES_SECURITY_BIN", common::TRUE_BIN)]))
            .contains("PASS: pri: OAuth token readable from Keychain")
    );
}

#[test]
fn test_doctor_writes_nothing() {
    let bench = DoctorBench::new();
    let before = file_times(&bench.temp.home);
    assert_eq!(bench.doctor(&[]).status.code(), Some(0));
    for (path, modified) in before {
        assert_eq!(mtime_ns(&path), modified, "{}", path.display());
    }
}
