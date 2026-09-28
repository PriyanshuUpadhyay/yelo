mod common;

use common::{
    TestHome, build_census_home, build_fixture_home, build_sessions_home, build_usage_home,
    fixture_env, mkdir, run_yelo, sealed_path, set_mode, usage_env, write,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

struct Case {
    name: &'static str,
    home: &'static str,
    args: &'static [&'static str],
    writes: bool,
}

fn scrub(text: &str, home: &Path) -> String {
    let real = home.canonicalize().unwrap();
    text.replace(&real.display().to_string(), "$HOME")
        .replace(&home.display().to_string(), "$HOME")
}

fn without_clocks(value: &mut Value) {
    match value {
        Value::Object(map) => {
            map.remove("asOf");
            map.remove("seenAt");
            for child in map.values_mut() {
                without_clocks(child);
            }
        }
        Value::Array(items) => {
            for child in items {
                without_clocks(child);
            }
        }
        _ => {}
    }
}

fn listing(home: &Path) -> Vec<String> {
    fn visit(home: &Path, dir: &Path, lines: &mut Vec<String>) {
        let mut dirs = Vec::new();
        let mut files = Vec::new();
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if fs::symlink_metadata(&path).unwrap().is_dir() {
                dirs.push(path);
            } else {
                files.push(path);
            }
        }
        dirs.sort();
        files.sort();
        let mut all: Vec<_> = dirs.iter().chain(files.iter()).collect();
        all.sort();
        for path in all {
            let meta = fs::symlink_metadata(path).unwrap();
            let mode = meta.permissions().mode() & 0o777;
            let relative = path.strip_prefix(home).unwrap().display();
            let mode = format!("0o{mode:o}");
            if meta.file_type().is_symlink() {
                lines.push(format!(
                    "L {relative} {mode} {}",
                    scrub(&fs::read_link(path).unwrap().display().to_string(), home)
                ));
            } else if meta.is_dir() {
                lines.push(format!("D {relative} {mode}"));
            } else {
                let content = String::from_utf8_lossy(&fs::read(path).unwrap()).into_owned();
                let digest = Sha256::digest(scrub(&content, home).as_bytes());
                lines.push(format!(
                    "F {relative} {mode} {}",
                    &format!("{digest:x}")[..16]
                ));
            }
        }
        for path in dirs {
            visit(home, &path, lines);
        }
    }
    let mut lines = Vec::new();
    visit(home, home, &mut lines);
    lines
}

fn run(case: &Case) -> Value {
    let temp = TestHome::new();
    let home = &temp.home;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    match case.home {
        "sessions" => build_sessions_home(home),
        "usage" => build_usage_home(home, now),
        "census" => build_census_home(home),
        "fixture" => build_fixture_home(home),
        other => panic!("unknown home {other}"),
    }
    let cwd = if case.home == "sessions" {
        home.join("proj")
    } else {
        home.to_path_buf()
    };
    let stubs = temp.root.join("stubs");
    mkdir(&stubs);
    let launchctl = stubs.join("launchctl");
    write(&launchctl, "#!/bin/sh\nexit 113\n");
    set_mode(&launchctl, 0o755);
    let mut env = if case.home == "usage" {
        usage_env(home)
    } else {
        fixture_env(home, common::FALSE_BIN)
    };
    // The CLI contract does not accept caller XDG or YELO settings.
    env.retain(|key, _| !key.starts_with("XDG_") && !key.starts_with("YELO_"));
    env.insert(
        "PATH".into(),
        format!("{}:{}", stubs.display(), sealed_path()),
    );
    let result = run_yelo(case.args, &env, Some(&cwd), "");
    let stdout = scrub(&String::from_utf8_lossy(&result.stdout), home);
    let mut stdout = serde_json::from_str::<Value>(&stdout).unwrap_or(Value::String(stdout));
    without_clocks(&mut stdout);
    let mut outcome = json!({
        "argv": case.args, "home": case.home, "exit": result.status.code().unwrap_or(-1),
        "stdout": stdout, "stderr": scrub(&String::from_utf8_lossy(&result.stderr), home)
    });
    if case.writes {
        outcome["home_after"] = json!(listing(home));
    }
    outcome
}

fn check(case: Case) {
    let golden = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/golden/cli")
        .join(format!("{}.json", case.name));
    let actual = run(&case);
    if std::env::var("YELO_RECORD_GOLDENS").as_deref() == Ok("1") {
        let second = run(&case);
        assert_eq!(
            actual, second,
            "{}: two runs differ, so it cannot be a golden",
            case.name
        );
        fs::write(
            golden,
            format!("{}\n", serde_json::to_string_pretty(&actual).unwrap()),
        )
        .unwrap();
    } else {
        let expected: Value = serde_json::from_slice(&fs::read(golden).unwrap()).unwrap();
        assert_eq!(actual, expected, "{}", case.name);
    }
}

macro_rules! cases {
    ($( $test:ident => ($name:literal, $home:literal, [$($arg:expr),* $(,)?], $writes:literal) ),* $(,)?) => {
        $(
            #[test]
            fn $test() {
                check(Case { name: $name, home: $home, args: &[$($arg),*], writes: $writes });
            }
        )*
    };
}

