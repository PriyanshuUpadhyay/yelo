mod common;

use common::{
    Env, TestHome, build_usage_home, mkdir, run_yelo, set_mode, stderr, stdout, write, write_json,
};
use serde_json::{Value, json};
use std::fs;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};
use std::process::{Command, Output, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs() as i64
}
fn quote(text: &str) -> String {
    format!("'{}'", text.replace('\u{27}', "'\\''"))
}
fn zsh() -> Option<PathBuf> {
    std::env::var_os("PATH").and_then(|path| {
        std::env::split_paths(&path)
            .map(|dir| dir.join("zsh"))
            .find(|path| path.is_file())
    })
}

#[link(name = "util")]
unsafe extern "C" {
    fn openpty(
        master: *mut i32,
        slave: *mut i32,
        name: *mut u8,
        termios: *const std::ffi::c_void,
        winsize: *const std::ffi::c_void,
    ) -> i32;
}

fn pty_pair() -> (fs::File, fs::File) {
    use std::os::fd::FromRawFd;
    let mut master = -1;
    let mut slave = -1;
    let status = unsafe {
        openpty(
            &mut master,
            &mut slave,
            std::ptr::null_mut(),
            std::ptr::null(),
            std::ptr::null(),
        )
    };
    assert_eq!(
        status,
        0,
        "openpty failed: {}",
        std::io::Error::last_os_error()
    );
    // openpty returns owned file descriptors on success.
    unsafe { (fs::File::from_raw_fd(master), fs::File::from_raw_fd(slave)) }
}

struct Installed {
    temp: TestHome,
    home: PathBuf,
    bin: PathBuf,
    env: Env,
    source: String,
    zsh: PathBuf,
}

impl Installed {
    fn new() -> Option<Self> {
        let zsh = zsh()?;
        let temp = TestHome::new();
        let home = temp.root.join("home with spaces");
        build_usage_home(&home, now());
        let bin = temp.root.join("bin");
        mkdir(&bin);
        let mut setup_env = Env::new();
        setup_env.insert("HOME".into(), home.display().to_string());
        setup_env.insert(
            "XDG_CONFIG_HOME".into(),
            home.join(".config").display().to_string(),
        );
        setup_env.insert("PATH".into(), bin.display().to_string());
        setup_env.insert(
            "YELO_DOTFILES_ROOT".into(),
            temp.root.join("dotfiles").display().to_string(),
        );
        let result = run_yelo(&["setup", "launchers"], &setup_env, None, "");
        assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
        let wrapper = bin.join("yelo");
        write(
            &wrapper,
            &format!(
                "#!/bin/sh\nexec {} \"$@\"\n",
                quote(env!("CARGO_BIN_EXE_yelo"))
            ),
        );
        set_mode(&wrapper, 0o755);
        for cli in ["claude", "codex"] {
            let path = bin.join(cli);
            write(
                &path,
                "#!/bin/sh\nprintf '%s\\n' \"home=$CODEX_HOME\" \"claude=$CLAUDE_CONFIG_DIR\" \"identity=$CLAUDE_SECURESTORAGE_CONFIG_DIR\"\nprintf 'arg=%s\\n' \"$@\"\nexit 23\n",
            );
            set_mode(&path, 0o755);
        }
        let mut env = Env::new();
        env.insert("HOME".into(), home.display().to_string());
        env.insert("PATH".into(), bin.display().to_string());
        env.insert(
            "AGENT_PROFILES_SECURITY_BIN".into(),
            common::TRUE_BIN.into(),
        );
        env.insert("PYTHONPATH".into(), "/missing/yelo".into());
        let source = format!(
            "source {}\n",
            quote(&home.join(".config/yelo/shell.sh").display().to_string())
        );
        Some(Self {
            temp,
            home,
            bin,
            env,
            source,
            zsh,
        })
    }

    fn run(&self, command: &str, extra: &[(&str, &str)]) -> Output {
        let mut env = self.env.clone();
        for (key, value) in extra {
            env.insert((*key).into(), (*value).into());
        }
        self.shell(&(self.source.clone() + command), &env)
    }

