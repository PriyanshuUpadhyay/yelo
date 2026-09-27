#![allow(dead_code)]

use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::sync::{
    OnceLock,
    atomic::{AtomicU32, Ordering},
};
use std::time::{SystemTime, UNIX_EPOCH};

pub const FALSE_BIN: &str = "/usr/bin/false";
pub const TRUE_BIN: &str = "/usr/bin/true";
const BLOCKED: [&str; 6] = ["herdr", "swift", "claude", "codex", "agy", "node"];
static NEXT_HOME: AtomicU32 = AtomicU32::new(0);

pub struct TestHome {
    pub root: PathBuf,
    pub home: PathBuf,
}

impl TestHome {
    pub fn new() -> Self {
        for _ in 0..1000 {
            let tick = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .subsec_nanos();
            let suffix = tick ^ std::process::id() ^ NEXT_HOME.fetch_add(1, Ordering::Relaxed);
            let root = PathBuf::from(format!("/tmp/yelo-{suffix:08x}"));
            if fs::create_dir(&root).is_ok() {
                set_mode(&root, 0o755);
                return Self {
                    home: root.join("home"),
                    root,
                };
            }
        }
        panic!("could not create a unique test home");
    }
}

impl Drop for TestHome {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

pub fn set_mode(path: &Path, mode: u32) {
    fs::set_permissions(path, fs::Permissions::from_mode(mode)).unwrap();
}

pub fn mkdir(path: &Path) {
    fs::create_dir_all(path).unwrap();
    // The fixture modes are part of the golden contract, even under umask 002.
    let mut current = path;
    while current.starts_with("/tmp") && current != Path::new("/tmp") {
        set_mode(current, 0o755);
        current = current.parent().unwrap();
    }
}

pub fn write(path: &Path, text: &str) {
    mkdir(path.parent().unwrap());
    fs::write(path, text).unwrap();
    set_mode(path, 0o644);
}

pub fn write_json(path: &Path, value: &Value) {
    write(path, &(python_json(value) + "\n"));
}

pub fn set_age(path: &Path, seconds: u64) {
    let stamp = SystemTime::now() - std::time::Duration::from_secs(seconds);
    let file = fs::File::options().write(true).open(path).unwrap();
    file.set_times(fs::FileTimes::new().set_accessed(stamp).set_modified(stamp))
        .unwrap();
}

pub fn mtime_ns(path: &Path) -> u128 {
    fs::metadata(path)
        .unwrap()
        .modified()
        .unwrap()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos()
}

// Python's json.dumps spacing is part of the recorded HOME file hashes.
pub fn python_json(value: &Value) -> String {
    let compact = serde_json::to_string(value).unwrap();
    let mut out = String::with_capacity(compact.len() + 16);
    let mut quoted = false;
    let mut escaped = false;
    for ch in compact.chars() {
        if quoted {
            out.push(ch);
            if escaped {
                escaped = false;
            } else if ch == '\\' {
                escaped = true;
            } else if ch == '"' {
                quoted = false;
            }
        } else {
            out.push(ch);
            if ch == '"' {
                quoted = true;
            }
            if ch == ',' || ch == ':' {
                out.push(' ');
            }
        }
    }
    out
}

fn jwt(payload: Value) -> String {
    fn segment(value: &Value) -> String {
        let bytes = python_json(value).into_bytes();
        const ALPHABET: &[u8; 64] =
            b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
        let mut out = String::new();
        for chunk in bytes.chunks(3) {
            let bits = ((chunk[0] as u32) << 16)
                | ((chunk.get(1).copied().unwrap_or(0) as u32) << 8)
                | chunk.get(2).copied().unwrap_or(0) as u32;
            out.push(ALPHABET[((bits >> 18) & 63) as usize] as char);
            out.push(ALPHABET[((bits >> 12) & 63) as usize] as char);
            if chunk.len() > 1 {
                out.push(ALPHABET[((bits >> 6) & 63) as usize] as char);
            }
            if chunk.len() > 2 {
                out.push(ALPHABET[(bits & 63) as usize] as char);
            }
        }
        out
    }
    format!(
        "{}.{}.signature",
        segment(&json!({"alg": "none"})),
        segment(&payload)
    )
}

pub fn codex_auth(email: &str, plan: &str, account_id: &str) -> Value {
    json!({"tokens": {
        "id_token": jwt(json!({"email": email, "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan, "chatgpt_account_id": account_id
        }})),
        "access_token": format!("access-{account_id}"), "account_id": account_id
    }})
}

