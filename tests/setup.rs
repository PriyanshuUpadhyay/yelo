mod common;

use common::{
    Bench, build_fixture_home, env_for, mkdir, run_yelo, seeded_settings, set_mode, stderr, stdout,
    tree_digest, write, write_plist,
};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::Path;
use std::process::Command;

const HUD_LABEL: &str = "io.github.priyanshuupadhyay.yelo-hud";
fn home(bench: &Bench) -> &Path {
    &bench.temp.home
}
fn seed_accounts(bench: &Bench) {
    build_fixture_home(home(bench));
}
fn accounts() -> BTreeSet<&'static str> {
    ["claude-pri", "claude-work", "codex-base", "codex-alt"].into()
}
fn install_hud_targets(home: &Path) {
    write_plist(
        &home.join(format!("Library/LaunchAgents/{HUD_LABEL}.plist")),
        &json!({"Label": HUD_LABEL}),
    );
    write(
        &home.join("Applications/UsageHUD.app/Contents/MacOS/UsageHUD"),
        "binary\n",
    );
}
fn row_detail(result: &std::process::Output, name: &str) -> String {
    stdout(result)
        .lines()
        .find_map(|line| line.strip_prefix(&format!("{name}\t")))
        .unwrap()
        .split('\t')
        .nth(1)
        .unwrap()
        .to_string()
}
fn names(path: &Path) -> BTreeSet<String> {
    fs::read_dir(path)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect()
}

#[test]
fn test_setup_idempotent() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let first = bench.run(&["setup"], &[]);
    assert_eq!(first.status.code(), Some(0), "{}", stderr(&first));
    assert_eq!(
        Bench::rows(&first),
        BTreeMap::from([
            ("launchers".into(), "changed".into()),
            ("profiles".into(), "unchanged".into()),
            ("profile-mirrors".into(), "changed".into())
        ])
    );
    let written = names(&bench.launchers);
    assert_eq!(
        written.iter().map(String::as_str).collect::<BTreeSet<_>>(),
        accounts()
    );
    for name in written {
        let path = bench.launchers.join(&name);
        assert_eq!(
            fs::read_to_string(&path).unwrap().lines().next(),
            Some("#!/bin/sh"),
            "{name}"
        );
        assert_ne!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o111,
            0,
            "{name}"
        );
    }
    let before = tree_digest(home(&bench));
    let second = bench.run(&["setup"], &[]);
    assert_eq!(second.status.code(), Some(0), "{}", stderr(&second));
    assert_eq!(
        Bench::rows(&second),
        BTreeMap::from([
            ("launchers".into(), "unchanged".into()),
            ("profiles".into(), "unchanged".into()),
            ("profile-mirrors".into(), "unchanged".into())
        ])
    );
    assert_eq!(tree_digest(home(&bench)), before);
}

#[test]
fn test_a_launcher_carries_the_accounts_environment() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let claude = fs::read_to_string(bench.launchers.join("claude-pri")).unwrap();
    let lines: Vec<_> = claude.lines().collect();
    assert_eq!(lines[1], "# written by yelo setup launchers: account pri");
    let profile = home(&bench).join(".claude/.profiles/pri");
    assert_eq!(
        lines[2],
        format!(
            "exec env AGENT_PROFILE_LABEL=pri CLAUDE_CONFIG_DIR={} CLAUDE_SECURESTORAGE_CONFIG_DIR={}/.claude-pri claude \"$@\"",
            profile.display(),
            home(&bench).display()
        )
    );
    let codex = fs::read_to_string(bench.launchers.join("codex-alt")).unwrap();
    let dir = home(&bench).join(".codex-alt");
    assert_eq!(
        codex.lines().nth(2),
        Some(
            format!(
                "exec env CODEX_HOME={} CODEX_CONFIG_PATH={}/config.toml codex \"$@\"",
                dir.display(),
                dir.display()
            )
            .as_str()
        )
    );
}