    fn shell(&self, script: &str, env: &Env) -> Output {
        Command::new(&self.zsh)
            .args(["-dfc", script])
            .env_clear()
            .envs(env)
            .current_dir(&self.home)
            .output()
            .unwrap()
    }

    fn fable_cache(&self, name: &str, pct: i64, expired: bool, reset_seconds: i64) {
        let path = self.home.join(format!(
            ".claude/.profiles/{name}/.usage-api-cache-fable.json"
        ));
        let now = now();
        write_json(
            &path,
            &json!({"seven_day": {"used_percentage": pct,
            "resets_at": if expired { now - 1 } else { now + reset_seconds }},
            "ts": now, "fetched_at": now, "source": "api"}),
        );
    }
}

#[test]
fn test_named_selection_without_yelo() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for (cli, name, suffix) in [
        ("claude", "pri", ".claude/.profiles/pri"),
        ("codex", "alt", ".codex-alt"),
    ] {
        let result = installed.run(
            &format!("{cli} --profile {name} 'hello world' '$(touch forbidden)'"),
            &[],
        );
        assert_eq!(result.status.code(), Some(23), "{cli}: {}", stderr(&result));
        let output = stdout(&result);
        assert!(
            output.contains(&installed.home.join(suffix).display().to_string()),
            "{cli}"
        );
        assert!(!output.contains("arg=--profile"), "{cli}");
        assert!(output.contains("arg=hello world"), "{cli}");
        assert!(output.contains("arg=$(touch forbidden)"), "{cli}");
        assert!(!installed.home.join("forbidden").exists(), "{cli}");
    }
}

#[test]
fn test_auto_selection_uses_existing_usage_ranking() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let expected = Command::new(installed.bin.join("yelo"))
            .args(["profile", "pick", "--cli", cli, "--json"])
            .env_clear()
            .envs(&installed.env)
            .output()
            .unwrap();
        assert_eq!(
            expected.status.code(),
            Some(0),
            "{cli}: {}",
            stderr(&expected)
        );
        let selected: Value = serde_json::from_slice(&expected.stdout).unwrap();
        let result = installed.run(cli, &[]);
        assert_eq!(result.status.code(), Some(23), "{cli}: {}", stderr(&result));
        assert!(
            stdout(&result).contains(selected["dir"].as_str().unwrap()),
            "{cli}"
        );
        assert!(stderr(&result).contains("expiring usage first"), "{cli}");
    }
}

#[test]
fn test_missing_yelo_runs_plain_cli() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let vendors = installed.temp.root.join(format!("vendors-{cli}"));
        mkdir(&vendors);
        symlink(installed.bin.join(cli), vendors.join(cli)).unwrap();
        let result = installed.run(
            &format!("{cli} --profile missing"),
            &[("PATH", &vendors.display().to_string())],
        );
        assert_eq!(result.status.code(), Some(23), "{cli}: {}", stderr(&result));
        assert!(
            stdout(&result).contains("arg=--profile\narg=missing"),
            "{cli}"
        );
    }
}

#[test]
fn test_bare_profile_uses_real_terminal_menu() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let script = installed.source.clone() + &format!("{cli} --profile");
        let (mut master, slave) = pty_pair();
        let mut child = Command::new(&installed.zsh)
            .args(["-dfc", &script])
            .env_clear()
            .envs(&installed.env)
            .current_dir(&installed.home)
            .stdin(Stdio::from(slave.try_clone().unwrap()))
            .stdout(Stdio::from(slave.try_clone().unwrap()))
            .stderr(Stdio::from(slave))
            .spawn()
            .unwrap();
        use std::io::{Read, Write};
        use std::sync::mpsc;
        let (prompt, ready) = mpsc::channel();
        let mut stream = master.try_clone().unwrap();
        let reader = std::thread::spawn(move || {
            let mut bytes = Vec::new();
            let mut chunk = [0_u8; 4096];
            let mut sent = false;
            while let Ok(n) = stream.read(&mut chunk) {
                if n == 0 {
                    break;
                }
                bytes.extend_from_slice(&chunk[..n]);
                if !sent
                    && bytes
                        .windows(b"choice:".len())
                        .any(|window| window == b"choice:")
                {
                    prompt.send(()).unwrap();
                    sent = true;
                }
            }
            bytes
        });
        if ready
            .recv_timeout(std::time::Duration::from_secs(10))
            .is_err()
        {
            child.kill().unwrap();
            panic!("{cli}: no menu prompt");
        }
        master.write_all(b"2\n").unwrap();
        let status = child.wait().unwrap();
        let output = String::from_utf8_lossy(&reader.join().unwrap()).into_owned();
        assert_eq!(status.code(), Some(23), "{cli}: {output}");
        assert!(output.contains("select an account"), "{cli}");
        let suffix = if cli == "claude" {
            ".profiles/work"
        } else {
            ".codex-alt"
        };
        assert!(output.contains(suffix), "{cli}");
    }
}