pub fn build_fixture_home(home: &Path) {
    write(&home.join(".claude.json"), "{\"oauthAccount\":null}\n");
    for (name, email) in [("pri", "pri@example.test"), ("work", "work@example.test")] {
        write(
            &home.join(format!(".claude/.profiles/{name}/email")),
            &format!("{email}\n"),
        );
    }
    for (name, email, plan, id) in [
        ("base", "base@example.test", "pro", "acc-base"),
        ("alt", "alt@example.test", "plus", "acc-alt"),
    ] {
        let dir = if name == "base" {
            home.join(".codex")
        } else {
            home.join(format!(".codex-{name}"))
        };
        mkdir(&dir.join("sessions"));
        write_json(&dir.join("auth.json"), &codex_auth(email, plan, id));
        write(&dir.join("config.toml"), "model = \"gpt-5-codex\"\n");
    }
    write(&home.join(".codex/profile-label"), "base\n");
    write_json(
        &home.join(".prime/agent-solo/auth.json"),
        &json!({"openai-codex": {
            "type": "oauth", "access": jwt(json!({"sub": "acc-alt"})), "accountId": "acc-alt"
        }}),
    );
}

pub fn build_census_home(home: &Path) {
    for relative in [
        ".claude/.profiles/pri",
        ".claude/.profiles/work-2",
        ".claude/.profiles/bad name",
        ".claude/.profiles/-lead",
        ".claude/.profiles/.hidden",
        ".claude/.profiles/.session-map",
        ".codex/sessions",
        ".codex-alt/sessions",
        ".codex-nomarker",
        ".codex-bad name",
        ".codex-",
        ".prime/agent",
        ".prime/agent-solo",
        ".prime/agent-x2",
        ".prime/agent-bad name",
        ".prime/.hidden",
    ] {
        mkdir(&home.join(relative));
    }
    for relative in [
        ".claude/.profiles/notes.txt",
        ".claude/.profiles/.aliases",
        ".codex-file",
        ".prime/agent-file",
    ] {
        write(&home.join(relative), "not a profile home\n");
    }
    write(&home.join(".codex/profile-label"), "base\n");
}

pub const FIVE_HOUR_RESET: i64 = 1290;
pub const SEVEN_DAY_RESET: i64 = 264630;
pub const CODEX_RESET: i64 = 95430;

pub fn statusline_cache(now: i64, five: i64, seven: i64) -> Value {
    json!({"five_hour": {"used_percentage": five, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"used_percentage": seven, "resets_at": now + SEVEN_DAY_RESET},
        "ts": now - 90, "activity_at": now - 30, "source": "statusline"})
}

pub fn api_cache(now: i64, five: i64, seven: i64, age: i64) -> Value {
    json!({"five_hour": {"used_percentage": five, "resets_at": now + FIVE_HOUR_RESET},
        "seven_day": {"used_percentage": seven, "resets_at": now + SEVEN_DAY_RESET},
        "ts": now - age, "fetched_at": now - age, "source": "api"})
}

pub fn codex_cache(now: i64, percent: i64, age: i64) -> Value {
    json!({"rate_limits": {"primary": {"used_percent": percent, "window_minutes": 10080,
        "resets_at": now + CODEX_RESET}, "secondary": null},
        "fetched_at": now - age, "source": "api"})
}

