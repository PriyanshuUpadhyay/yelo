mod common;

use common::{
    Bench, build_fixture_home, mkdir, seeded_settings, stderr, stdout, tree_digest, write,
    write_plist,
};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

const HUD_LABEL: &str = "io.github.priyanshuupadhyay.yelo-hud";
const LEGACY_PLIST: &str = "work.example.usage-hud.plist";
const USAGE_LINKS: [&str; 2] = ["usage-hud-data", "usage-hud-fetch"];
fn home(bench: &Bench) -> &Path {
    &bench.temp.home
}
fn seed_accounts(bench: &Bench) {
    build_fixture_home(home(bench));
}
fn install_hud_targets(bench: &Bench) {
    write_plist(
        &home(bench).join(format!("Library/LaunchAgents/{HUD_LABEL}.plist")),
        &json!({"Label": HUD_LABEL}),
    );
    write(
        &home(bench).join("Applications/UsageHUD.app/Contents/MacOS/UsageHUD"),
        "binary\n",
    );
}
fn verdicts(output: &std::process::Output) -> BTreeMap<String, String> {
    Bench::rows(output)
}
fn detail(output: &std::process::Output, name: &str) -> String {
    stdout(output)
        .lines()
        .find(|line| line.starts_with(&format!("{name}\t")))
        .unwrap()
        .split('\t')
        .nth(2)
        .unwrap()
        .to_string()
}

fn arrange(bench: &Bench, name: &str) -> (&'static str, &'static str) {
    match name {
        "absent" => ("missing", "missing"),
        "no_accounts" => {
            assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
            ("ok", "ok")
        }
        "installed" => {
            seed_accounts(bench);
            assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
            ("ok", "ok")
        }
        "stale_launcher" => {
            seed_accounts(bench);
            assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
            let path = bench.launchers.join("claude-pri");
            write(
                &path,
                &fs::read_to_string(&path)
                    .unwrap()
                    .replace(".profiles/pri", ".profiles/pri-old"),
            );
            ("missing", "ok")
        }
        "host_context_wired" => {
            bench.seed_settings(&seeded_settings());
            ("missing", "missing")
        }
        "settings_not_json" => {
            write(&bench.settings, "{ not json");
            ("missing", "missing")
        }
        "profiles_owned_by_dotfiles" => {
            let real = bench.dotfiles.join("home/.claude/.profiles");
            mkdir(&real);
            mkdir(bench.profiles.parent().unwrap());
            symlink(real, &bench.profiles).unwrap();
            ("missing", "owned-by-dotfiles")
        }
        "profiles_symlinked_elsewhere" => {
            let elsewhere = home(bench).join("elsewhere");
            mkdir(&elsewhere);
            mkdir(bench.profiles.parent().unwrap());
            symlink(elsewhere, &bench.profiles).unwrap();
            ("missing", "missing")
        }
        "profiles_is_a_file" => {
            write(&bench.profiles, "not a directory\n");
            ("missing", "missing")
        }
        "settings_symlinked_into_dotfiles" => {
            let target = bench.dotfiles.join("home/.claude/settings.json");
            write(
                &target,
                &(serde_json::to_string_pretty(&seeded_settings()).unwrap() + "\n"),
            );
            mkdir(bench.settings.parent().unwrap());
            symlink(target, &bench.settings).unwrap();
            ("missing", "missing")
        }
        "settings_symlinked_elsewhere" => {
            let stranger = home(bench).join("stranger.json");
            write(&stranger, "{\"model\": \"opus\"}\n");
            mkdir(bench.settings.parent().unwrap());
            symlink(stranger, &bench.settings).unwrap();
            ("missing", "missing")
        }
        _ => unreachable!(),
    }
}

#[test]
fn test_doctor_matrix() {
    for name in [
        "absent",
        "no_accounts",
        "installed",
        "stale_launcher",
        "host_context_wired",
        "settings_not_json",
        "profiles_owned_by_dotfiles",
        "profiles_symlinked_elsewhere",
        "profiles_is_a_file",
        "settings_symlinked_into_dotfiles",
        "settings_symlinked_elsewhere",
    ] {
        let bench = Bench::new();
        install_hud_targets(&bench);
        let (launchers, profiles) = arrange(&bench, name);
        let expected = BTreeMap::from([
            ("launchers".to_string(), launchers.to_string()),
            ("profiles".into(), profiles.into()),
            ("profile-mirrors".into(), "ok".into()),
            ("usage".into(), "ok".into()),
            ("hud".into(), "ok".into()),
        ]);
        let before = tree_digest(home(&bench));
        let result = bench.run(&["doctor"], &[]);
        assert_eq!(verdicts(&result), expected, "{name}");
        assert_eq!(
            result.status.code(),
            Some(if expected.values().any(|state| state == "missing") {
                1
            } else {
                0
            }),
            "{name}"
        );
        assert_eq!(tree_digest(home(&bench)), before, "{name}");
        for line in stdout(&result).lines() {
            let fields: Vec<_> = line.split('\t').collect();
            assert_eq!(fields.len(), 3, "{name}: {line}");
            assert!(!fields[2].is_empty(), "{name}: {line}");
        }
    }
}

