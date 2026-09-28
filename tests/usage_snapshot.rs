mod common;

use common::{
    TestHome, build_usage_home, codex_auth, fixture_env, mkdir, mtime_ns, rollout, run_yelo,
    sealed_path, set_age, set_mode, statusline_cache, stderr, stdout, usage_env, write, write_json,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}
fn show(home: &Path, flags: &[&str], overrides: &[(&str, &str)]) -> std::process::Output {
    let mut env = usage_env(home);
    for (key, value) in overrides {
        env.insert((*key).into(), (*value).into());
    }
    let mut args = vec!["usage", "show"];
    args.extend_from_slice(flags);
    run_yelo(&args, &env, None, "")
}
fn rows_of(output: &std::process::Output) -> Vec<Value> {
    assert_eq!(output.status.code(), Some(0), "{}", stderr(output));
    serde_json::from_slice(&output.stdout).unwrap()
}
fn show_rows(home: &Path, overrides: &[(&str, &str)]) -> Vec<Value> {
    rows_of(&show(home, &["--json"], overrides))
}
fn without_clocks(rows: &mut [Value]) {
    for row in rows {
        row.as_object_mut().unwrap().remove("asOf");
        row.as_object_mut().unwrap().remove("seenAt");
    }
}
fn claude_home(home: &Path) {
    write(
        &home.join(".claude/.profiles/pri/email"),
        "a@example.test\n",
    );
}
fn codex_home(home: &Path) -> PathBuf {
    let dir = home.join(".codex");
    mkdir(&dir.join("sessions"));
    write_json(
        &dir.join("auth.json"),
        &codex_auth("c@example.test", "pro", "acc-1"),
    );
    write(&dir.join("profile-label"), "\n");
    dir
}
fn profile(home: &Path) -> PathBuf {
    home.join(".claude/.profiles/pri")
}
fn read_times(root: &Path) -> BTreeMap<PathBuf, u128> {
    fn visit(dir: &Path, times: &mut BTreeMap<PathBuf, u128>) {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                visit(&path, times);
            } else {
                times.insert(path.clone(), mtime_ns(&path));
            }
        }
    }
    let mut times = BTreeMap::new();
    visit(root, &mut times);
    times
}

#[test]
fn test_show_matches_golden() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, now());
    let mut expected: Vec<Value> = serde_json::from_slice(
        &fs::read(Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/usage-show.json"))
            .unwrap(),
    )
    .unwrap();
    let mut actual = show_rows(&temp.home, &[]);
    without_clocks(&mut actual);
    without_clocks(&mut expected);
    assert_eq!(actual, expected);
}

#[test]
fn test_show_table_matches_golden() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, now());
    let result = show(&temp.home, &[], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        stdout(&result),
        fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/golden/usage-show.txt")
        )
        .unwrap()
    );
}

#[test]
fn test_window_scopes() {
    let temp = TestHome::new();
    let now = now();
    claude_home(&temp.home);
    write_json(
        &profile(&temp.home).join(".usage-api-cache-fable.json"),
        &json!({
            "five_hour": {"used_percentage": 77, "resets_at": now + common::FIVE_HOUR_RESET},
            "seven_day": {"used_percentage": 9, "resets_at": now + common::SEVEN_DAY_RESET},
            "ts": now, "fetched_at": now, "source": "api"
        }),
    );
    let rows = show_rows(&temp.home, &[]);
    let row = |window: &str| rows.iter().find(|row| row["window"] == window).unwrap();
    assert_eq!(row("5h")["pct"], 77);
    assert_eq!(row("fb")["pct"], 9);
    assert_eq!(row("7d")["state"], "missing");
    assert!(row("7d").get("pct").is_none());
}

