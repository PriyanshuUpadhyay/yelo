mod common;

use common::{
    Fixture, TestHome, build_census_home, build_usage_home, fixture_env, run_yelo, stderr, stdout,
    usage_env,
};
use serde_json::Value;
use std::fs;
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

fn run(home: &Path, args: &[&str]) -> std::process::Output {
    run_yelo(args, &fixture_env(home, common::FALSE_BIN), None, "")
}

#[test]
fn test_list_matches_golden() {
    let fixture = Fixture::new();
    for cli in ["claude", "codex"] {
        let result = fixture.run(&["profile", "list", "--cli", cli]);
        assert!(result.status.success(), "{cli}: {}", stderr(&result));
        let golden =
            Path::new(env!("CARGO_MANIFEST_DIR")).join(format!("tests/golden/list-{cli}.txt"));
        assert_eq!(
            stdout(&result),
            fs::read_to_string(golden).unwrap(),
            "{cli}"
        );
    }
}

fn valid_name(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(ch) if ch.is_ascii_alphanumeric())
        && chars.all(|ch| ch.is_ascii_alphanumeric() || "_.-".contains(ch))
}

fn census(home: &Path, cli: &str) -> usize {
    let (root, prefix, base) = match cli {
        "claude" => (home.join(".claude/.profiles"), "", None),
        "codex" => (home.to_path_buf(), ".codex-", Some(home.join(".codex"))),
        _ => unreachable!(),
    };
    let count = fs::read_dir(root)
        .unwrap()
        .filter(|entry| {
            let path = entry.as_ref().unwrap().path();
            let name = entry
                .as_ref()
                .unwrap()
                .file_name()
                .to_string_lossy()
                .into_owned();
            path.is_dir() && name.strip_prefix(prefix).is_some_and(valid_name)
        })
        .count();
    count + usize::from(base.is_some_and(|path| path.is_dir()))
}

#[test]
fn test_profile_census_matches_layout() {
    let temp = TestHome::new();
    build_census_home(&temp.home);
    for cli in ["claude", "codex"] {
        let result = run(&temp.home, &["profile", "list", "--cli", cli, "--json"]);
        assert!(result.status.success(), "{cli}: {}", stderr(&result));
        let rows: Value = serde_json::from_slice(&result.stdout).unwrap();
        let expected = census(&temp.home, cli);
        assert!(expected > 1, "{cli}");
        assert_eq!(rows.as_array().unwrap().len(), expected, "{cli}");
        for row in rows.as_array().unwrap() {
            assert!(
                ![
                    "bad name",
                    "-lead",
                    ".hidden",
                    ".session-map",
                    "notes.txt",
                    ".aliases",
                    "",
                    "file"
                ]
                .contains(&row["name"].as_str().unwrap()),
                "{cli}: {row}"
            );
        }
    }
}

#[test]
fn test_resolve_exit_codes() {
    let fixture = Fixture::new();
    let exact = fixture.run(&["profile", "resolve", "--cli", "claude", "--", "pri"]);
    assert_eq!(exact.status.code(), Some(0));
    assert_eq!(
        stdout(&exact),
        format!(
            "pri\t{}\n",
            fixture.temp.home.join(".claude/.profiles/pri").display()
        )
    );
    let substring = fixture.run(&["profile", "resolve", "--cli", "claude", "--", "wor"]);
    assert_eq!(substring.status.code(), Some(0));
    assert_eq!(stdout(&substring).split('\t').next(), Some("work"));
    let email = fixture.run(&[
        "profile",
        "resolve",
        "--cli",
        "codex",
        "--",
        "alt@example.test",
    ]);
    assert_eq!(email.status.code(), Some(0));
    assert_eq!(stdout(&email).split('\t').next(), Some("alt"));
    let ambiguous = fixture.run(&["profile", "resolve", "--cli", "claude", "--", "e"]);
    assert_eq!(ambiguous.status.code(), Some(2));
    assert!(stderr(&ambiguous).starts_with("claude: 'e' matches several profiles\n"));
    assert!(stderr(&ambiguous).contains("pri") && stderr(&ambiguous).contains("work"));
    let missing = fixture.run(&["profile", "resolve", "--cli", "claude", "--", "zzz"]);
    assert_eq!(missing.status.code(), Some(1));
    assert_eq!(stderr(&missing), "claude: no profile matches 'zzz'\n");
    assert!(missing.stdout.is_empty());
}