pub fn rollout(dir: &Path, now: i64, percent: i64) {
    let path = dir.join("sessions/2026/09/01/rollout-2026-09-01T09-00-00-019e08eb-508e-7e73-8bc3-1e9c69b5dfd3.jsonl");
    let record = json!({"timestamp": "2026-09-01T09:00:00", "type": "event_msg", "payload": {
        "rate_limits": {"primary": {"used_percent": percent, "window_minutes": 10080,
            "resets_at": now + CODEX_RESET}, "secondary": null}}});
    write_json(&path, &record);
}

pub fn build_usage_home(home: &Path, now: i64) {
    write(&home.join(".claude.json"), "{\"oauthAccount\":null}\n");
    for (name, email) in [("pri", "pri@example.test"), ("work", "work@example.test")] {
        write(
            &home.join(format!(".claude/.profiles/{name}/email")),
            &format!("{email}\n"),
        );
    }
    let profile = home.join(".claude/.profiles/pri");
    write_json(
        &profile.join(".usage-cache.json"),
        &statusline_cache(now, 43, 11),
    );
    write_json(
        &profile.join(".usage-api-cache.json"),
        &api_cache(now, 41, 12, 120),
    );
    write_json(
        &profile.join(".usage-api-cache-fable.json"),
        &api_cache(now, 40, 5, 150),
    );
    for (name, email, plan, id) in [
        ("", "base@example.test", "pro", "acc-base"),
        ("alt", "alt@example.test", "plus", "acc-alt"),
    ] {
        let dir = if name.is_empty() {
            home.join(".codex")
        } else {
            home.join(format!(".codex-{name}"))
        };
        mkdir(&dir.join("sessions"));
        write_json(&dir.join("auth.json"), &codex_auth(email, plan, id));
    }
    write(&home.join(".codex/profile-label"), "\n");
    write_json(
        &home.join(".codex/.usage-hud-api-cache.json"),
        &codex_cache(now, 30, 60),
    );
    rollout(&home.join(".codex-alt"), now, 18);
}

pub const OWNED_SESSION: &str = "019e08eb-508e-7e73-8bc3-1e9c69b5dfd3";
pub const SESSIONS: [(&str, &str, &str, &str, bool); 4] = [
    (
        ".codex",
        "2026-09-01T09-00-00",
        "019e0000-0000-7000-8000-000000000001",
        "proj",
        false,
    ),
    (
        ".codex",
        "2026-09-02T10-00-00",
        "019e0000-0000-7000-8000-000000000002",
        "other",
        false,
    ),
    (
        ".codex-alt",
        "2026-09-03T11-00-00",
        "019e0000-0000-7000-8000-000000000003",
        "proj",
        false,
    ),
    (
        ".codex-alt",
        "2026-08-30T08-00-00",
        "019e0000-0000-7000-8000-000000000004",
        "proj",
        true,
    ),
];

pub fn build_sessions_home(home: &Path) {
    build_fixture_home(home);
    for (codex, started, session_id, cwd, archived) in SESSIONS {
        let folder = if archived {
            "archived_sessions"
        } else {
            "sessions"
        };
        let day = started[..10].replace('-', "/");
        let directory = home.join(codex).join(folder).join(day);
        mkdir(&directory);
        mkdir(&home.join(cwd));
        write_json(
            &directory.join(format!("rollout-{started}-{session_id}.jsonl")),
            &json!({"timestamp": started, "type": "session_meta", "payload": {
                "id": session_id, "cwd": home.join(cwd).display().to_string()
            }}),
        );
    }
}

pub type Env = BTreeMap<String, String>;

