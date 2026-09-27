mod common;

use common::{
    Fixture, TestHome, build_fixture_home, fixture_env, mkdir, run_yelo, set_mode, stderr, stdout,
    write,
};
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::Path;

fn mode(path: &Path) -> u32 {
    fs::metadata(path).unwrap().permissions().mode() & 0o777
}
fn names(path: &Path) -> Vec<String> {
    let mut names: Vec<_> = fs::read_dir(path)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .collect();
    names.sort();
    names
}

#[test]
fn test_create_each_cli() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    write(&home.join(".codex/hooks.json"), "{}\n");
    let real_agents = home.join("real-AGENTS.md");
    write(&real_agents, "house rules\n");
    symlink(&real_agents, home.join(".codex/AGENTS.md")).unwrap();
    let result = fixture.run(&[
        "profile",
        "create",
        "--cli",
        "claude",
        "zed",
        "--email",
        "zed@example.test",
        "--yes",
    ]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let lines: Vec<_> = stdout(&result).lines().map(str::to_string).collect();
    assert_eq!(
        lines[0],
        "Created profile 'zed'. Sign in with: claude-zed auth login"
    );
    let launcher = home.join(".local/bin/claude-zed");
    assert_eq!(lines[1], format!("Wrote {}.", launcher.display()));
    assert!(
        fs::read_to_string(&launcher)
            .unwrap()
            .lines()
            .nth(1)
            .unwrap()
            .ends_with("account zed")
    );
    let dir = home.join(".claude/.profiles/zed");
    assert_eq!(mode(&dir), 0o700);
    assert_eq!(
        fs::read_to_string(dir.join("email")).unwrap(),
        "zed@example.test\n"
    );
    assert_eq!(
        fs::read_to_string(dir.join(".claude.json")).unwrap(),
        "{}\n"
    );
    assert_eq!(mode(&dir.join(".claude.json")), 0o600);

    let result = fixture.run(&["profile", "create", "--cli", "codex", "zeta", "--yes"]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    let lines: Vec<_> = stdout(&result).lines().map(str::to_string).collect();
    assert_eq!(
        lines[0],
        "Created profile 'zeta'. Sign in with: codex-zeta login"
    );
    assert_eq!(
        lines[1],
        format!("Wrote {}.", home.join(".local/bin/codex-zeta").display())
    );
    let dir = home.join(".codex-zeta");
    assert_eq!(mode(&dir), 0o700);
    assert_eq!(mode(&dir.join("sessions")), 0o700);
    assert_eq!(
        fs::read_to_string(dir.join("config.toml")).unwrap(),
        "model = \"gpt-5-codex\"\n"
    );
    assert_eq!(mode(&dir.join("config.toml")), 0o600);
    assert_eq!(
        dir.join("hooks.json").canonicalize().unwrap(),
        home.join(".codex/hooks.json").canonicalize().unwrap()
    );
    assert_eq!(
        dir.join("AGENTS.md").canonicalize().unwrap(),
        real_agents.canonicalize().unwrap()
    );
    assert!(stdout(&fixture.run(&["profile", "list", "--cli", "codex"])).contains("zeta"));
}

#[test]
fn test_create_refuses_a_command_somebody_else_owns() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    let launcher = home.join(".local/bin/claude-zed");
    write(&launcher, "#!/bin/sh\necho mine\n");
    let result = fixture.run(&["profile", "create", "--cli", "claude", "zed", "--yes"]);
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "claude: 'zed' would need a command somebody else owns: {}\n",
            launcher.display()
        )
    );
    assert!(result.stdout.is_empty());
    assert!(!home.join(".claude/.profiles/zed").exists());
    assert_eq!(
        fs::read_to_string(launcher).unwrap(),
        "#!/bin/sh\necho mine\n"
    );
}

#[test]
fn test_create_refuses_a_symlink_even_to_one_of_our_own_launchers() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    let real = home.join(".local/bin/claude-pri");
    write(
        &real,
        "#!/bin/sh\n# written by yelo setup launchers: account pri\nexec env AGENT_PROFILE_LABEL=pri claude \"$@\"\n",
    );
    set_mode(&real, 0o755);
    let link = home.join(".local/bin/claude-zed");
    symlink(&real, &link).unwrap();
    let result = fixture.run(&["profile", "create", "--cli", "claude", "zed", "--yes"]);
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "claude: 'zed' would need a command somebody else owns: {}\n",
            link.display()
        )
    );
    assert!(result.stdout.is_empty());
    assert!(!home.join(".claude/.profiles/zed").exists());
    assert!(
        fs::symlink_metadata(&link)
            .unwrap()
            .file_type()
            .is_symlink()
    );
    assert_eq!(link.canonicalize().unwrap(), real.canonicalize().unwrap());
}