fn choices(text: &str) -> Vec<&str> {
    text.split_once('{')
        .unwrap()
        .1
        .split_once('}')
        .unwrap()
        .0
        .split(',')
        .collect()
}

#[test]
fn test_help_lists_every_group() {
    let result = Fixture::new().run(&["--help"]);
    assert!(result.status.success());
    for group in ["profile", "usage", "setup", "doctor", "hud"] {
        assert!(stdout(&result).contains(group), "{group}");
    }
}

#[test]
fn test_supported_groups_registered() {
    let fixture = Fixture::new();
    let root = fixture.run(&["--help"]);
    assert_eq!(root.status.code(), Some(0));
    assert_eq!(
        choices(&stdout(&root)),
        ["profile", "usage", "setup", "doctor", "hud"]
    );
    for (group, expected) in [
        ("usage", &["show", "fetch", "doctor"][..]),
        ("hud", &["install", "assemble", "start", "stop"][..]),
    ] {
        let result = fixture.run(&[group, "--help"]);
        assert_eq!(
            result.status.code(),
            Some(0),
            "{group}: {}",
            stderr(&result)
        );
        let output = stdout(&result);
        let mut got = choices(&output);
        got.sort();
        let mut expected = expected.to_vec();
        expected.sort();
        assert_eq!(got, expected, "{group}");
    }
}

#[test]
fn test_help_needs_no_host() {
    let temp = TestHome::new();
    let mut env = common::Env::new();
    env.insert("HOME".into(), temp.root.display().to_string());
    env.insert("PATH".into(), temp.root.display().to_string());
    for args in [
        &["--help"][..],
        &["profile", "--help"],
        &["usage", "--help"],
        &["setup", "--help"],
        &["doctor", "--help"],
        &["hud", "--help"],
    ] {
        let result = run_yelo(args, &env, None, "");
        assert_eq!(
            result.status.code(),
            Some(0),
            "{args:?}: {}",
            stderr(&result)
        );
        assert!(!result.stdout.is_empty(), "{args:?}");
    }
}

#[test]
fn test_version() {
    let result = Fixture::new().run(&["--version"]);
    assert_eq!(result.status.code(), Some(0));
    assert_eq!(
        stdout(&result).trim(),
        concat!("yelo ", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn test_a_subcommand_without_the_hook_gets_argparses_own_error() {
    let temp = TestHome::new();
    let mut env = common::Env::new();
    env.insert("HOME".into(), temp.root.display().to_string());
    env.insert("PATH".into(), temp.root.display().to_string());
    let result = run_yelo(&["doctor", "--nope"], &env, None, "");
    assert_eq!(result.status.code(), Some(2));
    assert!(stderr(&result).contains("unrecognized arguments: --nope"));
}

#[test]
fn test_list_usage_in_process() {
    let temp = TestHome::new();
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64;
    build_usage_home(&temp.home, now);
    let mut env = usage_env(&temp.home);
    env.insert("PATH".into(), "/nonexistent".into());
    let listing = run_yelo(
        &["profile", "list", "--cli", "claude", "--usage"],
        &env,
        None,
        "",
    );
    assert_eq!(listing.status.code(), Some(0), "{}", stderr(&listing));
    assert!(stdout(&listing).contains("5h 57% left"));
    assert!(stdout(&listing).contains("no data"));
    let picked = run_yelo(
        &["profile", "pick", "--cli", "claude", "--json"],
        &env,
        None,
        "",
    );
    assert_eq!(picked.status.code(), Some(0), "{}", stderr(&picked));
    let row: Value = serde_json::from_slice(&picked.stdout).unwrap();
    assert_eq!(row["name"], "pri");
}

#[test]
fn test_version_is_the_package_version() {
    let result = Fixture::new().run(&["--version"]);
    assert_eq!(result.status.code(), Some(0));
    let manifest =
        fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml")).unwrap();
    let version = manifest
        .lines()
        .find_map(|line| {
            line.strip_prefix("version = \"")
                .and_then(|tail| tail.strip_suffix('"'))
        })
        .unwrap();
    assert_eq!(stdout(&result).trim(), format!("yelo {version}"));
}