pub fn sealed_path() -> &'static str {
    static PATH: OnceLock<String> = OnceLock::new();
    PATH.get_or_init(|| {
        let root = TestHome::new();
        let mut entries = Vec::new();
        for dir in ["/usr/bin", "/bin", "/usr/sbin", "/sbin"] {
            let path = Path::new(dir);
            if !path.is_dir() {
                continue;
            }
            let names: Vec<_> = fs::read_dir(path)
                .unwrap()
                .map(|entry| entry.unwrap())
                .collect();
            if names
                .iter()
                .any(|entry| BLOCKED.contains(&entry.file_name().to_str().unwrap_or("")))
            {
                let mirror = root
                    .root
                    .join(dir.trim_start_matches('/').replace('/', "-"));
                mkdir(&mirror);
                for entry in names {
                    if !BLOCKED.contains(&entry.file_name().to_str().unwrap_or("")) {
                        symlink(entry.path(), mirror.join(entry.file_name())).unwrap();
                    }
                }
                entries.push(mirror.display().to_string());
            } else {
                entries.push(dir.to_string());
            }
        }
        std::mem::forget(root);
        entries.join(":")
    })
}

pub fn fixture_env(home: &Path, security_bin: &str) -> Env {
    let mut env = Env::new();
    env.insert("HOME".into(), home.display().to_string());
    env.insert("PATH".into(), sealed_path().into());
    env.insert(
        "XDG_CACHE_HOME".into(),
        home.join(".cache").display().to_string(),
    );
    env.insert(
        "XDG_CONFIG_HOME".into(),
        home.join(".config").display().to_string(),
    );
    env.insert(
        "XDG_STATE_HOME".into(),
        home.join(".local/state").display().to_string(),
    );
    env.insert(
        "YELO_DOTFILES_ROOT".into(),
        home.join("dotfiles").display().to_string(),
    );
    env.insert("AGENT_PROFILES_SECURITY_BIN".into(), security_bin.into());
    env.insert(
        "CODEX_BIN".into(),
        home.join("no-codex-binary").display().to_string(),
    );
    env
}

pub fn usage_env(home: &Path) -> Env {
    fixture_env(home, TRUE_BIN)
}

pub fn env_for(home: &Path, dotfiles: &Path, binaries: Option<&Path>) -> Env {
    let mut env = fixture_env(home, FALSE_BIN);
    env.insert(
        "PATH".into(),
        binaries.map_or_else(|| sealed_path().to_string(), |p| p.display().to_string()),
    );
    env.insert("YELO_DOTFILES_ROOT".into(), dotfiles.display().to_string());
    env
}

pub fn run_yelo(args: &[&str], env: &Env, cwd: Option<&Path>, stdin: &str) -> Output {
    use std::io::Write;
    use std::process::Stdio;
    let mut command = Command::new("/bin/sh");
    command
        .arg("-c")
        .arg("umask 022; exec \"$0\" \"$@\"")
        .arg(env!("CARGO_BIN_EXE_yelo"))
        .args(args)
        .env_clear()
        .envs(env)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(cwd) = cwd {
        command.current_dir(cwd);
    }
    let mut child = command.spawn().unwrap();
    child
        .stdin
        .take()
        .unwrap()
        .write_all(stdin.as_bytes())
        .unwrap();
    child.wait_with_output().unwrap()
}

pub fn stdout(output: &Output) -> String {
    String::from_utf8(output.stdout.clone()).unwrap()
}

pub fn stderr(output: &Output) -> String {
    String::from_utf8(output.stderr.clone()).unwrap()
}

pub struct Fixture {
    pub temp: TestHome,
}

impl Fixture {
    pub fn new() -> Self {
        let temp = TestHome::new();
        build_fixture_home(&temp.home);
        Self { temp }
    }

    pub fn run(&self, args: &[&str]) -> Output {
        run_yelo(args, &fixture_env(&self.temp.home, FALSE_BIN), None, "")
    }
}

pub struct Bench {
    pub temp: TestHome,
    pub dotfiles: PathBuf,
    pub bin: PathBuf,
    pub settings: PathBuf,
    pub profiles: PathBuf,
    pub launchers: PathBuf,
    pub state: PathBuf,
}

