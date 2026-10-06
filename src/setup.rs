use crate::profile::{self, manage};
use serde::Serialize;
use std::{
    collections::BTreeSet,
    env, fs, io,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

#[derive(Serialize)]
struct SetupRow {
    step: &'static str,
    result: &'static str,
    detail: String,
}

#[derive(Serialize)]
struct DoctorRow {
    step: &'static str,
    state: &'static str,
    detail: String,
}

type Check = fn(&Path, bool) -> Result<(&'static str, String), (String, Option<PathBuf>)>;

fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}

pub(crate) fn dotfiles_root() -> PathBuf {
    fs::canonicalize(
        env::var("YELO_DOTFILES_ROOT")
            .ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| profile::home().join("dotfiles")),
    )
    .unwrap_or_else(|_| profile::home().join("dotfiles"))
}

pub(crate) fn dotfiles_owned(path: &Path) -> bool {
    path.is_symlink() && fs::canonicalize(path).is_ok_and(|p| p.starts_with(dotfiles_root()))
}

fn profile_path(home: &Path) -> PathBuf {
    home.join(".claude/.profiles")
}

fn profiles(home: &Path, apply: bool) -> Result<(&'static str, String), (String, Option<PathBuf>)> {
    let path = profile_path(home);
    if path.is_symlink() {
        return if dotfiles_owned(&path) {
            Ok((
                "owned-by-dotfiles",
                format!(
                    "symlink into {}: {}",
                    dotfiles_root().display(),
                    path.display()
                ),
            ))
        } else if apply {
            Err((
                "profile root is a symlink outside ~/dotfiles".into(),
                Some(path),
            ))
        } else {
            Ok((
                "missing",
                format!("symlink outside ~/dotfiles: {}", path.display()),
            ))
        };
    }
    if path.is_dir() {
        return Ok((
            if apply { "unchanged" } else { "ok" },
            path.display().to_string(),
        ));
    }
    if exists(&path) {
        return if apply {
            Err((
                "profile root exists and is not a directory".into(),
                Some(path),
            ))
        } else {
            Ok(("missing", format!("not a directory: {}", path.display())))
        };
    }
    if !apply {
        return Ok(("missing", format!("absent: {}", path.display())));
    }
    fs::create_dir_all(&path)
        .and_then(|_| fs::set_permissions(&path, fs::Permissions::from_mode(0o700)))
        .map_err(|e| (e.to_string(), Some(path.clone())))?;
    Ok(("changed", path.display().to_string()))
}

fn mirror_issues(name: &str, home: &Path) -> Vec<(String, PathBuf)> {
    let path = profile_path(home).join(name);
    if !path.is_dir() {
        return vec![("missing".into(), path)];
    }
    let names = manage::shared_names(home);
    let mut found = Vec::new();
    for item in &names {
        let entry = path.join(item);
        if !exists(&entry) {
            found.push(("missing".into(), entry));
        } else if entry.is_symlink() {
            if fs::read_link(&entry).ok() != Some(PathBuf::from("../..").join(item)) {
                found.push(("stale".into(), entry));
            }
        } else {
            found.push(("drift".into(), entry));
        }
    }
    for entry in profile::dirs(&path) {
        let Some(item) = entry.file_name().and_then(|s| s.to_str()) else {
            continue;
        };
        if names.iter().any(|name| name == item) {
            continue;
        }
        if fs::read_link(&entry).ok() == Some(PathBuf::from("../..").join(item)) {
            found.push(("stale".into(), entry));
        } else if !manage::excluded(item) {
            found.push(("drift".into(), entry));
        }
    }
    let config = path.join(".claude.json");
    if !exists(&config) {
        found.push(("missing".into(), config));
    } else if config.is_symlink() || !config.is_file() {
        found.push(("drift".into(), config));
    }
    found
}