#[test]
fn test_piped_menu_fails_without_starting_vendor() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let result = installed.run(&format!("{cli} --profile"), &[]);
        assert_eq!(result.status.code(), Some(2), "{cli}: {}", stderr(&result));
        assert!(result.stdout.is_empty(), "{cli}");
    }
}

#[test]
fn test_inherited_account_is_not_auto_switched() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for (cli, key) in [("claude", "CLAUDE_CONFIG_DIR"), ("codex", "CODEX_HOME")] {
        let result = installed.run(cli, &[(key, "/explicit/account")]);
        assert_eq!(result.status.code(), Some(23), "{cli}: {}", stderr(&result));
        assert!(stdout(&result).contains("/explicit/account"), "{cli}");
        assert!(!stderr(&result).contains("using profile"), "{cli}");
    }
}

#[test]
fn test_native_config_and_host_guard_are_preserved() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let result = installed.run("\n_codex_host_guard() { [[ \"$1\" != exec ]] || return 19; }\ncodex --profile alt -p deep-review -C '/tmp/a b' 'hello world'\n", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains("arg=-p\narg=deep-review\narg=-C\narg=/tmp/a b"));
    let result = installed.run("\n_codex_host_guard() { [[ \"$1\" != exec ]] || return 19; }\ncodex --profile alt -C /tmp exec hello\n", &[]);
    assert_eq!(result.status.code(), Some(19));
    assert!(result.stdout.is_empty());
}

#[test]
fn test_unknown_account_never_falls_back() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let result = installed.run(&format!("{cli} --profile missing"), &[]);
        assert_eq!(result.status.code(), Some(2), "{cli}");
        assert!(result.stdout.is_empty(), "{cli}");
    }
}

#[test]
fn test_unrelated_shell_definitions_survive() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let script = format!(
        "alias codex='print foreign-codex'\nclaude() {{ print foreign-claude; }}\n{}eval codex\nclaude",
        installed.source
    );
    let result = installed.shell(&script, &installed.env);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        stdout(&result).lines().collect::<Vec<_>>(),
        ["foreign-codex", "foreign-claude"]
    );
}

#[test]
fn test_bound_commands_cannot_auto_switch_in_a_pipe() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for command in [
        "claude --resume",
        "claude auth login",
        "codex resume",
        "codex login",
    ] {
        let result = installed.run(command, &[]);
        assert_eq!(result.status.code(), Some(2), "{command}");
        assert!(result.stdout.is_empty(), "{command}");
    }
}

#[test]
fn test_named_session_runs_in_the_account_that_owns_it() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let session = common::OWNED_SESSION;
    let result = installed.run(&format!("codex -C /tmp resume {session} 'carry on'"), &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "home={}",
        installed.home.join(".codex-alt").display()
    )));
    assert!(stdout(&result).contains(&format!(
        "arg=-C\narg=/tmp\narg=resume\narg={session}\narg=carry on"
    )));
    assert!(stderr(&result).contains("owns the session"));
    let result = installed.run("codex resume 019e08eb-0000-7000-8000-000000000000", &[]);
    assert_eq!(result.status.code(), Some(2));
    assert!(result.stdout.is_empty());
}

#[test]
fn test_help_passes_through_without_account_selection() {
    let Some(installed) = Installed::new() else {
        return;
    };
    for cli in ["claude", "codex"] {
        let result = installed.run(&format!("{cli} --help"), &[]);
        assert_eq!(result.status.code(), Some(23), "{cli}");
        assert!(!stderr(&result).contains("using profile"), "{cli}");
        assert!(stdout(&result).contains("arg=--help"), "{cli}");
    }
}

