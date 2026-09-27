use super::{API, API_FABLE, BASE, CODEX, FABLE, fetch, now, snapshot};
use crate::profile::{self, Row};
use serde_json::Value;
use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};

const LABEL: &str = "io.github.priyanshuupadhyay.yelo-hud";
const LEGACY: &str = "work.example.usage-hud";

struct Report {
    failed: bool,
}
impl Report {
    fn pass(&self, text: &str) {
        println!("PASS: {text}");
    }
    fn warn(&self, text: &str) {
        println!("WARN: {text}");
    }
    fn fail(&mut self, text: &str) {
        self.failed = true;
        println!("FAIL: {text}");
    }
}
fn cache_check(report: &mut Report, accounts: &[Row]) {
    for row in accounts {
        let name = row.name.as_deref().unwrap_or("");
        let mut files = 0;
        let mut windows = 0;
        let mut bad = false;
        for cache in [BASE, FABLE, API, API_FABLE] {
            let path = Path::new(&row.dir).join(cache);
            if !path.is_file() {
                continue;
            }
            files += 1;
            let Some(doc) = profile::read_json(&path).filter(Value::is_object) else {
                report.warn(&format!(
                    "{name}/{cache} is not parseable JSON — the data feed reads nothing from it"
                ));
                bad = true;
                continue;
            };
            let mut broken = 0;
            for key in ["five_hour", "seven_day"] {
                if doc[key].is_object() {
                    windows += 1;
                    if super::number(&doc[key]["used_percentage"]).is_none()
                        || super::number(&doc[key]["resets_at"]).is_none()
                    {
                        broken += 1;
                    }
                }
            }
            if broken > 0 {
                report.warn(&format!("{name}/{cache}: {broken} window(s) with a non-numeric used_percentage/resets_at — the data feed drops them silently"));
                bad = true;
            }
        }
        if files == 0 {
            report.warn(&format!(
                "{name}: no usage cache files — nothing feeds the HUD for this profile"
            ));
        } else if !bad {
            report.pass(&format!(
                "{name} caches well-formed ({files} file(s), {windows} window(s))"
            ));
        }
    }
}
fn seen_text(row: &Value, now: i64) -> String {
    let Some(stamp) = super::number(&row["seenAt"]) else {
        return "never confirmed".into();
    };
    let age = (now as f64 - stamp) as i64;
    if !(0..=315360000).contains(&age) {
        return "seenAt unusable".into();
    }
    if age < 120 {
        format!("seen {age}s ago")
    } else {
        format!("seen {}m ago", age / 60)
    }
}
fn freshness(report: &mut Report, rows: &[Value], now: i64) {
    for row in rows {
        println!(
            "  row: {} {} — {}, {}",
            row["label"].as_str().unwrap_or(""),
            row["window"].as_str().unwrap_or("-"),
            row["state"].as_str().unwrap_or(""),
            seen_text(row, now)
        );
    }
    let total = rows.len();
    let stale = rows.iter().filter(|r| r["state"] == "stale").count();
    let current = rows.iter().filter(|r| r["state"] == "ok").count();
    let other = total - stale - current;
    if total == 0 {
        report.fail("the usage snapshot emitted no rows — nothing feeds the HUD");
    } else if stale == total {
        report.fail(&format!(
            "every usage row is stale ({total} of {total}) — the usage pipeline has stopped"
        ));
    } else if stale > 0 {
        report.warn(&format!("{stale} of {total} row(s) stale — those writers have gone quiet, the HUD shows no number for them"));
    } else if current == 0 {
        report.warn(&format!("no usage row carries data ({other} without data) — nothing has been configured or written yet"));
    } else if other > 0 {
        report.pass(&format!(
            "{current} of {total} row(s) current ({other} without data)"
        ));
    } else {
        report.pass(&format!("all {total} row(s) current"));
    }
    let invalid = rows
        .iter()
        .filter(|r| r["state"] != "ok" && r["state"] != "stale" && r.get("pct").is_some())
        .count();
    if invalid > 0 {
        report.fail(&format!(
            "{invalid} row(s) without usable data carry a pct — the row gate has regressed"
        ));
    } else {
        report.pass("no row without usable data carries a pct");
    }
}
fn loaded(label: &str) -> Option<Option<i64>> {
    let uid = Command::new("id").arg("-u").output().ok()?;
    let uid = String::from_utf8(uid.stdout).ok()?;
    let output = Command::new("launchctl")
        .args(["print", &format!("gui/{}/{label}", uid.trim())])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    let pid = text
        .lines()
        .filter_map(|line| {
            let (key, value) = line.split_once('=')?;
            (key.trim() == "pid")
                .then(|| value.trim().parse::<i64>().ok())
                .flatten()
        })
        .next();
    Some(pid)
}
fn liveness(report: &mut Report, home: &Path) {
    let label = env::var("YELO_HUD_LABEL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| LABEL.into());
    match loaded(&label){
        Some(Some(pid))=>report.pass(&format!("{label} running (pid {pid})")),
        Some(None)=>report.fail(&format!("{label} loaded but not running — check {}",home.join("Library/Logs/yelo-hud.err.log").display())),
        None if loaded(LEGACY).is_some()=>report.warn(&format!("{label} not loaded; the dotfiles job {LEGACY} still draws the HUD — cut over with yelo hud install, then yelo hud start")),
        None=>report.fail(&format!("{label} not loaded — the usage HUD is not running (yelo hud install, then yelo hud start)")),
    }
    for (kind, path) in [
        (
            "bundle",
            home.join("Applications/UsageHUD.app/Contents/MacOS/UsageHUD"),
        ),
        (
            "LaunchAgent",
            home.join(format!("Library/LaunchAgents/{label}.plist")),
        ),
    ] {
        if path.is_file() {
            report.pass(&format!("HUD {kind} present: {}", path.display()));
        } else {
            report.warn(&format!(
                "HUD {kind} absent: {} (yelo hud install writes it)",
                path.display()
            ));
        }
    }
}
fn credentials(report: &mut Report, accounts: &[Row]) {
    if accounts.is_empty() {
        report.warn("credential check skipped — no claude profile found");
        return;
    }
    for row in accounts {
        let name = row.name.as_deref().unwrap_or("");
        if fetch::token(name).is_some() {
            report.pass(&format!("{name}: OAuth token readable from Keychain"));
        } else if Path::new(&row.dir).join(".credentials.json").is_file() {
            report.warn(&format!("{name}: .credentials.json present but no Keychain item — yelo usage fetch reads the Keychain only, so this profile never refreshes from the API"));
        } else {
            report.warn(&format!("{name}: no OAuth credential source — API refresh reports fetch-failed (statusline caches still feed the HUD)"));
        }
    }
}
fn codex(report: &mut Report, rows: &[Value]) {
    let mut seen = false;
    for row in rows {
        if row["provider"] != "codex" {
            continue;
        }
        seen = true;
        let label = format!(
            "{} {}",
            row["label"].as_str().unwrap_or(""),
            row["window"].as_str().unwrap_or("")
        );
        let source = row["source"].as_str().unwrap_or("unknown source");
        let state = row["state"].as_str().unwrap_or("");
        if state == "ok" {
            report.pass(&format!("{label}: current from {source}"));
        } else if state == "stale" {
            report.warn(&format!("{label}: stale data from {source}"));
        } else {
            report.warn(&format!("{label}: {state}"));
        }
    }
    if !seen {
        report.warn("no Codex rows — no profile currently feeds the HUD");
    }
}
fn scan_paths(home: &Path, accounts: &[Row]) -> Vec<PathBuf> {
    let mut paths = Vec::new();
    for name in [
        "yelo-hud.out.log",
        "yelo-hud.err.log",
        "usage-hud.out.log",
        "usage-hud.err.log",
    ] {
        let path = home.join("Library/Logs").join(name);
        if path.is_file() {
            paths.push(path);
        }
    }
    for row in accounts {
        for name in [BASE, FABLE, API, API_FABLE] {
            let path = Path::new(&row.dir).join(name);
            if path.is_file() {
                paths.push(path);
            }
        }
    }
    for row in profile::rows("codex", true) {
        let path = Path::new(&row.dir).join(CODEX);
        if path.is_file() {
            paths.push(path);
        }
    }
    paths
}
fn leaks(report: &mut Report, home: &Path, accounts: &[Row]) {
    let paths = scan_paths(home, accounts);
    if paths.is_empty() {
        report.warn("no pipeline-written file exists to scan — the HUD has never run and no cache has been written");
        return;
    }
    let hits = paths
        .iter()
        .filter(|p| {
            let text = fs::read_to_string(p).unwrap_or_default();
            ["Bearer ", "accessToken", "eyJ"]
                .iter()
                .any(|marker| text.contains(marker))
        })
        .count();
    if hits > 0 {
        report.fail(&format!(
            "{hits} pipeline-written file(s) hold possible token material — LEAK, inspect + rotate"
        ));
    } else {
        report.pass(&format!(
            "no token material in {} pipeline-written file(s)",
            paths.len()
        ));
    }
}
pub(super) fn run() -> i32 {
    let home = profile::home();
    let now = now();
    let accounts = profile::rows("claude", true);
    let mut report = Report { failed: false };
    println!("\n— usage meter —");
    let rows = match snapshot(now) {
        Ok(rows) => Some(rows),
        Err(path) => {
            report.fail(&format!("the usage snapshot failed: cannot read the claude profile root ({}) — the HUD polls this exact command",path.display()));
            None
        }
    };
    cache_check(&mut report, &accounts);
    if let Some(rows) = &rows {
        freshness(&mut report, rows, now);
    }
    liveness(&mut report, &home);
    credentials(&mut report, &accounts);
    println!("\n— codex usage meter —");
    if let Some(rows) = &rows {
        codex(&mut report, rows);
    } else {
        report.warn("Codex row check skipped — the usage snapshot is unavailable");
    }
    println!("\n— token-leak scan —");
    leaks(&mut report, &home, &accounts);
    if report.failed { 1 } else { 0 }
}

#[cfg(test)]
mod tests {
    use super::{Report, freshness};
    use serde_json::json;

    #[test]
    fn freshness_rejects_pct_on_offline_row() {
        let mut report = Report { failed: false };
        freshness(
            &mut report,
            &[json!({"label":"cl·pri","window":"5h","state":"offline","pct":43})],
            1_900_000_000,
        );
        assert!(report.failed);
    }
}