fn mirrors(home: &Path, apply: bool) -> Result<(&'static str, String), (String, Option<PathBuf>)> {
    let rows = profile::rows("claude", false);
    let before: BTreeSet<_> = rows
        .iter()
        .flat_map(|r| mirror_issues(r.name.as_deref().unwrap_or(""), home))
        .collect();
    if apply {
        for row in &rows {
            manage::mirror_sync(row.name.as_deref().unwrap_or(""), home)
                .map_err(|e| (e.to_string(), None))?;
        }
    }
    let after: BTreeSet<_> = rows
        .iter()
        .flat_map(|r| mirror_issues(r.name.as_deref().unwrap_or(""), home))
        .collect();
    if apply {
        let detail = format!(
            "{} Claude profile mirrors{}",
            rows.len(),
            if after.is_empty() {
                String::new()
            } else {
                format!(
                    "; {}",
                    after
                        .iter()
                        .map(|(kind, path)| format!("{kind}: {}", path.display()))
                        .collect::<Vec<_>>()
                        .join("; ")
                )
            }
        );
        Ok((
            if before == after {
                "unchanged"
            } else {
                "changed"
            },
            detail,
        ))
    } else if after.is_empty() {
        Ok((
            "ok",
            format!(
                "{} Claude profile mirrors under {}",
                rows.len(),
                profile_path(home).display()
            ),
        ))
    } else {
        Ok((
            "missing",
            after
                .iter()
                .map(|(kind, path)| format!("{kind}: {}", path.display()))
                .collect::<Vec<_>>()
                .join("; "),
        ))
    }
}

fn bin_dir(home: &Path) -> PathBuf {
    home.join(".local/bin")
}

fn wanted_launchers(home: &Path) -> Vec<(PathBuf, String)> {
    let mut wanted = Vec::new();
    for cli in ["claude", "codex"] {
        for row in profile::rows(cli, false) {
            if let Some(name) = row.name.as_deref().filter(|n| profile::valid_name(n)) {
                wanted.push((
                    bin_dir(home).join(format!("{cli}-{name}")),
                    manage::launcher(cli, name, Path::new(&row.dir), home),
                ));
            }
        }
    }
    wanted
}

fn launcher_state(path: &Path, body: &str) -> &'static str {
    if !exists(path) {
        "changed"
    } else if !manage::owned_launcher(path) {
        "kept"
    } else if fs::read_to_string(path).ok().as_deref() == Some(body)
        && fs::metadata(path).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
    {
        "unchanged"
    } else {
        "changed"
    }
}

fn atomic_write(path: &Path, body: &[u8], mode: u32) -> io::Result<()> {
    let parent = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let temporary = parent.join(format!(
        ".{}.{}",
        path.file_name().unwrap_or_default().to_string_lossy(),
        std::process::id()
    ));
    fs::write(&temporary, body)?;
    fs::set_permissions(&temporary, fs::Permissions::from_mode(mode))?;
    fs::rename(temporary, path)
}

fn orphans(home: &Path, wanted: &[(PathBuf, String)]) -> Vec<PathBuf> {
    profile::dirs(&bin_dir(home))
        .into_iter()
        .filter(|path| {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            (name.starts_with("claude-") || name.starts_with("codex-"))
                && !wanted.iter().any(|(p, _)| p == path)
                && manage::owned_launcher(path)
        })
        .collect()
}

pub(crate) fn config_dir(home: &Path) -> PathBuf {
    env::var("XDG_CONFIG_HOME")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".config"))
        .join("yelo")
}
fn shell_path(home: &Path) -> PathBuf {
    config_dir(home).join("shell.sh")
}

fn shell_text() -> &'static str {
    include_str!("profile_shell.zsh")
}

fn old_shell() -> String {
    let mut body = "# Installed by jello simple setup\n".to_string();
    for cli in ["claude", "codex"] {
        body.push_str(&format!(r#"if ! alias {cli} >/dev/null 2>&1 &&
   {{ ! typeset -f {cli} >/dev/null 2>&1 ||
      [[ "$(typeset -f {cli})" == *"jello-agent --shell {cli}"* ]]; }}; then
    eval '{cli}() {{
    local jello_launch
    jello_launch="$(command jello-agent --shell {cli} "$@")" || return
    ( eval "$jello_launch" )
}}'
else
    printf '%s\n' 'jello: kept existing {cli} alias/function. Use jello-agent {cli} alongside it, or eval "$(jello shell-init --replace)" to save and replace this shell definition.' >&2
fi
"#));
    }
    body
}

fn standalone_shell() -> &'static str {
    include_str!("standalone_shell.zsh")
}

