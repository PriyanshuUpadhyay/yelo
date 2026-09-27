use super::{home, resolve, rows, valid_name};
use serde_json::Value;
use std::{
    fs, io,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
};

fn mode(path: &Path, value: u32) -> io::Result<()> {
    fs::set_permissions(path, fs::Permissions::from_mode(value))
}
fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
fn excluded(name: &str) -> bool {
    matches!(name, ".profiles" | ".claude.json" | "email")
        || name.starts_with(".usage-cache")
        || name.starts_with(".usage-api-cache")
}
fn shared_names(home: &Path) -> Vec<String> {
    let mut names: Vec<_> = super::dirs(&home.join(".claude"))
        .into_iter()
        .filter_map(|p| p.file_name()?.to_str().map(str::to_owned))
        .filter(|name| !excluded(name))
        .collect();
    names.sort();
    names
}
pub(crate) fn mirror_sync(name: &str, home: &Path) -> io::Result<Vec<PathBuf>> {
    let profile = home.join(".claude/.profiles").join(name);
    if !profile.is_dir() {
        fs::create_dir_all(&profile)?;
        mode(&profile, 0o700)?;
    }
    let names = shared_names(home);
    let mut drift = Vec::new();
    for item in &names {
        let path = profile.join(item);
        let target = PathBuf::from("../..").join(item);
        if let Ok(existing) = fs::read_link(&path) {
            if existing == target {
                continue;
            }
            fs::remove_file(&path)?;
        } else if exists(&path) {
            drift.push(path);
            continue;
        }
        symlink(target, path)?;
    }
    for path in super::dirs(&profile) {
        let Some(item) = path.file_name().and_then(|n| n.to_str()) else {
            continue;
        };
        if !excluded(item) && !names.iter().any(|name| name == item) {
            drift.push(path);
        }
    }
    let config = profile.join(".claude.json");
    let source = home.join(".claude.json");
    if !exists(&config) && source.is_file() {
        let bytes = fs::read(&source)?;
        if let Ok(mut data) = serde_json::from_slice::<Value>(&bytes) {
            if let Value::Object(object) = &mut data {
                object.remove("oauthAccount");
            }
            fs::write(&config, format!("{}\n", serde_json::to_string(&data)?))?;
        } else {
            fs::write(&config, bytes)?;
        }
        mode(&config, 0o600)?;
    }
    drift.sort();
    drift.dedup();
    Ok(drift)
}
fn shell_quote(text: &str) -> String {
    if !text.is_empty()
        && text
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
    {
        text.to_owned()
    } else {
        format!("'{}'", text.replace('\'', "'\"'\"'"))
    }
}
pub(crate) fn launcher(cli: &str, name: &str, directory: &Path, home: &Path) -> String {
    let environment = if cli == "claude" {
        vec![
            ("AGENT_PROFILE_LABEL", name.to_owned()),
            (
                "CLAUDE_CONFIG_DIR",
                directory.to_string_lossy().into_owned(),
            ),
            (
                "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                home.join(format!(".claude-{name}"))
                    .to_string_lossy()
                    .into_owned(),
            ),
        ]
    } else {
        vec![
            ("CODEX_HOME", directory.to_string_lossy().into_owned()),
            (
                "CODEX_CONFIG_PATH",
                directory.join("config.toml").to_string_lossy().into_owned(),
            ),
        ]
    };
    let assignments = environment
        .iter()
        .map(|(key, value)| format!("{key}={}", shell_quote(value)))
        .collect::<Vec<_>>()
        .join(" ");
    format!(
        "#!/bin/sh\n# written by yelo setup launchers: account {name}\nexec env {assignments} {cli} \"$@\"\n"
    )
}
pub(crate) fn owned_launcher(path: &Path) -> bool {
    if path.is_symlink() {
        return false;
    }
    fs::read_to_string(path)
        .ok()
        .and_then(|text| text.lines().nth(1).map(str::to_owned))
        .is_some_and(|line| line.starts_with("# written by yelo setup launchers: account "))
}
fn write_launcher(cli: &str, name: &str, directory: &Path, home: &Path) -> io::Result<PathBuf> {
    let bin = home.join(".local/bin");
    fs::create_dir_all(&bin)?;
    let path = bin.join(format!("{cli}-{name}"));
    let temporary = bin.join(format!(".{cli}-{name}.{}", std::process::id()));
    fs::write(&temporary, launcher(cli, name, directory, home))?;
    mode(&temporary, 0o755)?;
    fs::rename(&temporary, &path)?;
    Ok(path)
}
fn link_target(path: &Path) -> Option<PathBuf> {
    if !path.exists() {
        return None;
    }
    if path.is_symlink() {
        fs::canonicalize(path).ok()
    } else {
        Some(path.to_owned())
    }
}
fn create_inner(
    cli: &str,
    name: &str,
    email: Option<&str>,
    yes: bool,
    home: &Path,
) -> Result<String, (String, i32)> {
    let usage = if cli == "claude" {
        "usage: yelo profile create --cli claude NAME [--email ADDR] [--yes]"
    } else {
        "usage: yelo profile create --cli codex NAME [--yes]"
    };
    if email.is_some() && cli != "claude" {
        return Err((usage.into(), 2));
    }
    if email == Some("") {
        return Err((format!("{cli}: --email requires an address"), 2));
    }
    if !valid_name(name) {
        return Err((format!("{cli}: invalid profile name: {name}"), 2));
    }
    let root = home.join(".claude/.profiles");
    if cli == "claude" && root.is_symlink() {
        return Err((
            format!(
                "claude: profile root must not be a symlink: {}",
                root.display()
            ),
            1,
        ));
    }
    let directory = if cli == "claude" {
        root.join(name)
    } else {
        home.join(format!(".codex-{name}"))
    };
    if exists(&directory) {
        return Err((format!("{cli}: profile already exists: {name}"), 1));
    }
    match resolve(cli, name) {
        Ok(_) => {
            return Err((
                format!("{cli}: '{name}' already identifies an existing account"),
                1,
            ));
        }
        Err((_, 2, _)) => {
            return Err((
                format!("{cli}: '{name}' matches several existing accounts"),
                1,
            ));
        }
        _ => {}
    }
    let launcher_path = home.join(".local/bin").join(format!("{cli}-{name}"));
    if exists(&launcher_path) && !owned_launcher(&launcher_path) {
        return Err((
            format!(
                "{cli}: '{name}' would need a command somebody else owns: {}",
                launcher_path.display()
            ),
            1,
        ));
    }
    if !yes {
        return Err((
            format!("{cli}: confirmation required; rerun with --yes in a non-interactive shell"),
            2,
        ));
    }
    if cli == "claude" {
        if !root.is_dir() {
            fs::create_dir_all(&root).map_err(|e| (format!("{cli}: {e}"), 1))?;
            mode(&root, 0o700).map_err(|e| (format!("{cli}: {e}"), 1))?;
        }
        fs::create_dir(&directory).map_err(|e| (format!("{cli}: {e}"), 1))?;
        mode(&directory, 0o700).map_err(|e| (format!("{cli}: {e}"), 1))?;
        if let Some(email) = email {
            fs::write(directory.join("email"), format!("{email}\n"))
                .map_err(|e| (format!("{cli}: {e}"), 1))?;
        }
        mirror_sync(name, home).map_err(|e| (format!("{cli}: {e}"), 1))?;
    } else {
        fs::create_dir(&directory).map_err(|e| (format!("{cli}: {e}"), 1))?;
        mode(&directory, 0o700).map_err(|e| (format!("{cli}: {e}"), 1))?;
        let sessions = directory.join("sessions");
        fs::create_dir(&sessions).map_err(|e| (format!("{cli}: {e}"), 1))?;
        mode(&sessions, 0o700).map_err(|e| (format!("{cli}: {e}"), 1))?;
        let base = home.join(".codex");
        let config = base.join("config.toml");
        if config.is_file() {
            let target = directory.join("config.toml");
            fs::copy(config, &target).map_err(|e| (format!("{cli}: {e}"), 1))?;
            mode(&target, 0o600).map_err(|e| (format!("{cli}: {e}"), 1))?;
        }
        for item in ["hooks.json", "AGENTS.md"] {
            if let Some(target) = link_target(&base.join(item)) {
                symlink(target, directory.join(item)).map_err(|e| (format!("{cli}: {e}"), 1))?;
            }
        }
    }
    let written = write_launcher(cli, name, &directory, home).map_err(|e| (format!("{cli}: created {}, but its launcher could not be written: {e}. Run `yelo setup launchers` to finish.", directory.display()), 1))?;
    let login = if cli == "claude" {
        "auth login"
    } else {
        "login"
    };
    Ok(format!(
        "Created profile '{name}'. Sign in with: {cli}-{name} {login}\nWrote {}.",
        written.display()
    ))
}
pub(super) fn create(cli: &str, args: &[String]) -> i32 {
    if !matches!(cli, "claude" | "codex") {
        eprintln!("yelo: profile create: unsupported provider: {cli}");
        return 2;
    }
    let usage = if cli == "claude" {
        "usage: yelo profile create --cli claude NAME [--email ADDR] [--yes]"
    } else {
        "usage: yelo profile create --cli codex NAME [--yes]"
    };
    let mut name = None;
    let mut email = None;
    let mut index = 2;
    while index < args.len() {
        match args[index].as_str() {
            "--cli" => index += 2,
            "--email" => {
                email = Some(
                    args.get(index + 1)
                        .filter(|s| !s.starts_with('-'))
                        .map(String::as_str)
                        .unwrap_or(""),
                );
                index += if email == Some("") { 1 } else { 2 };
            }
            "--yes" | "-y" => index += 1,
            "--" => {
                name = args.get(index + 1).map(String::as_str);
                break;
            }
            arg if arg.starts_with('-') => index += 1,
            arg => {
                name = Some(arg);
                index += 1;
            }
        }
    }
    let Some(name) = name else {
        eprintln!("{usage}");
        return 2;
    };
    match create_inner(
        cli,
        name,
        email,
        args.iter().any(|s| s == "--yes" || s == "-y"),
        &home(),
    ) {
        Ok(message) => {
            println!("{message}");
            0
        }
        Err((message, code)) => {
            eprintln!("{message}");
            code
        }
    }
}
pub(super) fn sync() -> i32 {
    let home = home();
    let found = rows("claude", true);
    let mut drift = Vec::new();
    for row in &found {
        match mirror_sync(row.name.as_deref().unwrap_or(""), &home) {
            Ok(paths) => drift.extend(paths),
            Err(error) => {
                eprintln!("yelo: profile sync: {error}");
                return 1;
            }
        }
    }
    println!("synced {} Claude profiles", found.len());
    for path in &drift {
        println!("drift\t{}", path.display());
    }
    if drift.is_empty() { 0 } else { 1 }
}