#[test]
fn test_current_shell_can_replace_the_obsolete_wrapper() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let old = installed.home.join("old.zsh");
    write(
        &old,
        &fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("src/standalone_shell.zsh"))
            .unwrap(),
    );
    let script = format!(
        "_codex_host_guard() {{ return 0; }}\nsource {}\n{}{}codex --profile alt",
        quote(&old.display().to_string()),
        installed.source,
        installed.source
    );
    let result = installed.shell(&script, &installed.env);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&installed.home.join(".codex-alt").display().to_string()));
}

#[test]
fn test_foreign_integration_file_is_not_overwritten() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let path = installed.home.join(".config/yelo/shell.sh");
    write(&path, "# My custom shell integration\n");
    let result = run_yelo(&["setup", "launchers"], &installed.env, None, "");
    assert_eq!(result.status.code(), Some(1));
    assert!(stderr(&result).contains("another owner"));
    assert_eq!(
        fs::read_to_string(path).unwrap(),
        "# My custom shell integration\n"
    );
}

#[test]
fn test_claude_model_selects_usable_fable_account() {
    for (model, expected) in [
        ("fable", "work"),
        ("claude-fable-5", "work"),
        ("best", "work"),
        ("sonnet", "pri"),
    ] {
        let Some(installed) = Installed::new() else {
            return;
        };
        installed.fable_cache("pri", 100, false, 86400);
        let result = installed.run(&format!("claude --model {model}"), &[]);
        assert_eq!(
            result.status.code(),
            Some(23),
            "{model}: {}",
            stderr(&result)
        );
        assert!(
            stdout(&result).contains(&format!(
                "claude={}",
                installed
                    .home
                    .join(format!(".claude/.profiles/{expected}"))
                    .display()
            )),
            "{model}"
        );
        assert!(
            stdout(&result).contains(&format!("arg=--model\narg={model}")),
            "{model}"
        );
    }
}

#[test]
fn test_startup_model_sources_reach_installed_selector() {
    for source in ["user", "project", "environment", "override", "alias"] {
        let Some(installed) = Installed::new() else {
            return;
        };
        installed.fable_cache("pri", 100, false, 86400);
        let mut command = "claude".to_string();
        let mut extra = Vec::new();
        match source {
            "user" => write(
                &installed.home.join(".claude/settings.json"),
                "{\"model\":\"fable\"}",
            ),
            "project" => {
                mkdir(&installed.home.join(".git"));
                write(
                    &installed.home.join(".claude/settings.local.json"),
                    "{\"model\":\"fable\"}",
                );
            }
            "environment" => extra.push(("ANTHROPIC_MODEL", "fable")),
            "override" => command.push_str(" --settings '{\"model\":\"fable\"}'"),
            "alias" => {
                extra.push(("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-fable-5"));
                command.push_str(" --model sonnet");
            }
            _ => unreachable!(),
        }
        let result = installed.run(&command, &extra);
        assert_eq!(
            result.status.code(),
            Some(23),
            "{source}: {}",
            stderr(&result)
        );
        assert!(
            stdout(&result).contains(&format!(
                "claude={}",
                installed.home.join(".claude/.profiles/work").display()
            )),
            "{source}"
        );
    }
}

#[test]
fn test_all_fable_exhausted_stops_before_vendor_launch() {
    for single in [false, true] {
        let Some(installed) = Installed::new() else {
            return;
        };
        installed.fable_cache("pri", 100, false, 86400);
        if single {
            fs::remove_dir_all(installed.home.join(".claude/.profiles/work")).unwrap();
        } else {
            installed.fable_cache("work", 100, false, 86400);
        }
        let result = installed.run("claude --model fable", &[]);
        assert_eq!(result.status.code(), Some(2), "single={single}");
        assert!(
            stderr(&result).contains("Fable usage is exhausted"),
            "single={single}"
        );
        assert!(result.stdout.is_empty(), "single={single}");
    }
}