fn shell_owned(path: &Path) -> bool {
    if path.is_symlink() {
        return false;
    }
    fs::read_to_string(path).is_ok_and(|body| {
        body.starts_with("# Installed by yelo profile integration.\n")
            || body == old_shell()
            || body == standalone_shell()
    })
}

fn source_line(home: &Path) -> String {
    let path = shell_path(home);
    if path == home.join(".config/yelo/shell.sh") {
        "source \"$HOME/.config/yelo/shell.sh\"".into()
    } else {
        let raw = path.to_string_lossy();
        let quoted = if raw
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"_@%+=:,./-".contains(&b))
        {
            raw.into_owned()
        } else {
            format!("'{}'", raw.replace('\'', "'\"'\"'"))
        };
        format!("source {quoted}")
    }
}

fn integration_missing(home: &Path) -> Vec<PathBuf> {
    let shell = shell_path(home);
    let mut missing = Vec::new();
    if shell.is_symlink() || fs::read_to_string(&shell).ok().as_deref() != Some(shell_text()) {
        missing.push(shell);
    }
    let rc = env::var("ZDOTDIR")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.to_owned())
        .join(".zshrc");
    if !fs::read_to_string(&rc)
        .unwrap_or_default()
        .lines()
        .any(|s| s == source_line(home))
    {
        missing.push(rc);
    }
    missing
}

fn apply_integration(home: &Path) -> Result<(Vec<PathBuf>, String), (String, Option<PathBuf>)> {
    let shell = shell_path(home);
    if exists(&shell) && !shell_owned(&shell) {
        return Err((
            "profile integration target belongs to another owner".into(),
            Some(shell),
        ));
    }
    let mut changed = Vec::new();
    let archive = shell.with_file_name("profiles.pyz");
    if exists(&archive)
        && !archive.is_symlink()
        && fs::read(&archive).is_ok_and(|b| {
            b.starts_with(b"#!/usr/bin/env python3\n# Installed by yelo profile integration.\n")
        })
    {
        fs::remove_file(&archive).map_err(|e| (e.to_string(), Some(archive.clone())))?;
        changed.push(archive);
    }
    let current = fs::read_to_string(&shell).ok();
    if current.as_deref() != Some(shell_text()) {
        if let Some(body) = current {
            let backup = PathBuf::from(format!("{}.before-profile-integration", shell.display()));
            if !exists(&backup) {
                fs::write(&backup, body).map_err(|e| (e.to_string(), Some(backup)))?;
            }
        }
        atomic_write(&shell, shell_text().as_bytes(), 0o755)
            .map_err(|e| (e.to_string(), Some(shell.clone())))?;
        changed.push(shell.clone());
    }
    let rc = env::var("ZDOTDIR")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| home.to_owned())
        .join(".zshrc");
    let line = source_line(home);
    let body = fs::read_to_string(&rc).unwrap_or_default();
    if !body.lines().any(|s| s == line) {
        if rc.is_symlink() {
            return Ok((changed, format!("add to {}: {line}", rc.display())));
        }
        let mut file = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&rc)
            .map_err(|e| (e.to_string(), Some(rc.clone())))?;
        use std::io::Write;
        writeln!(file, "\n{line}").map_err(|e| (e.to_string(), Some(rc.clone())))?;
        changed.push(rc);
    }
    Ok((
        changed,
        "profile menu and automatic selection installed; open a new terminal".into(),
    ))
}