#[test]
fn test_a_launcher_that_cannot_be_written_is_reported() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    let binaries = home.join(".local/bin");
    mkdir(&binaries);
    set_mode(&binaries, 0o500);
    let result = fixture.run(&["profile", "create", "--cli", "claude", "zed", "--yes"]);
    set_mode(&binaries, 0o700);
    assert_eq!(result.status.code(), Some(1));
    assert!(stderr(&result).starts_with("claude: created "));
    assert!(stderr(&result).contains("its launcher could not be written"));
    assert!(stderr(&result).contains("yelo setup launchers"));
    assert!(home.join(".claude/.profiles/zed").is_dir());
}

#[test]
fn test_create_refusals() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    let before = names(home);
    let cases: &[(&[&str], i32, &str)] = &[
        (
            &["--cli", "claude", "bad name"],
            2,
            "claude: invalid profile name: bad name",
        ),
        (
            &["--cli", "claude", "é"],
            2,
            "claude: invalid profile name: é",
        ),
        (
            &["--cli", "codex", "é"],
            2,
            "codex: invalid profile name: é",
        ),
        (
            &["--cli", "claude", "ok", "--email", "--yes"],
            2,
            "claude: --email requires an address",
        ),
        (
            &["--cli", "codex", "ok", "--email", "a@b.test", "--yes"],
            2,
            "usage: yelo profile create --cli codex NAME [--yes]",
        ),
        (
            &["--cli", "claude", "pri", "--yes"],
            1,
            "claude: profile already exists: pri",
        ),
        (
            &["--cli", "claude", "wor", "--yes"],
            1,
            "claude: 'wor' already identifies an existing account",
        ),
        (
            &["--cli", "claude", "e", "--yes"],
            1,
            "claude: 'e' matches several existing accounts",
        ),
        (
            &["--cli", "codex", "fresh"],
            2,
            "codex: confirmation required; rerun with --yes in a non-interactive shell",
        ),
    ];
    for (argv, code, message) in cases {
        let mut args = vec!["profile", "create"];
        args.extend_from_slice(argv);
        let result = fixture.run(&args);
        assert_eq!(
            result.status.code(),
            Some(*code),
            "{argv:?}: {}",
            stderr(&result)
        );
        assert!(
            stderr(&result).contains(message),
            "{argv:?}: {}",
            stderr(&result)
        );
        assert!(result.stdout.is_empty(), "{argv:?}");
    }
    assert!(!home.join(".codex-fresh").exists());
    assert!(!home.join(".claude/.profiles/wor").exists());
    assert_eq!(names(home), before);
}

#[test]
fn test_create_dash_name() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    let before = names(home);
    for (cli, message) in [
        ("claude", "claude: invalid profile name: -bad"),
        ("codex", "codex: invalid profile name: -bad"),
    ] {
        let result = fixture.run(&["profile", "create", "--cli", cli, "--yes", "--", "-bad"]);
        assert_eq!(result.status.code(), Some(2), "{cli}");
        assert_eq!(stderr(&result), format!("{message}\n"), "{cli}");
        assert!(result.stdout.is_empty(), "{cli}");
    }
    for (cli, usage) in [
        (
            "claude",
            "usage: yelo profile create --cli claude NAME [--email ADDR] [--yes]",
        ),
        (
            "codex",
            "usage: yelo profile create --cli codex NAME [--yes]",
        ),
    ] {
        let result = fixture.run(&["profile", "create", "--cli", cli]);
        assert_eq!(result.status.code(), Some(2), "{cli}");
        assert_eq!(stderr(&result), format!("{usage}\n"), "{cli}");
        assert!(result.stdout.is_empty(), "{cli}");
    }
    let bare = fixture.run(&["profile", "create", "--cli", "codex", "--yes", "-bad"]);
    assert_eq!(bare.status.code(), Some(2));
    assert!(bare.stdout.is_empty());
    assert_eq!(names(home), before);
}

#[test]
fn test_create_refuses_symlinked_claude_root() {
    let temp = TestHome::new();
    build_fixture_home(&temp.home);
    let root = temp.home.join(".claude/.profiles");
    let elsewhere = temp.root.join("elsewhere");
    mkdir(&elsewhere);
    fs::rename(&root, temp.root.join("moved")).unwrap();
    symlink(&elsewhere, &root).unwrap();
    let result = run_yelo(
        &["profile", "create", "--cli", "claude", "zed", "--yes"],
        &fixture_env(&temp.home, common::FALSE_BIN),
        None,
        "",
    );
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "claude: profile root must not be a symlink: {}\n",
            root.display()
        )
    );
    assert!(names(&elsewhere).is_empty());
}