#[test]
fn test_launchers_work_without_yelo_on_path() {
    for (cli, account) in [("claude", "pri"), ("codex", "alt")] {
        let bench = Bench::new();
        seed_accounts(&bench);
        assert_eq!(
            bench.run(&["setup", "launchers"], &[]).status.code(),
            Some(0),
            "{cli}"
        );
        let vendor = bench.temp.root.join("vendor");
        mkdir(&vendor);
        let executable = vendor.join(cli);
        write(
            &executable,
            "#!/bin/sh\nprintf '%s\\n' \"$CLAUDE_CONFIG_DIR\" \"$CODEX_HOME\" \"$@\"\nexit 23\n",
        );
        set_mode(&executable, 0o755);
        symlink("/usr/bin/env", vendor.join("env")).unwrap();
        let args = [
            "--profile",
            "deep-review",
            "a prompt with spaces",
            "$(touch forbidden)",
        ];
        let result = Command::new(bench.launchers.join(format!("{cli}-{account}")))
            .args(args)
            .env_clear()
            .env("PATH", &vendor)
            .current_dir(&bench.temp.root)
            .output()
            .unwrap();
        assert_eq!(result.status.code(), Some(23), "{cli}: {}", stderr(&result));
        let directories = if cli == "claude" {
            vec![
                home(&bench)
                    .join(".claude/.profiles/pri")
                    .display()
                    .to_string(),
                String::new(),
            ]
        } else {
            vec![
                String::new(),
                home(&bench).join(".codex-alt").display().to_string(),
            ]
        };
        let expected: Vec<_> = directories.iter().map(String::as_str).chain(args).collect();
        assert_eq!(
            stdout(&result).lines().collect::<Vec<_>>(),
            expected,
            "{cli}"
        );
        assert!(!bench.temp.root.join("forbidden").exists(), "{cli}");
    }
}

#[test]
fn test_a_file_that_is_not_ours_is_kept() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let stranger = bench.launchers.join("claude-pri");
    write(&stranger, "#!/bin/sh\necho mine\n");
    let result = bench.run(&["setup", "launchers"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        fs::read_to_string(&stranger).unwrap(),
        "#!/bin/sh\necho mine\n"
    );
    assert!(row_detail(&result, "launchers").contains("kept"));
    assert!(row_detail(&result, "launchers").contains(&stranger.display().to_string()));
    let doctor = bench.run(&["doctor"], &[]);
    assert_eq!(Bench::rows(&doctor)["launchers"], "ok");
    assert!(row_detail(&doctor, "launchers").contains("kept, not a yelo launcher"));
}

#[test]
fn test_a_symlink_at_a_wanted_path_is_kept_not_replaced() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let real = bench.launchers.join("claude-pri");
    let body = fs::read_to_string(&real).unwrap();
    let link = bench.launchers.join("claude-work");
    fs::remove_file(&link).unwrap();
    symlink(&real, &link).unwrap();
    let result = bench.run(&["setup", "launchers"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert!(
        fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(link.canonicalize().unwrap(), real.canonicalize().unwrap());
    assert_eq!(fs::read_to_string(&real).unwrap(), body);
    assert!(
        row_detail(&result, "launchers")
            .contains(&format!("kept, not a yelo launcher: {}", link.display()))
    );
    let doctor = bench.run(&["doctor"], &[]);
    assert_eq!(Bench::rows(&doctor)["launchers"], "ok");
    assert!(
        row_detail(&doctor, "launchers")
            .contains(&format!("kept, not a yelo launcher: {}", link.display()))
    );
}

#[test]
fn test_a_stale_launcher_is_missing_not_ok() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let path = bench.launchers.join("claude-work");
    write(
        &path,
        &fs::read_to_string(&path)
            .unwrap()
            .replace(".profiles/work", ".profiles/work-old"),
    );
    let doctor = bench.run(&["doctor"], &[]);
    assert_eq!(Bench::rows(&doctor)["launchers"], "missing");
    assert!(row_detail(&doctor, "launchers").contains(&format!("stale: {}", path.display())));
    assert_eq!(
        Bench::rows(&bench.run(&["setup", "launchers"], &[]))["launchers"],
        "changed"
    );
    assert_eq!(Bench::rows(&bench.run(&["doctor"], &[]))["launchers"], "ok");
}

#[test]
fn test_the_launcher_of_a_deleted_account_is_stale_and_then_removed() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let orphan = bench.launchers.join("claude-work");
    assert!(orphan.is_file());
    fs::remove_dir_all(home(&bench).join(".claude/.profiles/work")).unwrap();
    let doctor = bench.run(&["doctor"], &[]);
    assert_eq!(Bench::rows(&doctor)["launchers"], "missing");
    assert!(row_detail(&doctor, "launchers").contains(&format!("stale: {}", orphan.display())));
    let result = bench.run(&["setup", "launchers"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(Bench::rows(&result)["launchers"], "changed");
    assert!(row_detail(&result, "launchers").contains(&format!(
        "removed, its account is gone: {}",
        orphan.display()
    )));
    assert!(!orphan.exists());
    assert_eq!(Bench::rows(&bench.run(&["doctor"], &[]))["launchers"], "ok");
}

#[test]
fn test_a_foreign_file_is_never_removed_as_an_orphan() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let stranger = bench.launchers.join("claude-stranger");
    write(&stranger, "#!/bin/sh\necho mine\n");
    let link = bench.launchers.join("claude-linked");
    symlink(bench.launchers.join("claude-pri"), &link).unwrap();
    assert_eq!(Bench::rows(&bench.run(&["doctor"], &[]))["launchers"], "ok");
    let result = bench.run(&["setup", "launchers"], &[]);
    assert_eq!(Bench::rows(&result)["launchers"], "unchanged");
    assert_eq!(
        fs::read_to_string(stranger).unwrap(),
        "#!/bin/sh\necho mine\n"
    );
    assert!(fs::symlink_metadata(link).unwrap().file_type().is_symlink());
}

#[test]
fn test_every_launcher_goes_when_the_last_account_does() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    for relative in [".claude/.profiles", ".codex", ".codex-alt", ".prime"] {
        fs::remove_dir_all(home(&bench).join(relative)).unwrap();
    }
    let result = bench.run(&["setup", "launchers"], &[]);
    assert_eq!(Bench::rows(&result)["launchers"], "changed");
    assert!(names(&bench.launchers).is_empty());
    assert_eq!(Bench::rows(&bench.run(&["doctor"], &[]))["launchers"], "ok");
}