#[test]
fn test_row_states() {
    let temp = TestHome::new();
    let now = now();
    claude_home(&temp.home);
    let cache = profile(&temp.home).join(".usage-cache.json");
    write_json(&cache, &statusline_cache(now - 2400, 43, 11));
    let rows = show_rows(&temp.home, &[]);
    let five = rows.iter().find(|row| row["window"] == "5h").unwrap();
    assert_eq!(five["state"], "stale");
    assert_eq!(five["pct"], 43);
    write_json(
        &cache,
        &json!({"five_hour": {"used_percentage": 43, "resets_at": now - 5},
        "ts": now, "activity_at": now, "source": "statusline"}),
    );
    let rows = show_rows(&temp.home, &[]);
    assert_eq!(
        rows.iter().find(|row| row["window"] == "5h").unwrap()["state"],
        "stale"
    );
    assert_eq!(
        rows.iter().find(|row| row["window"] == "7d").unwrap()["state"],
        "missing"
    );
    fs::remove_file(cache).unwrap();
    let offline = show_rows(&temp.home, &[]);
    assert_eq!(
        offline,
        vec![
            json!({"label": "cl·a@example.test", "provider": "claude", "state": "offline", "reason": "no data", "canFetch": true})
        ]
    );
    let logged_out = rows_of(&run_yelo(
        &["usage", "show", "--json"],
        &fixture_env(&temp.home, common::FALSE_BIN),
        None,
        "",
    ));
    assert_eq!(logged_out, offline);
}

#[test]
fn test_stale_after_is_the_gate() {
    let temp = TestHome::new();
    claude_home(&temp.home);
    write_json(
        &profile(&temp.home).join(".usage-cache.json"),
        &statusline_cache(now() - 600, 43, 11),
    );
    let fresh = show_rows(&temp.home, &[]);
    assert_eq!(
        fresh.iter().find(|row| row["window"] == "5h").unwrap()["state"],
        "ok"
    );
    let tightened = show_rows(&temp.home, &[("USAGE_HUD_STALE_AFTER", "60")]);
    assert_eq!(
        tightened.iter().find(|row| row["window"] == "5h").unwrap()["state"],
        "stale"
    );
}

#[test]
fn test_codex_rows() {
    let temp = TestHome::new();
    let now = now();
    let dir = codex_home(&temp.home);
    rollout(&dir, now, 18);
    set_age(&dir.join("sessions/2026/09/01/rollout-2026-09-01T09-00-00-019e08eb-508e-7e73-8bc3-1e9c69b5dfd3.jsonl"), 4000);
    let cache = dir.join(".usage-hud-api-cache.json");
    write_json(
        &cache,
        &json!({"rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080,
        "resets_at": now + common::CODEX_RESET}, "secondary": null}, "fetched_at": now - 60, "source": "api"}),
    );
    let rows = show_rows(&temp.home, &[]);
    assert_eq!(
        rows.iter()
            .map(|row| row["window"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["7d"]
    );
    assert_eq!(rows[0]["pct"], 30);
    assert_eq!(rows[0]["source"], "api");
    write_json(
        &cache,
        &json!({"rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080,
        "resets_at": now + common::CODEX_RESET}, "secondary": {"used_percent": 8,
        "window_minutes": 300, "resets_at": now + common::FIVE_HOUR_RESET}},
        "fetched_at": now - 60, "source": "api"}),
    );
    let rows = show_rows(&temp.home, &[]);
    assert_eq!(
        rows.iter()
            .map(|row| (
                row["window"].as_str().unwrap(),
                row["pct"].as_i64().unwrap()
            ))
            .collect::<Vec<_>>(),
        [("7d", 30), ("5h", 8)]
    );
}