pub fn seeded_settings() -> Value {
    json!({
        "$schema": "https://json.schemastore.org/claude-code-settings.json",
        "model": "opus",
        "permissions": {"allow": ["Bash"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "$HOME/.claude/hooks/bash-guard.sh"}]}],
            "SessionStart": [{"hooks": [
                {"type": "command", "command": "$HOME/.claude/hooks/session-model-cache.sh"},
                {"type": "command", "command": "~/.claude/scripts/agent-host-context.py"}
            ]}],
            "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "$HOME/.claude/scripts/context-nudge.sh"}]}]
        },
        "statusLine": {"type": "command", "command": "statusline.sh"}
    })
}

impl Bench {
    pub fn new() -> Self {
        let temp = TestHome::new();
        mkdir(&temp.home);
        let dotfiles = temp.root.join("dotfiles");
        mkdir(&dotfiles.join("home/.claude/hooks"));
        let bin = temp.root.join("bin");
        mkdir(&bin);
        Self {
            settings: temp.home.join(".claude/settings.json"),
            profiles: temp.home.join(".claude/.profiles"),
            launchers: temp.home.join(".local/bin"),
            state: temp.home.join(".local/state"),
            temp,
            dotfiles,
            bin,
        }
    }

    pub fn run(&self, args: &[&str], overrides: &[(&str, &str)]) -> Output {
        let mut env = env_for(&self.temp.home, &self.dotfiles, Some(&self.bin));
        for (key, value) in overrides {
            env.insert((*key).into(), (*value).into());
        }
        run_yelo(args, &env, None, "")
    }

    pub fn seed_settings(&self, value: &Value) {
        write(
            &self.settings,
            &(serde_json::to_string_pretty(value).unwrap() + "\n"),
        );
    }

    pub fn parsed(&self) -> Value {
        serde_json::from_slice(&fs::read(&self.settings).unwrap()).unwrap()
    }

    pub fn fake(&self, name: &str, body: &str) -> PathBuf {
        let path = self.bin.join(name);
        let log = self.bin.join(format!("{name}.log"));
        write(
            &path,
            &format!(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{}'\n{body}\n",
                log.display()
            ),
        );
        set_mode(&path, 0o755);
        path
    }

    pub fn calls(&self, name: &str) -> Vec<String> {
        fs::read_to_string(self.bin.join(format!("{name}.log")))
            .unwrap_or_default()
            .lines()
            .map(str::to_string)
            .collect()
    }

    pub fn rows(output: &Output) -> BTreeMap<String, String> {
        String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter_map(|line| {
                line.split_once('\t').map(|(key, rest)| {
                    (
                        key.to_string(),
                        rest.split('\t').next().unwrap().to_string(),
                    )
                })
            })
            .collect()
    }
}

pub fn tree_digest(root: &Path) -> String {
    let mut digest = Sha256::new();
    fn visit(root: &Path, dir: &Path, digest: &mut Sha256) {
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
        let mut paths: Vec<_> = dirs.iter().chain(files.iter()).collect();
        paths.sort();
        for path in paths {
            let relative = path.strip_prefix(root).unwrap().display();
            let metadata = fs::symlink_metadata(path).unwrap();
            let mode = metadata.permissions().mode() & 0o777;
            if metadata.file_type().is_symlink() {
                digest.update(format!(
                    "L {relative} {mode} {}\n",
                    fs::read_link(path).unwrap().display()
                ));
            } else if metadata.is_dir() {
                digest.update(format!("D {relative} {mode}\n"));
            } else {
                digest.update(format!("F {relative} {mode} "));
                digest.update(format!("{:x}", Sha256::digest(fs::read(path).unwrap())));
                digest.update(b"\n");
            }
        }
        for path in dirs {
            visit(root, &path, digest);
        }
    }
    visit(root, root, &mut digest);
    format!("{:x}", digest.finalize())
}