#[test]
fn test_fable_reset_restores_eligibility() {
    let Some(installed) = Installed::new() else {
        return;
    };
    installed.fable_cache("pri", 100, true, 86400);
    installed.fable_cache("work", 100, false, 86400);
    let result = installed.run("claude --model=fable", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/pri").display()
    )));
}

#[test]
fn test_explicit_profile_stays_in_control_with_fable() {
    let Some(installed) = Installed::new() else {
        return;
    };
    installed.fable_cache("pri", 100, false, 86400);
    let result = installed.run("claude --profile pri --model fable", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/pri").display()
    )));
}

#[test]
fn test_fable_window_participates_in_ratio_ranking() {
    let Some(installed) = Installed::new() else {
        return;
    };
    installed.fable_cache("pri", 90, false, 86400);
    installed.fable_cache("work", 20, false, 3600);
    let result = installed.run("claude --model fable", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/work").display()
    )));
    let result = installed.run("claude --model sonnet", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/pri").display()
    )));
}

#[test]
fn test_explicit_model_overrides_configured_fable() {
    let Some(installed) = Installed::new() else {
        return;
    };
    installed.fable_cache("pri", 100, false, 86400);
    write(
        &installed.home.join(".claude/settings.json"),
        "{\"model\":\"fable\"}",
    );
    let result = installed.run("claude --model sonnet", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/pri").display()
    )));
}

#[test]
fn test_fable_ranks_by_the_fable_window_not_a_hot_session_window() {
    let Some(installed) = Installed::new() else {
        return;
    };
    let profile = installed.home.join(".claude/.profiles/pri");
    fs::remove_file(profile.join(".usage-cache.json")).unwrap();
    let now = now();
    write_json(
        &profile.join(".usage-api-cache.json"),
        &json!({
            "five_hour": {"used_percentage": 0, "resets_at": now + 300},
            "seven_day": {"used_percentage": 10, "resets_at": now + 5 * 86400},
            "ts": now, "fetched_at": now, "source": "api"
        }),
    );
    installed.fable_cache("pri", 90, false, 86400);
    installed.fable_cache("work", 20, false, 3 * 86400);
    let result = installed.run("claude --model fable", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/work").display()
    )));
    let result = installed.run("claude --model sonnet", &[]);
    assert_eq!(result.status.code(), Some(23), "{}", stderr(&result));
    assert!(stdout(&result).contains(&format!(
        "claude={}",
        installed.home.join(".claude/.profiles/pri").display()
    )));
}

#[test]
fn test_codex_model_with_its_own_limit_picks_by_that_limit() {
    let cases = [
        ("codex", None, ".codex-alt"),
        ("codex -m gpt-5.3-codex-spark", None, ".codex"),
        ("codex exec --model=GPT-5.3-Codex-Spark hi", None, ".codex"),
        ("codex -c 'model=\"gpt-5.3-codex-spark\"'", None, ".codex"),
        ("codex", Some("model = \"gpt-5.3-codex-spark\"\n"), ".codex"),
        (
            "codex -m gpt-6-astra",
            Some("model = \"gpt-5.3-codex-spark\"\n"),
            ".codex-alt",
        ),
    ];
    for (command, config, expected) in cases {
        let Some(installed) = Installed::new() else {
            return;
        };
        let cache = installed.home.join(".codex/.usage-hud-api-cache.json");
        let mut document: Value = serde_json::from_slice(&fs::read(&cache).unwrap()).unwrap();
        document["limits_by_id"] = json!({"codex_bengalfox": {
            "limit_name": "GPT-5.3-Codex-Spark",
            "primary": {"used_percent": 0, "window_minutes": 300, "resets_at": now() + 600},
            "secondary": null
        }});
        write_json(&cache, &document);
        if let Some(config) = config {
            write(&installed.home.join(".codex/config.toml"), config);
        }
        let result = installed.run(command, &[]);
        assert_eq!(
            result.status.code(),
            Some(23),
            "{command}: {}",
            stderr(&result)
        );
        assert!(
            stdout(&result).contains(&format!(
                "home={}\n",
                installed.home.join(expected).display()
            )),
            "{command}"
        );
    }
}