#[test]
fn test_codex_reset_credits() {
    let temp = TestHome::new();
    let now = now();
    let dir = codex_home(&temp.home);
    let cache = dir.join(".usage-hud-api-cache.json");
    let mut doc = json!({"rate_limits": {"primary": {"used_percent": 30, "window_minutes": 10080,
        "resets_at": now + common::CODEX_RESET}, "secondary": {"used_percent": 8,
        "window_minutes": 300, "resets_at": now + common::FIVE_HOUR_RESET}},
        "fetched_at": now - 60, "source": "api"});
    doc["reset_credits"] =
        json!({"available": 4, "expires_at": [null, now + 6 * 86400, now - 5, now + 3600]});
    write_json(&cache, &doc);
    let rows = show_rows(&temp.home, &[]);
    assert_eq!(rows.len(), 2);
    for row in &rows {
        assert_eq!(row["resetCredits"], 3);
        assert_eq!(
            row["resetCreditsExpireAt"],
            json!([now + 3600, now + 6 * 86400, null])
        );
    }
    doc["reset_credits"] = json!({"available": 2});
    write_json(&cache, &doc);
    let rows = show_rows(&temp.home, &[]);
    assert_eq!(rows[0]["resetCredits"], 2);
    assert!(rows[0].get("resetCreditsExpireAt").is_none());
    fs::remove_file(&cache).unwrap();
    assert!(show_rows(&temp.home, &[])[0].get("resetCredits").is_none());
}

#[test]
fn test_only_the_five_newest_rollouts_are_read() {
    let temp = TestHome::new();
    let now = now();
    let dir = codex_home(&temp.home);
    let day = dir.join("sessions/2026/09/01");
    mkdir(&day);
    for index in 0..6 {
        let path = day.join(format!(
            "rollout-2026-09-0{}T09-00-00-{index}.jsonl",
            index + 1
        ));
        let record = if index == 0 {
            json!({"payload": {"rate_limits": {"primary": {
            "used_percent": 22, "window_minutes": 10080, "resets_at": now + common::CODEX_RESET},
            "secondary": null}}})
        } else {
            json!({"payload": {"type": "message"}})
        };
        write_json(&path, &record);
        set_age(&path, (1000 * (6 - index)) as u64);
    }
    assert_eq!(show_rows(&temp.home, &[])[0]["state"], "offline");
    set_age(&day.join("rollout-2026-09-01T09-00-00-0.jsonl"), 0);
    assert_eq!(show_rows(&temp.home, &[])[0]["pct"], 22);
}

#[test]
fn test_codex_offline_reasons() {
    let temp = TestHome::new();
    let dir = codex_home(&temp.home);
    fs::remove_dir(dir.join("sessions")).unwrap();
    assert_eq!(show_rows(&temp.home, &[])[0]["reason"], "not set up");
    mkdir(&dir.join("sessions"));
    assert_eq!(show_rows(&temp.home, &[])[0]["reason"], "no data");
}

#[test]
fn test_row_keys_and_table() {
    let temp = TestHome::new();
    let now = now();
    build_usage_home(&temp.home, now);
    let rows = show_rows(&temp.home, &[]);
    let allowed: BTreeSet<_> = [
        "label",
        "provider",
        "window",
        "state",
        "pct",
        "reset",
        "asOf",
        "seenAt",
        "active",
        "source",
        "canFetch",
        "primeSignedIn",
        "reason",
        "resetCredits",
        "resetCreditsExpireAt",
    ]
    .into();
    for row in &rows {
        assert!(
            row.as_object()
                .unwrap()
                .keys()
                .all(|key| allowed.contains(key.as_str()))
        );
        if row["state"] != "ok" && row["state"] != "stale" {
            assert!(row.get("pct").is_none());
        } else {
            assert!(row["pct"].as_i64().is_some());
            assert!(row["seenAt"].as_i64().is_some());
            assert!(row["asOf"].as_i64().is_some());
        }
        assert_eq!(row["canFetch"], row["provider"] == "claude");
        assert!(row.get("primeSignedIn").is_none());
    }
    assert_eq!(
        rows.iter()
            .filter(|row| row["provider"] == "claude")
            .map(|row| row.get("active").and_then(Value::as_bool))
            .collect::<Vec<_>>(),
        [Some(true), Some(true), Some(true), None]
    );
    assert!(
        rows.iter()
            .filter(|row| row["provider"] == "codex")
            .all(|row| row.get("active").is_none())
    );
    let output = show(&temp.home, &[], &[]);
    let table = stdout(&output);
    let lines: Vec<_> = table.lines().collect();
    assert_eq!(
        lines[0].split_whitespace().collect::<Vec<_>>(),
        [
            "LABEL", "WINDOW", "PCT", "RESET", "CREDITS", "STATE", "SOURCE"
        ]
    );
    assert_eq!(lines.len(), rows.len() + 1);
    let bare = temp.root.join("bare/home");
    claude_home(&bare);
    write_json(
        &profile(&bare).join(".usage-api-cache.json"),
        &json!({
        "five_hour": {"used_percentage": 5, "resets_at": now + common::FIVE_HOUR_RESET},
        "fetched_at": now, "source": "api"}),
    );
    assert!(
        show_rows(&bare, &[])
            .iter()
            .all(|row| row.get("active").is_none())
    );
}

