use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
mod manage;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

#[derive(Clone, Serialize)]
struct Row {
    name: Option<String>,
    dir: String,
    email: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    plan: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    signed_in: Option<bool>,
    aliases: Vec<String>,
}

fn home() -> PathBuf {
    PathBuf::from(env::var("HOME").unwrap_or_default())
}
fn claude_root() -> PathBuf {
    env::var("AGENT_PROFILES_CLAUDE_ROOT")
        .ok()
        .filter(|s| !s.is_empty())
        .map_or_else(|| home().join(".claude/.profiles"), PathBuf::from)
}
fn codex_root() -> PathBuf {
    env::var("AGENT_PROFILES_CODEX_GLOB_ROOT")
        .ok()
        .filter(|s| !s.is_empty())
        .map_or_else(home, PathBuf::from)
}
fn valid_name(name: &str) -> bool {
    let mut chars = name.chars();
    chars.next().is_some_and(|c| c.is_ascii_alphanumeric())
        && chars.all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
}
fn emittable(path: &Path) -> bool {
    !path.to_string_lossy().contains(['\t', '\n'])
}
fn first_line(path: &Path) -> Option<String> {
    fs::read_to_string(path)
        .ok()?
        .lines()
        .next()
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
}
fn read_json(path: &Path) -> Option<Value> {
    serde_json::from_slice(&fs::read(path).ok()?).ok()
}
fn dirs(path: &Path) -> Vec<PathBuf> {
    let mut entries: Vec<_> = fs::read_dir(path)
        .into_iter()
        .flatten()
        .flatten()
        .map(|e| e.path())
        .collect();
    entries.sort();
    entries
}
fn aliases(root: &Path) -> Vec<(String, String)> {
    fs::read_to_string(root.join(".aliases"))
        .unwrap_or_default()
        .lines()
        .filter_map(|line| {
            let (old, new) = line.split('#').next()?.trim().split_once('=')?;
            let (old, new) = (old.trim(), new.trim());
            (!old.is_empty() && !new.is_empty()).then(|| (old.to_owned(), new.to_owned()))
        })
        .collect()
}
fn claude_email(path: &Path) -> Option<String> {
    read_json(&path.join(".claude.json"))
        .and_then(|v| {
            v.pointer("/oauthAccount/emailAddress")?
                .as_str()
                .map(str::to_owned)
        })
        .filter(|s| !s.is_empty())
        .or_else(|| first_line(&path.join("email")))
}
fn decode_base64url(text: &str) -> Option<Vec<u8>> {
    let mut out = Vec::new();
    let mut bits = 0_u32;
    let mut count = 0_u8;
    for byte in text.bytes() {
        let value = match byte {
            b'A'..=b'Z' => byte - b'A',
            b'a'..=b'z' => byte - b'a' + 26,
            b'0'..=b'9' => byte - b'0' + 52,
            b'-' => 62,
            b'_' => 63,
            b'=' => break,
            _ => return None,
        };
        bits = (bits << 6) | u32::from(value);
        count += 6;
        if count >= 8 {
            count -= 8;
            out.push((bits >> count) as u8);
            bits &= (1 << count) - 1;
        }
    }
    Some(out)
}
fn codex_identity(path: &Path) -> (Option<String>, Option<String>, bool) {
    let Some(auth) = read_json(&path.join("auth.json")) else {
        return (None, None, false);
    };
    let tokens = auth.get("tokens");
    let signed_in = ["access_token", "refresh_token"].iter().any(|key| {
        tokens
            .and_then(|t| t.get(*key))
            .and_then(Value::as_str)
            .is_some_and(|s| !s.is_empty())
    }) || auth
        .get("OPENAI_API_KEY")
        .and_then(Value::as_str)
        .is_some_and(|s| !s.is_empty());
    let payload = tokens
        .and_then(|t| t.get("id_token"))
        .and_then(Value::as_str)
        .and_then(|s| s.split('.').nth(1))
        .and_then(decode_base64url)
        .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
    let email = payload
        .as_ref()
        .and_then(|p| p.get("email"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    let plan = payload
        .as_ref()
        .and_then(|p| p.pointer("/https:~1~1api.openai.com~1auth/chatgpt_plan_type"))
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    (email, plan, signed_in)
}
fn rows(cli: &str, identity: bool) -> Vec<Row> {
    if cli == "claude" {
        let root = claude_root();
        let alias_map = aliases(&root);
        return dirs(&root)
            .into_iter()
            .filter_map(|path| {
                let name = path.file_name()?.to_str()?.to_owned();
                if !valid_name(&name) || !path.is_dir() || !emittable(&path) {
                    return None;
                }
                let mut aliases: Vec<_> = alias_map
                    .iter()
                    .filter(|(_, target)| target == &name)
                    .map(|(old, _)| old.clone())
                    .collect();
                aliases.sort();
                Some(Row {
                    name: Some(name),
                    dir: path.to_string_lossy().into_owned(),
                    email: identity.then(|| claude_email(&path)).flatten(),
                    plan: None,
                    signed_in: None,
                    aliases,
                })
            })
            .collect();
    }
    let root = codex_root();
    let mut paths = Vec::new();
    let base = root.join(".codex");
    if base.is_dir() && emittable(&base) {
        paths.push((base.clone(), first_line(&base.join("profile-label"))));
    }
    paths.extend(dirs(&root).into_iter().filter_map(|path| {
        let name = path
            .file_name()?
            .to_str()?
            .strip_prefix(".codex-")?
            .to_owned();
        (valid_name(&name) && path.is_dir() && emittable(&path)).then_some((path, Some(name)))
    }));
    paths
        .into_iter()
        .map(|(path, name)| {
            let (email, plan, signed_in) = if identity {
                codex_identity(&path)
            } else {
                (None, None, false)
            };
            Row {
                name: name.filter(|s| valid_name(s)),
                dir: path.to_string_lossy().into_owned(),
                email,
                plan,
                signed_in: identity.then_some(signed_in),
                aliases: Vec::new(),
            }
        })
        .collect()
}
fn add_signed_in(cli: &str, rows: &mut [Row]) {
    if cli == "claude" {
        for row in rows {
            let security =
                env::var("AGENT_PROFILES_SECURITY_BIN").unwrap_or_else(|_| "security".into());
            let user = env::var("USER").unwrap_or_default();
            let service = keychain_service(&home(), row.name.as_deref().unwrap_or_default());
            row.signed_in = Some(
                Command::new(&security)
                    .args(["find-generic-password", "-s", &service, "-a", &user])
                    .output()
                    .is_ok_and(|o| o.status.success()),
            );
        }
    }
}
fn keychain_service(home: &Path, name: &str) -> String {
    let identity = home.join(format!(".claude-{name}"));
    let hash = Sha256::digest(identity.to_string_lossy().as_bytes());
    format!(
        "Claude Code-credentials-{:02x}{:02x}{:02x}{:02x}",
        hash[0], hash[1], hash[2], hash[3]
    )
}

fn render(header: Vec<&str>, body: Vec<Vec<String>>) -> String {
    let widths: Vec<_> = (0..header.len())
        .map(|i| {
            body.iter()
                .map(|r| r[i].chars().count())
                .max()
                .unwrap_or(0)
                .max(header[i].chars().count())
        })
        .collect();
    let mut out = String::new();
    for row in std::iter::once(header.iter().map(|s| (*s).to_owned()).collect()).chain(body) {
        let line = row
            .iter()
            .enumerate()
            .map(|(i, cell)| format!("{cell:width$}", width = widths[i]))
            .collect::<Vec<_>>()
            .join("  ");
        out.push_str(line.trim_end());
        out.push('\n');
    }
    out
}
fn command_list(cli: &str, json: bool) -> i32 {
    let mut found = rows(cli, true);
    add_signed_in(cli, &mut found);
    if json {
        println!("{}", serde_json::to_string(&found).unwrap());
        return 0;
    }
    if found.is_empty() {
        eprintln!("{cli}: no profiles found");
        return 1;
    }
    let mut header = vec!["NAME", "EMAIL"];
    if cli == "codex" {
        header.push("PLAN");
    }
    let body = found
        .iter()
        .map(|r| {
            let mut cells = vec![
                r.name.as_deref().unwrap_or("-").to_owned(),
                r.email.as_deref().unwrap_or("-").to_owned(),
            ];
            if cli == "codex" {
                cells.push(r.plan.as_deref().unwrap_or("-").to_owned());
            }
            cells
        })
        .collect();
    print!("{}", render(header, body));
    0
}
fn emit_row(row: &Row, json: bool) {
    if json {
        println!("{}", serde_json::to_string(row).unwrap());
    } else {
        println!("{}\t{}", row.name.as_deref().unwrap_or(""), row.dir);
    }
}
fn resolve(cli: &str, query: &str) -> Result<Row, (String, i32, Vec<Row>)> {
    let query = query.trim();
    if query.is_empty() {
        return Err((format!("{cli}: empty profile name"), 2, Vec::new()));
    }
    let found = rows(cli, true);
    let select =
        |matches: Vec<Row>, reason: &str| -> Result<Option<Row>, (String, i32, Vec<Row>)> {
            if matches.len() == 1 {
                Ok(matches.into_iter().next())
            } else if matches.is_empty() {
                Ok(None)
            } else {
                Err((format!("{cli}: '{query}' matches {reason}"), 2, matches))
            }
        };
    if let Some(row) = select(
        found
            .iter()
            .filter(|r| r.name.as_deref() == Some(query))
            .cloned()
            .collect(),
        "several profiles by name",
    )? {
        return Ok(row);
    }
    if cli == "claude"
        && let Some(target) = aliases(&claude_root())
            .iter()
            .rev()
            .find(|(old, _)| old == query)
            .map(|(_, new)| new)
        && let Some(row) = select(
            found
                .iter()
                .filter(|r| r.name.as_deref() == Some(target.as_str()))
                .cloned()
                .collect(),
            "several profiles by alias",
        )?
    {
        return Ok(row);
    }
    let lower = query.to_lowercase();
    if let Some(row) = select(
        found
            .iter()
            .filter(|r| {
                r.email
                    .as_deref()
                    .is_some_and(|s| s.to_lowercase() == lower)
            })
            .cloned()
            .collect(),
        "several profiles by email",
    )? {
        return Ok(row);
    }
    if let Some(row) = select(
        found
            .iter()
            .filter(|r| {
                r.name
                    .as_deref()
                    .is_some_and(|s| s.to_lowercase().contains(&lower))
                    || r.email
                        .as_deref()
                        .is_some_and(|s| s.to_lowercase().contains(&lower))
            })
            .cloned()
            .collect(),
        "several profiles",
    )? {
        return Ok(row);
    }
    Err((
        format!("{cli}: no profile matches '{query}'"),
        1,
        Vec::new(),
    ))
}
fn command_resolve(cli: &str, query: &str, json: bool) -> i32 {
    match resolve(cli, query) {
        Ok(row) => {
            emit_row(&row, json);
            0
        }
        Err((message, code, candidates)) => {
            eprintln!("{message}");
            for row in candidates {
                eprintln!(
                    "  {}\t{}",
                    row.name.as_deref().unwrap_or("-"),
                    row.email.as_deref().unwrap_or("-")
                );
            }
            code
        }
    }
}
fn command_menu(cli: &str) -> i32 {
    let mut found = rows(cli, true);
    if found.is_empty() {
        eprintln!("{cli}: no profiles found");
        return 1;
    }
    add_signed_in(cli, &mut found);
    let mut body = Vec::new();
    for row in &found {
        let mut cells = vec![
            row.name.as_deref().unwrap_or("-").to_owned(),
            row.email.as_deref().unwrap_or("-").to_owned(),
        ];
        if cli == "codex" {
            cells.push(row.plan.as_deref().unwrap_or("-").to_owned());
        }
        cells.push(
            if row.signed_in == Some(false) {
                "not signed in"
            } else {
                "no data"
            }
            .to_owned(),
        );
        body.push(cells);
    }
    let blank = vec![""; body[0].len()];
    let formatted = render(blank, body);
    for (index, (row, line)) in found.iter().zip(formatted.lines().skip(1)).enumerate() {
        println!(
            "{}\t{}\t{}\t{}",
            index + 1,
            row.name.as_deref().unwrap_or(""),
            row.dir,
            line
        );
    }
    0
}
fn command_pick(cli: &str, json: bool) -> i32 {
    let mut found = rows(cli, true);
    add_signed_in(cli, &mut found);
    found.retain(|row| row.signed_in == Some(true));
    match found.as_slice() {
        [] => {
            eprintln!("{cli}: no signed-in account to pick from");
            1
        }
        [row] => {
            emit_row(row, json);
            0
        }
        _ => {
            eprintln!("{cli}: no usage data to pick an account from");
            1
        }
    }
}
fn rollout_files(directory: &Path, archived: bool) -> Vec<(String, String, PathBuf)> {
    fn collect(path: &Path, depth: usize, out: &mut Vec<PathBuf>) {
        if depth == 0 {
            out.extend(dirs(path).into_iter().filter(|p| p.is_file()));
        } else {
            for child in dirs(path).into_iter().filter(|p| p.is_dir()) {
                collect(&child, depth - 1, out);
            }
        }
    }
    let mut paths = Vec::new();
    collect(&directory.join("sessions"), 3, &mut paths);
    if archived {
        fn archived_files(path: &Path, out: &mut Vec<PathBuf>) {
            for child in dirs(path) {
                if child.is_dir() {
                    archived_files(&child, out);
                } else if child.is_file() {
                    out.push(child);
                }
            }
        }
        archived_files(&directory.join("archived_sessions"), &mut paths);
    }
    paths
        .into_iter()
        .filter_map(|path| {
            let name = path.file_name()?.to_str()?;
            let rest = name.strip_prefix("rollout-")?.strip_suffix(".jsonl")?;
            let (started, id) = rest.split_at_checked(19)?;
            let id = id.strip_prefix('-')?;
            let valid_time = started.as_bytes().iter().enumerate().all(|(i, b)| {
                if [4, 7, 10, 13, 16].contains(&i) {
                    *b == if i == 10 { b'T' } else { b'-' }
                } else {
                    b.is_ascii_digit()
                }
            });
            let valid_id = id.len() == 36
                && id.as_bytes().iter().enumerate().all(|(i, b)| {
                    if [8, 13, 18, 23].contains(&i) {
                        *b == b'-'
                    } else {
                        b.is_ascii_hexdigit()
                    }
                });
            if valid_time && valid_id {
                let started = started.to_owned();
                let id = id.to_lowercase();
                Some((started, id, path))
            } else {
                None
            }
        })
        .collect()
}
fn session_rows(limit: usize, everywhere: bool, cwd: &Path) -> Vec<Value> {
    let mut candidates = Vec::new();
    for profile in rows("codex", true) {
        for (started, id, path) in rollout_files(Path::new(&profile.dir), false) {
            candidates.push((started, id, path, profile.clone()));
        }
    }
    candidates.sort_by(|a, b| b.0.cmp(&a.0));
    let mut output = Vec::new();
    for (started, id, path, profile) in candidates {
        if output.len() >= limit {
            break;
        }
        let meta = first_line(&path).and_then(|line| serde_json::from_str::<Value>(&line).ok());
        let Some(meta) =
            meta.filter(|m| m.get("type").and_then(Value::as_str) == Some("session_meta"))
        else {
            continue;
        };
        let Some(session_cwd) = meta.pointer("/payload/cwd").and_then(Value::as_str) else {
            continue;
        };
        if !everywhere && fs::canonicalize(session_cwd).ok() != fs::canonicalize(cwd).ok() {
            continue;
        }
        let session_id = meta
            .pointer("/payload/id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or(&id);
        output.push(
            serde_json::json!({"profile": profile.name, "dir": profile.dir,
            "id": session_id, "path": path, "cwd": session_cwd, "started": started}),
        );
    }
    output
}
fn home_relative(path: &str) -> String {
    let home = home();
    let home = home.to_string_lossy();
    if path == home {
        "~".to_owned()
    } else if path.starts_with(home.as_ref()) && path.as_bytes().get(home.len()) == Some(&b'/') {
        format!("~{}", &path[home.len()..])
    } else {
        path.to_owned()
    }
}
fn command_sessions(args: &[String], json: bool) -> i32 {
    let limit = args
        .windows(2)
        .find(|p| p[0] == "--limit")
        .and_then(|p| p[1].parse::<i32>().ok())
        .unwrap_or(20);
    if limit <= 0 {
        eprintln!("codex: --limit must be a positive number");
        return 2;
    }
    let everywhere = args.iter().any(|a| a == "--all");
    let cwd = args
        .windows(2)
        .find(|p| p[0] == "--cwd")
        .map(|p| PathBuf::from(&p[1]))
        .unwrap_or_else(|| env::current_dir().unwrap_or_default());
    let sessions = session_rows(limit as usize, everywhere, &cwd);
    if json {
        println!("{}", serde_json::to_string(&sessions).unwrap());
        return 0;
    }
    if sessions.is_empty() {
        let scope = if everywhere {
            "any account".to_owned()
        } else {
            format!("any account for {}", home_relative(&cwd.to_string_lossy()))
        };
        eprintln!("codex: no sessions found in {scope}");
        return 1;
    }
    let mut header = vec!["PROFILE", "WHEN", "ID"];
    if everywhere {
        header.insert(2, "CWD");
    }
    let body = sessions
        .iter()
        .map(|s| {
            let started = s["started"].as_str().unwrap_or("");
            let mut line = vec![
                s["profile"].as_str().unwrap_or("-").to_owned(),
                format!(
                    "{} {}:{}",
                    &started[..10],
                    &started[11..13],
                    &started[14..16]
                ),
                s["id"].as_str().unwrap_or("").to_owned(),
            ];
            if everywhere {
                line.insert(2, home_relative(s["cwd"].as_str().unwrap_or("")));
            }
            line
        })
        .collect();
    print!("{}", render(header, body));
    0
}
fn command_owner(args: &[String], json: bool) -> i32 {
    let owner = if args.iter().any(|a| a == "--last") {
        let cwd = env::current_dir().unwrap_or_default();
        session_rows(1, args.iter().any(|a| a == "--all"), &cwd)
            .first()
            .cloned()
            .map(|s| {
                (
                    s["profile"].as_str().map(str::to_owned),
                    s["dir"].as_str().unwrap_or("").to_owned(),
                )
            })
    } else {
        let query = args.last().map(String::as_str).unwrap_or("");
        let mut found = None;
        for profile in rows("codex", false) {
            if rollout_files(Path::new(&profile.dir), true)
                .iter()
                .any(|(_, id, _)| id == &query.to_lowercase())
            {
                found = Some((profile.name, profile.dir));
                break;
            }
        }
        if found.is_none() {
            eprintln!("codex: session {query} is not in any account");
            return 1;
        }
        found
    };
    let Some((name, dir)) = owner else {
        eprintln!("codex: no sessions found in any account");
        return 1;
    };
    if json {
        println!("{}", serde_json::json!({"name": name, "dir": dir}));
    } else {
        println!("{}\t{}", name.as_deref().unwrap_or(""), dir);
    }
    0
}
pub fn run(args: &[String]) -> i32 {
    if args.first().map(String::as_str) != Some("profile") {
        eprintln!(
            "yelo: {}: not ported yet",
            args.first().map_or("", String::as_str)
        );
        return 2;
    }
    let action = args.get(1).map(String::as_str).unwrap_or("");
    let cli = args
        .windows(2)
        .find(|p| p[0] == "--cli")
        .map(|p| p[1].as_str())
        .unwrap_or("");
    let json = args.iter().any(|a| a == "--json");
    match action {
        "list" => command_list(cli, json),
        "resolve" => command_resolve(cli, args.last().map(String::as_str).unwrap_or(""), json),
        "menu" => command_menu(cli),
        "pick" => command_pick(cli, json),
        "sessions" => command_sessions(args, json),
        "owner" => command_owner(args, json),
        "create" => manage::create(cli, args),
        "sync" => manage::sync(),
        _ => {
            eprintln!("yelo: profile {action}: not ported yet");
            2
        }
    }
}

#[cfg(test)]
mod tests {
    use super::keychain_service;
    use std::path::Path;

    #[test]
    fn keychain_service_matches_python() {
        assert_eq!(
            keychain_service(Path::new("/h"), "pri"),
            "Claude Code-credentials-d36dbee4"
        );
    }
}
