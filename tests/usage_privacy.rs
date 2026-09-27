mod common;

use common::{
    Bench, TestHome, build_usage_home, fixture_env, mkdir, run_yelo, set_mode, stderr, write,
};
use serde_json::Value;
use std::fs;

#[test]
fn test_snapshot_reads_local_identity_without_subprocesses() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, 1_900_000_000);
    let mut env = fixture_env(&temp.home, common::FALSE_BIN);
    let log = temp.root.join("child.log");
    let stubs = temp.root.join("stubs");
    mkdir(&stubs);
    for name in ["security", "claude", "codex"] {
        let script = stubs.join(name);
        write(
            &script,
            &format!(
                "#!/bin/sh\nprintf '%s\\n' {name} >> '{}'\nexit 1\n",
                log.display()
            ),
        );
        set_mode(&script, 0o755);
    }
    env.insert(
        "AGENT_PROFILES_SECURITY_BIN".into(),
        stubs.join("security").display().to_string(),
    );
    env.insert(
        "PATH".into(),
        format!("{}:{}", stubs.display(), common::sealed_path()),
    );
    let result = run_yelo(&["usage", "show", "--json"], &env, None, "");
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let rows: Value = serde_json::from_slice(&result.stdout).unwrap();
    let rows = rows.as_array().unwrap();
    let providers: std::collections::BTreeSet<_> = rows
        .iter()
        .map(|row| row["provider"].as_str().unwrap())
        .collect();
    assert_eq!(providers, ["claude", "codex"].into());
    let labels: std::collections::BTreeSet<_> = rows
        .iter()
        .map(|row| row["label"].as_str().unwrap())
        .collect();
    assert_eq!(
        labels,
        [
            "cl·pri@example.test",
            "cl·work@example.test",
            "cx·base@example.test",
            "cx·alt@example.test"
        ]
        .into()
    );
    assert!(rows.iter().any(|row| row["pct"] == 43));
    assert!(rows.iter().all(|row| row.get("primeSignedIn").is_none()));
    assert!(!log.exists());
}

#[test]
fn test_removed_providers_cannot_be_installed() {
    let bench = Bench::new();
    for group in ["herdr", "prime"] {
        assert_eq!(
            bench.run(&[group, "setup"], &[]).status.code(),
            Some(2),
            "{group}"
        );
    }
    assert_eq!(
        bench
            .run(
                &["profile", "create", "--cli", "prime", "example", "--yes"],
                &[]
            )
            .status
            .code(),
        Some(2)
    );
    assert!(!bench.temp.home.join(".prime").exists());
}

#[test]
fn test_setup_leaves_old_prime_launchers_alone() {
    let bench = Bench::new();
    mkdir(&bench.launchers);
    let launcher = bench.launchers.join("prime-agent-work");
    let content = "#!/bin/sh\n# written by yelo setup launchers: account work\nexit 0\n";
    write(&launcher, content);
    assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
    assert_eq!(fs::read_to_string(launcher).unwrap(), content);
}

#[test]
fn test_snapshot_names_only_yelo_launchers() {
    let temp = TestHome::new();
    build_usage_home(&temp.home, 1_900_000_000);
    let env = fixture_env(&temp.home, common::FALSE_BIN);
    assert_eq!(
        run_yelo(&["setup", "launchers"], &env, None, "")
            .status
            .code(),
        Some(0)
    );
    let launchers = temp.home.join(".local/bin");
    let mut written: Vec<_> = fs::read_dir(&launchers)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .collect();
    written.sort();
    write(&written[0], "#!/bin/sh\nexit 0\n");
    let result = run_yelo(&["usage", "show", "--json"], &env, None, "");
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let rows: Value = serde_json::from_slice(&result.stdout).unwrap();
    let tagged: std::collections::BTreeSet<_> = rows
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|row| row["launcher"].as_str())
        .collect();
    let expected: std::collections::BTreeSet<_> = written[1..]
        .iter()
        .map(|path| path.to_str().unwrap())
        .collect();
    assert_eq!(tagged, expected);
}
