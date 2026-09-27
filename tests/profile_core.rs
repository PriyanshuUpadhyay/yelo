mod common;

use common::{TestHome, fixture_env, mkdir, run_yelo, set_mode, stderr, write, write_json};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}

fn json_output(output: &std::process::Output) -> Value {
    assert_eq!(output.status.code(), Some(0), "{}", stderr(output));
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn test_keychain_service_shared() {
    let temp = TestHome::new();
    let home = &temp.home;
    for name in ["pri", "work"] {
        mkdir(&home.join(format!(".claude/.profiles/{name}")));
    }
    let identity = home.join(".claude-pri");
    let service = format!(
        "Claude Code-credentials-{}",
        &format!(
            "{:x}",
            Sha256::digest(identity.display().to_string().as_bytes())
        )[..8]
    );
    let security = temp.root.join("security");
    let log = temp.root.join("security.log");
    write(
        &security,
        "#!/bin/sh\nprintf '%s\\t' \"$@\" >> \"$FAKE_SECURITY_LOG\"\nprintf '\\n' >> \"$FAKE_SECURITY_LOG\"\nprev=\nfor arg in \"$@\"; do\n  if [ \"$prev\" = -s ] && [ \"$arg\" = \"$FAKE_SERVICE\" ]; then exit 0; fi\n  prev=$arg\ndone\nexit 1\n",
    );
    set_mode(&security, 0o755);
    let mut env = fixture_env(home, &security.display().to_string());
    env.insert("FAKE_SECURITY_LOG".into(), log.display().to_string());
    env.insert("FAKE_SERVICE".into(), service.clone());
    let result = run_yelo(
        &["profile", "list", "--cli", "claude", "--json"],
        &env,
        None,
        "",
    );
    let rows = json_output(&result);
    let pri = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["name"] == "pri")
        .unwrap();
    let work = rows
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["name"] == "work")
        .unwrap();
    assert_eq!(pri["signed_in"], true);
    assert_eq!(work["signed_in"], false);
    let calls = fs::read_to_string(log).unwrap();
    assert!(calls.lines().any(|line| {
        line.split('\t')
            .collect::<Vec<_>>()
            .windows(2)
            .any(|pair| pair == ["-s", service.as_str()])
    }));
    assert!(calls.lines().all(|line| {
        line.split('\t')
            .collect::<Vec<_>>()
            .windows(2)
            .filter(|pair| pair[0] == "-a")
            .all(|pair| pair[1].is_empty())
    }));
}

type PickCache<'a> = (&'a str, &'a [(&'a str, i64, i64)]);