#[test]
fn test_unreadable_cache_and_error_shape() {
    let temp = TestHome::new();
    let now = now();
    claude_home(&temp.home);
    let dir = profile(&temp.home);
    write(
        &dir.join(".usage-api-cache.json"),
        "{\"five_hour\": {\"used_per",
    );
    write_json(
        &dir.join(".usage-cache.json"),
        &json!({"five_hour": {"used_percentage": 43,
        "resets_at": now + common::FIVE_HOUR_RESET}, "ts": now, "activity_at": now, "source": "statusline"}),
    );
    let rows = show_rows(&temp.home, &[]);
    let five = rows.iter().find(|row| row["window"] == "5h").unwrap();
    let seven = rows.iter().find(|row| row["window"] == "7d").unwrap();
    assert_eq!(five["pct"], 43);
    assert_eq!(seven["state"], "missing");
    assert_eq!(
        seven
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect::<BTreeSet<_>>(),
        ["label", "provider", "window", "state", "active", "canFetch"].into()
    );
    fs::remove_file(dir.join(".usage-cache.json")).unwrap();
    assert_eq!(
        show_rows(&temp.home, &[]),
        vec![json!({"label": "cl·a@example.test", "provider": "claude",
        "state": "offline", "reason": "no data", "canFetch": true})]
    );
    let root = temp.home.join(".claude/.profiles");
    set_mode(&root, 0o000);
    let result = show(&temp.home, &["--json"], &[]);
    set_mode(&root, 0o755);
    assert_eq!(result.status.code(), Some(1));
    assert!(result.stdout.is_empty());
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: usage show: cannot read the claude profile root ({})\n",
            root.display()
        )
    );
}

#[test]
fn test_show_never_reads_token_bytes() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, now());
    let stub = temp.root.join("stubs/security");
    write(
        &stub,
        "#!/bin/sh\nprintf '%s\\t' \"$@\" >> \"$FAKE_SECURITY_LOG\"\nprintf '\\n' >> \"$FAKE_SECURITY_LOG\"\nexit 0\n",
    );
    set_mode(&stub, 0o755);
    let log = temp.root.join("security.log");
    let mut env = usage_env(&temp.home);
    env.insert(
        "AGENT_PROFILES_SECURITY_BIN".into(),
        stub.display().to_string(),
    );
    env.insert("FAKE_SECURITY_LOG".into(), log.display().to_string());
    env.insert(
        "PATH".into(),
        format!("{}:{}", stub.parent().unwrap().display(), sealed_path()),
    );
    let rows = rows_of(&run_yelo(&["usage", "show", "--json"], &env, None, ""));
    assert!(!rows.is_empty());
    assert!(!log.exists());
}

#[test]
fn test_show_writes_nothing() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, now());
    let before = read_times(&temp.home);
    assert_eq!(show(&temp.home, &["--json"], &[]).status.code(), Some(0));
    for (path, modified) in before {
        assert_eq!(mtime_ns(&path), modified, "{}", path.display());
    }
}