#[test]
fn test_doctor_json_matches_the_table() {
    let bench = Bench::new();
    assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
    install_hud_targets(&bench);
    let before = tree_digest(home(&bench));
    let result = bench.run(&["doctor", "--json"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let report: Value = serde_json::from_slice(&result.stdout).unwrap();
    assert_eq!(
        report
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row["step"].as_str().unwrap())
            .collect::<Vec<_>>(),
        ["launchers", "profiles", "profile-mirrors", "usage", "hud"]
    );
    assert!(
        report
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["state"] == "ok")
    );
    assert_eq!(tree_digest(home(&bench)), before);
}

#[test]
fn test_doctor_never_creates_the_launcher_directory() {
    let bench = Bench::new();
    seed_accounts(&bench);
    assert_eq!(bench.run(&["doctor"], &[]).status.code(), Some(1));
    assert!(!bench.launchers.exists());
}

#[test]
fn test_doctor_reports_profile_mirror_drift_without_repairing_it() {
    let bench = Bench::new();
    seed_accounts(&bench);
    mkdir(&home(&bench).join(".claude/projects"));
    assert_eq!(bench.run(&["setup"], &[]).status.code(), Some(0));
    let drift = bench.profiles.join("pri/projects");
    fs::remove_file(&drift).unwrap();
    mkdir(&drift);
    let config = bench.profiles.join("work/.claude.json");
    fs::remove_file(&config).unwrap();
    let before = tree_digest(home(&bench));
    let result = bench.run(&["doctor"], &[]);
    assert_eq!(verdicts(&result)["profile-mirrors"], "missing");
    assert!(stdout(&result).contains(&format!("drift: {}", drift.display())));
    assert!(stdout(&result).contains(&format!("missing: {}", config.display())));
    assert_eq!(tree_digest(home(&bench)), before);
}

fn hud_dotfiles(bench: &Bench) -> PathBuf {
    let root = home(bench).join("dotfiles");
    let binaries = root.join("home/.local/bin");
    let agents = root.join("home/Library/LaunchAgents");
    mkdir(&binaries);
    mkdir(&agents);
    for name in USAGE_LINKS {
        write(&binaries.join(name), "#!/bin/sh\n");
    }
    write_plist(
        &agents.join(LEGACY_PLIST),
        &json!({"Label": "work.example.usage-hud"}),
    );
    root
}
fn link_usage_commands(bench: &Bench, root: &Path) {
    let binaries = home(bench).join(".local/bin");
    mkdir(&binaries);
    for name in USAGE_LINKS {
        symlink(root.join("home/.local/bin").join(name), binaries.join(name)).unwrap();
    }
}
fn link_legacy_agent(bench: &Bench, root: &Path) {
    let agents = home(bench).join("Library/LaunchAgents");
    mkdir(&agents);
    symlink(
        root.join("home/Library/LaunchAgents").join(LEGACY_PLIST),
        agents.join(LEGACY_PLIST),
    )
    .unwrap();
}

#[test]
fn test_doctor_hud_matrix() {
    let cases = [
        (
            "before-cutover",
            true,
            true,
            false,
            ("owned-by-dotfiles", "owned-by-dotfiles"),
            0,
        ),
        (
            "both-installed",
            true,
            true,
            true,
            ("owned-by-dotfiles", "owned-by-dotfiles"),
            0,
        ),
        ("after-cutover", false, false, true, ("ok", "ok"), 0),
        (
            "nothing-installed",
            false,
            false,
            false,
            ("ok", "missing"),
            1,
        ),
        (
            "agent-only",
            false,
            true,
            false,
            ("ok", "owned-by-dotfiles"),
            0,
        ),
    ];
    for (name, links, legacy, yelo_pair, expected, code) in cases {
        let bench = Bench::new();
        let root = hud_dotfiles(&bench);
        let root_string = root.display().to_string();
        assert_eq!(
            bench
                .run(&["setup"], &[("YELO_DOTFILES_ROOT", &root_string)])
                .status
                .code(),
            Some(0),
            "{name}"
        );
        if links {
            link_usage_commands(&bench, &root);
        }
        if legacy {
            link_legacy_agent(&bench, &root);
        }
        if yelo_pair {
            install_hud_targets(&bench);
        }
        let before = tree_digest(home(&bench));
        let result = bench.run(&["doctor"], &[("YELO_DOTFILES_ROOT", &root_string)]);
        let rows = verdicts(&result);
        assert_eq!(
            (rows["usage"].as_str(), rows["hud"].as_str()),
            expected,
            "{name}"
        );
        assert_eq!(result.status.code(), Some(code), "{name}");
        assert_eq!(tree_digest(home(&bench)), before, "{name}");
        assert_eq!(
            detail(&result, "usage").contains(&root_string),
            links,
            "{name}"
        );
        assert_eq!(
            detail(&result, "hud").contains(LEGACY_PLIST),
            legacy,
            "{name}"
        );
        if !legacy {
            assert!(detail(&result, "hud").contains(HUD_LABEL), "{name}");
        }
    }
}

#[test]
fn test_doctor_hud_row_never_asks_launchd() {
    let bench = Bench::new();
    install_hud_targets(&bench);
    let result = bench.run(&["doctor"], &[("PATH", "")]);
    assert_eq!(verdicts(&result)["hud"], "ok");
}
