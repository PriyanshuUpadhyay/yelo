use super::{API, API_FABLE, CODEX, codex_bin, label, now};
use crate::profile::{self, Row};
use serde_json::{Value, json};
use std::{
    env, fs,
    io::{BufRead, BufReader, Write},
    path::Path,
    process::{Command, Stdio},
    sync::mpsc,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn debug(message: &str) {
    if env::var_os("USAGE_HUD_FETCH_DEBUG").is_some() {
        eprintln!("[dbg] {message}");
    }
}
pub(super) fn token(name: &str) -> Option<String> {
    let service = env::var("CLAUDE_KEYCHAIN_SERVICE")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| profile::keychain_service(&profile::home(), name));
    let security = env::var("AGENT_PROFILES_SECURITY_BIN").unwrap_or_else(|_| "security".into());
    let user = env::var("USER").unwrap_or_default();
    let output = Command::new(security)
        .args(["find-generic-password", "-s", &service, "-a", &user, "-w"])
        .output()
        .ok()?;
    if !output.status.success() {
        debug(&format!(
            "{name}: keychain miss svc='{service}' acct='{user}'"
        ));
        return None;
    }
    let blob: Value = serde_json::from_slice(&output.stdout).ok()?;
    let found = blob
        .pointer("/claudeAiOauth/accessToken")
        .and_then(Value::as_str)
        .or_else(|| blob["accessToken"].as_str())
        .filter(|s| !s.is_empty())?;
    debug(&format!(
        "{name}: token found svc='{service}' acct='{user}'"
    ));
    Some(found.into())
}
fn probe<'a>(value: &'a Value, names: &[&str]) -> &'a Value {
    for name in names {
        let item = &value[*name];
        if !item.is_null() && item != false {
            return item;
        }
    }
    &Value::Null
}
fn number(value: &Value) -> Option<f64> {
    if let Some(n) = value.as_f64() {
        return n.is_finite().then_some(n);
    }
    value
        .as_str()?
        .parse::<f64>()
        .ok()
        .filter(|n| n.is_finite())
}
fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let year = year - i64::from(month <= 2);
    let era = year.div_euclid(400);
    let yoe = year - era * 400;
    let doy = (153 * (month + if month > 2 { -3 } else { 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}
fn epoch(value: &Value) -> Option<i64> {
    if value.is_boolean() {
        return None;
    }
    if let Some(n) = number(value) {
        return Some(n as i64);
    }
    let raw = value.as_str()?;
    let mut text = raw.to_owned();
    if let Some(dot) = text.find('.') {
        let end = dot
            + 1
            + text[dot + 1..]
                .bytes()
                .take_while(u8::is_ascii_digit)
                .count();
        text.replace_range(dot..end, "");
    }
    if text.ends_with("+00:00") {
        text.truncate(text.len() - 6);
        text.push('Z');
    }
    if text.len() == 20
        && text.as_bytes()[4] == b'-'
        && text.as_bytes()[7] == b'-'
        && text.as_bytes()[10] == b'T'
        && text.as_bytes()[13] == b':'
        && text.as_bytes()[16] == b':'
        && text.ends_with('Z')
    {
        let parse = |a, b| text[a..b].parse::<i64>().ok();
        let (year, month, day, hour, minute, second) = (
            parse(0, 4)?,
            parse(5, 7)?,
            parse(8, 10)?,
            parse(11, 13)?,
            parse(14, 16)?,
            parse(17, 19)?,
        );
        if (1..=12).contains(&month)
            && (1..=31).contains(&day)
            && hour < 24
            && minute < 60
            && second < 60
        {
            return Some(
                days_from_civil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second,
            );
        }
    }
    None
}
fn pct(window: &Value) -> Option<Value> {
    let value = probe(
        window,
        &["used_percentage", "utilization", "used_percent", "percent"],
    );
    // A JSON number is written back as it came, so 43 stays 43 in the cache, as in Python.
    number(value).map(|n| {
        if value.is_number() {
            value.clone()
        } else {
            json!(n)
        }
    })
}
fn reset(window: &Value) -> Option<i64> {
    epoch(probe(window, &["resets_at", "reset_at", "resets"]))
}
fn cache_doc(five: &Value, seven: Option<&Value>, ts: i64) -> Value {
    let mut result = json!({"five_hour":{"used_percentage":pct(five),"resets_at":reset(five)},"ts":ts,"fetched_at":ts,"source":"api"});
    if let Some(seven) = seven {
        result["seven_day"] = json!({"used_percentage":pct(seven),"resets_at":reset(seven)});
    }
    result
}
fn fable_window(payload: &Value) -> &Value {
    let direct = probe(
        payload,
        &["fable", "seven_day_fable", "fable_seven_day", "fable_week"],
    );
    if !direct.is_null() {
        return direct;
    }
    if let Some(limits) = payload["limits"].as_array() {
        for entry in limits {
            if entry["kind"] == "weekly_scoped"
                && entry.pointer("/scope/model/display_name") == Some(&json!("Fable"))
            {
                return entry;
            }
        }
    }
    &Value::Null
}
fn write_cache(path: &Path, doc: &Value) -> bool {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let tmp = path.with_file_name(format!(
        "{}.tmp.{}.{}",
        path.file_name().unwrap().to_string_lossy(),
        std::process::id(),
        nonce
    ));
    let result = (|| -> std::io::Result<()> {
        let mut file = fs::OpenOptions::new()
            .create_new(true)
            .write(true)
            .open(&tmp)?;
        serde_json::to_writer_pretty(&mut file, doc)?;
        file.write_all(b"\n")?;
        file.flush()?;
        fs::rename(&tmp, path)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    result.is_ok()
}
fn claude(row: &Row) -> (&'static str, bool) {
    let name = row.name.as_deref().unwrap_or("");
    let Some(secret) = token(name) else {
        return ("fetch-failed", false);
    };
    let url = env::var("YELO_USAGE_API_URL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "https://api.anthropic.com/api/oauth/usage".into());
    let mut child = match Command::new("curl")
        .args([
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--write-out",
            "\n%{http_code}",
            "-H",
            "@-",
            "-H",
            "anthropic-beta: oauth-2025-04-20",
            &url,
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return ("fetch-failed", false),
    };
    if let Some(mut stdin) = child.stdin.take()
        && writeln!(stdin, "Authorization: Bearer {secret}").is_err()
    {
        let _ = child.kill();
        let _ = child.wait();
        return ("fetch-failed", false);
    }
    let output = match child.wait_with_output() {
        Ok(output) => output,
        Err(_) => return ("fetch-failed", false),
    };
    if !output.status.success() {
        debug(&format!("{name}: request failed (network/timeout)"));
        return ("fetch-failed", false);
    }
    let Some(split) = output.stdout.iter().rposition(|b| *b == b'\n') else {
        return ("fetch-failed", false);
    };
    let code = std::str::from_utf8(&output.stdout[split + 1..])
        .ok()
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    debug(&format!("{name}: http {code}"));
    if code == 401 || code == 403 {
        return ("auth-stale", false);
    }
    if code != 200 {
        return ("fetch-failed", false);
    }
    let body = &output.stdout[..split];
    let payload = serde_json::from_slice::<Value>(body).ok();
    if env::var_os("USAGE_HUD_FETCH_DUMP").is_some() {
        let dump = payload
            .as_ref()
            .map_or_else(|| "unparseable".into(), |p| p.to_string());
        eprintln!("[dump] {name}: {}", dump.replace(&secret, "<token>"));
    }
    let Some(payload) = payload else {
        return ("fetch-failed", false);
    };
    let five = probe(&payload, &["five_hour", "fiveHour", "five_hour_window"]);
    let seven = probe(&payload, &["seven_day", "sevenDay", "seven_day_window"]);
    let fable = fable_window(&payload);
    let mut windows = Vec::new();
    if pct(five).is_some() {
        windows.push("five_hour");
    }
    if pct(seven).is_some() {
        windows.push("seven_day");
    }
    if pct(fable).is_some() {
        windows.push("fable");
    }
    debug(&format!(
        "{name}: windows found = {}",
        if windows.is_empty() {
            "none".into()
        } else {
            windows.join(",")
        }
    ));
    if !windows.contains(&"five_hour") {
        return ("fetch-failed", false);
    }
    let ts = now();
    let base = cache_doc(five, pct(seven).map(|_| seven), ts);
    if !write_cache(&Path::new(&row.dir).join(API), &base) {
        return ("fetch-failed", false);
    }
    if pct(fable).is_some() {
        let doc = cache_doc(five, Some(fable), ts);
        if !write_cache(&Path::new(&row.dir).join(API_FABLE), &doc) {
            return ("fable-write-failed", true);
        }
    }
    ("ok", true)
}
fn normalize_window(value: &Value) -> Option<Value> {
    if value.is_null() {
        return Some(Value::Null);
    }
    let (used, minutes, reset) = (
        super::number(&value["usedPercent"])?,
        super::number(&value["windowDurationMins"])?,
        super::number(&value["resetsAt"])?,
    );
    let rounded = |n: f64| n.round() as i64;
    Some(
        json!({"used_percent":rounded(used.clamp(0.0,100.0)),"window_minutes":rounded(minutes).max(0),"resets_at":rounded(reset)}),
    )
}
fn timeout_seconds() -> f64 {
    env::var("USAGE_HUD_CODEX_FETCH_TIMEOUT")
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .filter(|n| *n > 0.0)
        .unwrap_or(25.0)
}
fn codex(row: &Row) -> (&'static str, bool) {
    let Some(bin) = codex_bin() else {
        return ("fetch-failed", false);
    };
    let timeout = timeout_seconds();
    let mut child = match Command::new(bin)
        .args(["app-server", "--listen", "stdio://"])
        .env("CODEX_HOME", &row.dir)
        .env(
            "RUST_LOG",
            env::var("RUST_LOG").unwrap_or_else(|_| "error".into()),
        )
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
    {
        Ok(child) => child,
        Err(_) => return ("fetch-failed", false),
    };
    let Some(stdout) = child.stdout.take() else {
        return ("fetch-failed", false);
    };
    let Some(mut stdin) = child.stdin.take() else {
        return ("fetch-failed", false);
    };
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if tx.send(line).is_err() {
                break;
            }
        }
    });
    let init = json!({"method":"initialize","id":1,"params":{"clientInfo":{"name":"usage_hud","title":"Usage HUD","version":"1.0.0"}}});
    let mut result = None;
    if writeln!(stdin, "{init}").is_ok() && stdin.flush().is_ok() {
        let deadline = std::time::Instant::now() + Duration::from_secs_f64(timeout);
        let mut requested = false;
        while let Some(remaining) = deadline.checked_duration_since(std::time::Instant::now()) {
            let Ok(Ok(line)) = rx.recv_timeout(remaining) else {
                break;
            };
            let Ok(message) = serde_json::from_str::<Value>(&line) else {
                continue;
            };
            if message["id"] == 1 && !requested {
                if !message["error"].is_null() {
                    break;
                }
                if writeln!(
                    stdin,
                    "{}",
                    json!({"method":"account/rateLimits/read","id":2})
                )
                .is_err()
                    || stdin.flush().is_err()
                {
                    break;
                }
                requested = true;
            } else if message["id"] == 2 {
                if !message["error"].is_null() {
                    break;
                }
                let limits = &message["result"]["rateLimits"];
                if let Some(primary) = normalize_window(&limits["primary"]).filter(|v| !v.is_null())
                {
                    let secondary = normalize_window(&limits["secondary"]);
                    if let Some(secondary) = secondary {
                        let mut named = serde_json::Map::new();
                        if let Some(entries) = message["result"]["rateLimitsByLimitId"].as_object()
                        {
                            for (id, entry) in entries {
                                if let (Some(name), Some(primary), Some(secondary)) = (
                                    entry["limitName"].as_str(),
                                    normalize_window(&entry["primary"]),
                                    normalize_window(&entry["secondary"]),
                                ) && !primary.is_null()
                                {
                                    named.insert(id.clone(),json!({"limit_name":name,"primary":primary,"secondary":secondary}));
                                }
                            }
                        }
                        let mut doc = json!({"rate_limits":{"primary":primary,"secondary":secondary},"limits_by_id":named,"fetched_at":now(),"source":"api"});
                        let credits = &message["result"]["rateLimitResetCredits"];
                        if let Some(count) = super::number(&credits["availableCount"]) {
                            let mut entry = json!({"available": (count.round() as i64).max(0)});
                            if let Some(list) = credits["credits"].as_array() {
                                // A null expiry is a credit that never expires; a credit with any
                                // other status or a malformed expiry is dropped.
                                entry["expires_at"] = list
                                    .iter()
                                    .filter(|credit| credit["status"] == "available")
                                    .filter_map(|credit| {
                                        let at = &credit["expiresAt"];
                                        if at.is_null() {
                                            Some(Value::Null)
                                        } else {
                                            super::number(at).map(|n| json!(n.round() as i64))
                                        }
                                    })
                                    .collect();
                            }
                            doc["reset_credits"] = entry;
                        }
                        result = Some(doc);
                    }
                }
                break;
            }
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    let Some(doc) = result else {
        return ("fetch-failed", false);
    };
    if write_cache(&Path::new(&row.dir).join(CODEX), &doc) {
        ("ok", true)
    } else {
        ("fetch-failed", false)
    }
}
pub(super) fn run() -> i32 {
    let mut jobs = Vec::new();
    for row in profile::rows("claude", true) {
        jobs.push((label("claude", &row), true, row));
    }
    for row in profile::rows("codex", true) {
        jobs.push((label("codex", &row), false, row));
    }
    if jobs.is_empty() {
        return 1;
    }
    let mut any = false;
    for (name, is_claude, row) in jobs {
        let (word, ok) = if is_claude { claude(&row) } else { codex(&row) };
        println!("{name}: {word}");
        any |= ok;
    }
    if any { 0 } else { 1 }
}

#[cfg(test)]
mod tests {
    use super::{normalize_window, timeout_seconds};
    use serde_json::{Value, json};

    #[test]
    fn normalize_window_clamps_and_rejects_bad_input() {
        assert_eq!(normalize_window(&Value::Null), Some(Value::Null));
        assert_eq!(
            normalize_window(&json!({"usedPercent":140,"windowDurationMins":10080,"resetsAt":1.6})),
            Some(json!({"used_percent":100,"window_minutes":10080,"resets_at":2}))
        );
        assert_eq!(
            normalize_window(&json!({"usedPercent":-5,"windowDurationMins":300,"resetsAt":10})),
            Some(json!({"used_percent":0,"window_minutes":300,"resets_at":10}))
        );
        assert_eq!(normalize_window(&json!([1, 2])), None);
        assert_eq!(
            normalize_window(&json!({"usedPercent":null,"windowDurationMins":300,"resetsAt":10})),
            None
        );
    }

    #[test]
    fn timeout_reads_variable_and_uses_default() {
        unsafe { std::env::set_var("USAGE_HUD_CODEX_FETCH_TIMEOUT", "3.5") };
        assert_eq!(timeout_seconds(), 3.5);
        unsafe { std::env::set_var("USAGE_HUD_CODEX_FETCH_TIMEOUT", "not a number") };
        assert_eq!(timeout_seconds(), 25.0);
        unsafe { std::env::remove_var("USAGE_HUD_CODEX_FETCH_TIMEOUT") };
    }
}