#[test]
fn test_an_unlabelled_home_gets_no_launcher() {
    let bench = Bench::new();
    seed_accounts(&bench);
    write(&home(&bench).join(".codex/profile-label"), "\n");
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    assert!(!bench.launchers.join("codex-base").exists());
    assert!(bench.launchers.join("codex-alt").exists());
}

#[test]
fn test_setup_runs_one_named_step() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let result = bench.run(&["setup", "profiles"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        Bench::rows(&result)
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>(),
        ["profiles"]
    );
    assert!(!bench.launchers.exists());
    let unknown = bench.run(&["setup", "nope"], &[]);
    assert_eq!(unknown.status.code(), Some(1));
    assert_eq!(stderr(&unknown), "yelo: setup: unknown step: nope\n");
}

#[test]
fn test_setup_never_writes_the_claude_settings() {
    let bench = Bench::new();
    bench.seed_settings(&seeded_settings());
    let before = fs::read_to_string(&bench.settings).unwrap();
    seed_accounts(&bench);
    assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
    assert_eq!(fs::read_to_string(&bench.settings).unwrap(), before);
}

#[test]
fn test_setup_respects_dotfiles_ownership() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let real_profiles = bench.dotfiles.join("home/.claude/.profiles");
    mkdir(&real_profiles);
    fs::remove_dir_all(&bench.profiles).unwrap();
    symlink(&real_profiles, &bench.profiles).unwrap();
    install_hud_targets(home(&bench));
    bench.seed_settings(&seeded_settings());
    let result = bench.run(&["setup"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(Bench::rows(&result)["profiles"], "owned-by-dotfiles");
    assert!(
        fs::symlink_metadata(&bench.profiles)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    let doctor = bench.run(&["doctor"], &[]);
    assert!(stdout(&doctor).contains("profiles\towned-by-dotfiles"));
    assert_eq!(Bench::rows(&doctor)["usage"], "ok");
    assert_eq!(Bench::rows(&doctor)["hud"], "ok");
}

#[test]
fn test_profile_root_symlinked_elsewhere_is_an_error() {
    let bench = Bench::new();
    let elsewhere = home(&bench).join("elsewhere");
    mkdir(&elsewhere);
    mkdir(bench.profiles.parent().unwrap());
    symlink(&elsewhere, &bench.profiles).unwrap();
    let result = bench.run(&["setup", "profiles"], &[]);
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: setup: profile root is a symlink outside ~/dotfiles ({})\n",
            bench.profiles.display()
        )
    );
    assert!(
        fs::symlink_metadata(&bench.profiles)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert!(names(&elsewhere).is_empty());
}

#[test]
fn test_setup_json_output() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let result = bench.run(&["setup", "--json"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let report: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(
        report
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["step"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["launchers", "profiles", "profile-mirrors"]
    );
}

#[test]
fn test_every_account_core_reports_gets_a_launcher() {
    let bench = Bench::new();
    seed_accounts(&bench);
    let env = env_for(home(&bench), &bench.dotfiles, Some(&bench.bin));
    let mut listed = BTreeSet::new();
    for cli in ["claude", "codex"] {
        let result = run_yelo(&["profile", "list", "--cli", cli, "--json"], &env, None, "");
        assert_eq!(result.status.code(), Some(0), "{cli}: {}", stderr(&result));
        let rows: Value = serde_json::from_slice(&result.stdout).unwrap();
        for row in rows.as_array().unwrap() {
            let name = row["name"].as_str().unwrap();
            if !name.is_empty() {
                listed.insert(format!("{cli}-{name}"));
            }
        }
    }
    assert_eq!(
        listed.iter().map(String::as_str).collect::<BTreeSet<_>>(),
        accounts()
    );
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    assert_eq!(names(&bench.launchers), listed);
}