fn launchers(
    home: &Path,
    apply: bool,
) -> Result<(&'static str, String), (String, Option<PathBuf>)> {
    let (integration_changes, integration_detail) = if apply {
        apply_integration(home)?
    } else {
        (Vec::new(), String::new())
    };
    let wanted = wanted_launchers(home);
    let mut written = Vec::new();
    let mut absent = Vec::new();
    let mut stale = Vec::new();
    let mut kept = Vec::new();
    for (path, body) in &wanted {
        match launcher_state(path, body) {
            "kept" => kept.push(format!("kept, not a yelo launcher: {}", path.display())),
            "changed" if apply => {
                atomic_write(path, body.as_bytes(), 0o755)
                    .map_err(|e| (e.to_string(), Some(path.clone())))?;
                written.push(path.clone());
            }
            "changed" if exists(path) => stale.push(path.clone()),
            "changed" => absent.push(path.clone()),
            _ => {}
        }
    }
    let mut removed = Vec::new();
    for path in orphans(home, &wanted) {
        if apply {
            fs::remove_file(&path).map_err(|e| (e.to_string(), Some(path.clone())))?;
            removed.push(format!("removed, its account is gone: {}", path.display()));
        } else {
            stale.push(path);
        }
    }
    if apply {
        let head = if wanted.is_empty() {
            format!(
                "no account has a launcher to write: {}",
                bin_dir(home).display()
            )
        } else if written.is_empty() {
            format!(
                "{} launchers under {}",
                wanted.len(),
                bin_dir(home).display()
            )
        } else {
            format!(
                "wrote {} of {} launchers under {}",
                written.len(),
                wanted.len(),
                bin_dir(home).display()
            )
        };
        let detail = [vec![head], removed, kept, vec![integration_detail]]
            .concat()
            .join("; ");
        Ok((
            if written.is_empty() && integration_changes.is_empty() && !detail.contains("removed,")
            {
                "unchanged"
            } else {
                "changed"
            },
            detail,
        ))
    } else {
        stale.extend(integration_missing(home));
        if !absent.is_empty() || !stale.is_empty() {
            stale.sort();
            let mut details: Vec<_> = absent
                .iter()
                .map(|p| format!("absent: {}", p.display()))
                .collect();
            details.extend(stale.iter().map(|p| format!("stale: {}", p.display())));
            details.extend(kept);
            Ok(("missing", details.join("; ")))
        } else {
            let detail = format!(
                "{} launchers under {}",
                wanted.len() - kept.len(),
                bin_dir(home).display()
            );
            Ok(("ok", [vec![detail], kept].concat().join("; ")))
        }
    }
}

fn error(command: &str, message: &str, path: Option<&Path>) -> i32 {
    if let Some(path) = path {
        eprintln!("yelo: {command}: {message} ({})", path.display());
    } else {
        eprintln!("yelo: {command}: {message}");
    }
    1
}

pub fn run(args: &[String]) -> i32 {
    let selected: Vec<_> = args
        .iter()
        .skip(1)
        .filter(|s| s.as_str() != "--json")
        .collect();
    let mut unknown: Vec<_> = selected
        .iter()
        .filter(|s| !matches!(s.as_str(), "launchers" | "profiles" | "profile-mirrors"))
        .map(|s| s.as_str())
        .collect();
    unknown.sort();
    unknown.dedup();
    if !unknown.is_empty() {
        return error(
            "setup",
            &format!("unknown step: {}", unknown.join(", ")),
            None,
        );
    }
    let home = profile::home();
    let mut rows = Vec::new();
    for (name, check) in [
        ("launchers", launchers as Check),
        ("profiles", profiles as Check),
        ("profile-mirrors", mirrors as Check),
    ] {
        if !selected.is_empty() && !selected.iter().any(|s| s.as_str() == name) {
            continue;
        }
        match check(&home, true) {
            Ok((result, detail)) => rows.push(SetupRow {
                step: name,
                result,
                detail,
            }),
            Err((message, path)) => return error("setup", &message, path.as_deref()),
        }
    }
    if args.iter().any(|s| s == "--json") {
        println!("{}", serde_json::to_string_pretty(&rows).unwrap());
    } else {
        for row in rows {
            println!("{}\t{}\t{}", row.step, row.result, row.detail);
        }
    }
    0
}

pub fn doctor(args: &[String]) -> i32 {
    let home = profile::home();
    let mut rows = Vec::new();
    for (name, check) in [
        ("launchers", launchers as Check),
        ("profiles", profiles as Check),
        ("profile-mirrors", mirrors as Check),
        ("usage", crate::hud::check_usage as Check),
        ("hud", crate::hud::check_hud as Check),
    ] {
        match check(&home, false) {
            Ok((state, detail)) => rows.push(DoctorRow {
                step: name,
                state,
                detail,
            }),
            Err((message, path)) => return error("doctor", &message, path.as_deref()),
        }
    }
    let missing = rows.iter().any(|row| row.state == "missing");
    if args.iter().any(|s| s == "--json") {
        println!("{}", serde_json::to_string_pretty(&rows).unwrap());
    } else {
        for row in rows {
            println!("{}\t{}\t{}", row.step, row.state, row.detail);
        }
    }
    if missing { 1 } else { 0 }
}