fn run_pick(caches: &[PickCache<'_>], names: &[&str]) -> Value {
    let temp = TestHome::new();
    let root = temp.root.join("profiles");
    for name in names {
        mkdir(&root.join(name));
    }
    let now = now();
    for (name, windows) in caches {
        let mut doc = json!({"ts": now, "fetched_at": now, "source": "api"});
        for (key, pct, offset) in *windows {
            doc[*key] = json!({"used_percentage": pct, "resets_at": now + offset});
        }
        write_json(&root.join(name).join(".usage-api-cache.json"), &doc);
    }
    let mut env = fixture_env(&temp.root, common::TRUE_BIN);
    env.insert(
        "AGENT_PROFILES_CLAUDE_ROOT".into(),
        root.display().to_string(),
    );
    json_output(&run_yelo(
        &["profile", "pick", "--cli", "claude", "--json"],
        &env,
        None,
        "",
    ))
}

#[test]
fn test_expiring_window_beats_fuller_account() {
    let picked = run_pick(
        &[
            ("a", &[("five_hour", 50, 1830)]),
            ("b", &[("five_hour", 0, 17430)]),
        ],
        &["a", "b"],
    );
    assert_eq!(picked["name"], "a");
}

#[test]
fn test_exhausted_window_loses_to_usable_account() {
    let picked = run_pick(
        &[
            ("a", &[("five_hour", 20, 630), ("seven_day", 100, 174630)]),
            ("b", &[("five_hour", 40, 14430), ("seven_day", 40, 432630)]),
        ],
        &["a", "b"],
    );
    assert_eq!(picked["name"], "b");
}

struct Sessions {
    temp: TestHome,
    work: PathBuf,
}

impl Sessions {
    fn new() -> Self {
        let temp = TestHome::new();
        mkdir(&temp.root.join(".codex"));
        write(&temp.root.join(".codex/profile-label"), "base\n");
        mkdir(&temp.root.join(".codex-other"));
        let work = temp.root.join("work");
        mkdir(&work);
        Self { temp, work }
    }

    fn rollout(&self, home: &str, started: &str, id: &str, cwd: &Path, meta_type: &str) {
        let day = started[..10].replace('-', "/");
        let dir = self.temp.root.join(home).join("sessions").join(day);
        mkdir(&dir);
        write_json(
            &dir.join(format!("rollout-{started}-{id}.jsonl")),
            &json!({
                "timestamp": started, "type": meta_type,
                "payload": {"id": id, "cwd": cwd.display().to_string()}
            }),
        );
    }

    fn run(&self, verb: &str, flags: &[&str], cwd: &Path) -> std::process::Output {
        let mut env = fixture_env(&self.temp.root, common::FALSE_BIN);
        env.insert(
            "AGENT_PROFILES_CODEX_GLOB_ROOT".into(),
            self.temp.root.display().to_string(),
        );
        let mut args = vec!["profile", verb, "--cli", "codex", "--json"];
        args.extend_from_slice(flags);
        run_yelo(&args, &env, Some(cwd), "")
    }

    fn sessions(&self, flags: &[&str]) -> Value {
        json_output(&self.run("sessions", flags, &self.work))
    }
    fn owner(&self, flags: &[&str], cwd: &Path) -> Value {
        json_output(&self.run("owner", flags, cwd))
    }
}

const A: &str = "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3";
const B: &str = "019e08eb-4c3f-7610-b2f9-c7b4a062e1ee";
const C: &str = "019e08eb-4d19-7181-b8c8-ed0f8584a1ea";

#[test]
fn test_newest_first_across_accounts() {
    let s = Sessions::new();
    s.rollout(".codex", "2026-08-01T09-00-00", A, &s.work, "session_meta");
    s.rollout(
        ".codex-other",
        "2026-08-02T09-00-00",
        B,
        &s.work,
        "session_meta",
    );
    let rows = s.sessions(&[]);
    assert_eq!(
        rows.as_array()
            .unwrap()
            .iter()
            .map(|row| row["profile"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["other", "base"]
    );
}

#[test]
fn test_cwd_narrows_and_all_widens() {
    let s = Sessions::new();
    s.rollout(".codex", "2026-08-01T09-00-00", A, &s.work, "session_meta");
    s.rollout(
        ".codex",
        "2026-08-03T09-00-00",
        C,
        &s.temp.root.join("elsewhere"),
        "session_meta",
    );
    assert_eq!(s.sessions(&[]).as_array().unwrap().len(), 1);
    assert_eq!(s.sessions(&["--all"]).as_array().unwrap().len(), 2);
}

#[test]
fn test_limit_caps_the_list() {
    let s = Sessions::new();
    for (day, id) in [("01", A), ("02", B), ("03", C)] {
        s.rollout(
            ".codex",
            &format!("2026-08-{day}T09-00-00"),
            id,
            &s.work,
            "session_meta",
        );
    }
    let rows = s.sessions(&["--limit", "2", "--all"]);
    assert_eq!(rows.as_array().unwrap().len(), 2);
    assert_eq!(rows[0]["started"], "2026-08-03T09-00-00");
}

#[test]
fn test_owner_is_the_home_holding_the_session() {
    let s = Sessions::new();
    s.rollout(".codex", "2026-08-01T09-00-00", A, &s.work, "session_meta");
    s.rollout(
        ".codex-other",
        "2026-08-02T09-00-00",
        B,
        &s.temp.root.join("elsewhere"),
        "session_meta",
    );
    let archived = s.temp.root.join(".codex-other/archived_sessions");
    mkdir(&archived);
    write(
        &archived.join(format!("rollout-2026-07-01T09-00-00-{C}.jsonl")),
        "",
    );
    let owner = s.owner(&["019E08EB-4C3F-7610-B2F9-C7B4A062E1EE"], &s.work);
    assert_eq!(owner["name"], "other");
    assert_eq!(
        owner["dir"],
        s.temp.root.join(".codex-other").display().to_string()
    );
    assert_eq!(s.owner(&[C], &s.work)["name"], "other");
    assert_eq!(s.owner(&["--last"], &s.work)["name"], "base");
    assert_eq!(s.owner(&["--last", "--all"], &s.work)["name"], "other");
    let absent = s.run("owner", &["019e08eb-0000-7000-8000-000000000000"], &s.work);
    assert_eq!(absent.status.code(), Some(1));
    assert!(stderr(&absent).contains("is not in any account"));
    let empty = s.run("owner", &["--last"], &s.temp.root);
    assert_eq!(empty.status.code(), Some(1));
    assert!(stderr(&empty).contains("no sessions found"));
}

#[test]
fn test_non_meta_first_line_is_skipped() {
    let s = Sessions::new();
    s.rollout(".codex", "2026-08-01T09-00-00", A, &s.work, "response_item");
    assert_eq!(s.sessions(&["--all"]), json!([]));
}