cases! {
    test_matches_golden_list_claude => ("list-claude", "fixture", ["profile", "list", "--cli", "claude"], false),
    test_matches_golden_list_codex => ("list-codex", "fixture", ["profile", "list", "--cli", "codex"], false),
    test_matches_golden_list_claude_json => ("list-claude-json", "fixture", ["profile", "list", "--cli", "claude", "--json"], false),
    test_matches_golden_list_codex_json => ("list-codex-json", "fixture", ["profile", "list", "--cli", "codex", "--json"], false),
    test_matches_golden_resolve_exact => ("resolve-exact", "fixture", ["profile", "resolve", "--cli", "claude", "--", "pri"], false),
    test_matches_golden_resolve_exact_json => ("resolve-exact-json", "fixture", ["profile", "resolve", "--cli", "claude", "--json", "--", "pri"], false),
    test_matches_golden_resolve_substring => ("resolve-substring", "fixture", ["profile", "resolve", "--cli", "claude", "--", "wor"], false),
    test_matches_golden_resolve_email => ("resolve-email", "fixture", ["profile", "resolve", "--cli", "codex", "--", "alt@example.test"], false),
    test_matches_golden_resolve_ambiguous => ("resolve-ambiguous", "fixture", ["profile", "resolve", "--cli", "claude", "--", "e"], false),
    test_matches_golden_resolve_missing => ("resolve-missing", "fixture", ["profile", "resolve", "--cli", "claude", "--", "zzz"], false),
    test_matches_golden_menu_no_terminal => ("menu-no-terminal", "fixture", ["profile", "menu", "--cli", "claude"], false),
    test_matches_golden_pick_signed_out => ("pick-signed-out", "fixture", ["profile", "pick", "--cli", "claude", "--json"], false),
    test_matches_golden_doctor_json => ("doctor-json", "fixture", ["doctor", "--json"], false),
    test_matches_golden_census_list_claude => ("census-list-claude", "census", ["profile", "list", "--cli", "claude"], false),
    test_matches_golden_census_list_codex => ("census-list-codex", "census", ["profile", "list", "--cli", "codex"], false),
    test_matches_golden_usage_show => ("usage-show", "usage", ["usage", "show"], false),
    test_matches_golden_usage_show_json => ("usage-show-json", "usage", ["usage", "show", "--json"], false),
    test_matches_golden_usage_list_claude => ("usage-list-claude", "usage", ["profile", "list", "--cli", "claude", "--usage"], false),
    test_matches_golden_usage_list_codex_json => ("usage-list-codex-json", "usage", ["profile", "list", "--cli", "codex", "--usage", "--json"], false),
    test_matches_golden_usage_pick_claude => ("usage-pick-claude", "usage", ["profile", "pick", "--cli", "claude", "--json"], false),
    test_matches_golden_usage_pick_codex => ("usage-pick-codex", "usage", ["profile", "pick", "--cli", "codex", "--json"], false),
    test_matches_golden_usage_sessions => ("usage-sessions", "usage", ["profile", "sessions", "--cli", "codex", "--all", "--json"], false),
    test_matches_golden_usage_owner => ("usage-owner", "usage", ["profile", "owner", "--cli", "codex", "--json", common::OWNED_SESSION], false),
    test_matches_golden_usage_doctor => ("usage-doctor", "usage", ["usage", "doctor"], false),
    test_matches_golden_sessions_here => ("sessions-here", "sessions", ["profile", "sessions", "--cli", "codex", "--json"], false),
    test_matches_golden_sessions_all => ("sessions-all", "sessions", ["profile", "sessions", "--cli", "codex", "--all", "--json"], false),
    test_matches_golden_sessions_limit => ("sessions-limit", "sessions", ["profile", "sessions", "--cli", "codex", "--all", "--limit", "1", "--json"], false),
    test_matches_golden_sessions_table => ("sessions-table", "sessions", ["profile", "sessions", "--cli", "codex", "--all"], false),
    test_matches_golden_owner_live => ("owner-live", "sessions", ["profile", "owner", "--cli", "codex", "--json", "019e0000-0000-7000-8000-000000000003"], false),
    test_matches_golden_owner_archived => ("owner-archived", "sessions", ["profile", "owner", "--cli", "codex", "--json", "019e0000-0000-7000-8000-000000000004"], false),
    test_matches_golden_owner_last => ("owner-last", "sessions", ["profile", "owner", "--cli", "codex", "--last", "--json"], false),
    test_matches_golden_owner_unknown => ("owner-unknown", "sessions", ["profile", "owner", "--cli", "codex", "--json", "019e0000-0000-7000-8000-00000000dead"], false),
    test_matches_golden_create_claude => ("create-claude", "fixture", ["profile", "create", "--cli", "claude", "--yes", "newone"], true),
    test_matches_golden_create_codex => ("create-codex", "fixture", ["profile", "create", "--cli", "codex", "--yes", "newone"], true),
    test_matches_golden_sync_claude => ("sync-claude", "fixture", ["profile", "sync", "--cli", "claude"], true),
}
