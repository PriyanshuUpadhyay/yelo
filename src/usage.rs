use crate::profile::{self, Row};
use serde_json::{Value, json};
use std::{
    env, fs,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
mod doctor;
mod fetch;

const BASE: &str = ".usage-cache.json";
const FABLE: &str = ".usage-cache-fable.json";
const API: &str = ".usage-api-cache.json";
const API_FABLE: &str = ".usage-api-cache-fable.json";
const CODEX: &str = ".usage-hud-api-cache.json";

pub(crate) fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs() as i64
}
fn number(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64().filter(|x| x.is_finite()),
        _ => None,
    }
}
fn int(value: &Value) -> Option<i64> {
    number(value).map(|x| x.floor() as i64)
}
fn percent(value: &Value) -> i64 {
    number(value).map_or(0, |x| x.clamp(0.0, 100.0).round() as i64)
}
fn clock(doc: &Value, keys: &[&str]) -> Option<i64> {
    for key in keys {
        let value = &doc[*key];
        if !value.is_null() && value != false {
            return int(value);
        }
    }
    None
}
fn human_reset(epoch: i64, now: i64) -> String {
    let seconds = epoch - now;
    if seconds <= 0 {
        return "now".into();
    }
    let days = seconds / 86400;
    let hours = seconds % 86400 / 3600;
    let minutes = seconds % 3600 / 60;
    if days > 0 {
        format!("{days}d{hours}h")
    } else if hours > 0 {
        format!("{hours}h{minutes}m")
    } else {
        format!("{minutes}m")
    }
}
fn window_label(value: &Value, fallback: &str) -> String {
    let minutes = value
        .as_i64()
        .or_else(|| value.as_str()?.parse().ok())
        .unwrap_or(0);
    match minutes {
        0 => fallback.into(),
        300 => "5h".into(),
        10080 => "7d".into(),
        x if x >= 1440 => format!("{}d", x / 1440),
        x => format!("{}h", (x + 59) / 60),
    }
}
#[derive(Clone, Default)]
struct Pick {
    pct: Option<i64>,
    epoch: Option<i64>,
    as_of: Option<i64>,
    seen: Option<i64>,
    source: String,
    fresh: bool,
}
fn consider(
    pick: &mut Pick,
    pct: i64,
    epoch: i64,
    as_of: Option<i64>,
    seen: Option<i64>,
    source: &str,
    timing: (i64, i64),
) {
    let (now, limit) = timing;
    let fresh = seen.is_some_and(|s| now - s <= limit);
    if let Some(previous) = pick.epoch {
        let better = if (epoch - previous).abs() > 120 {
            epoch > previous
        } else {
            (pct, fresh, seen.unwrap_or(0), as_of.unwrap_or(0))
                > (
                    pick.pct.unwrap_or(0),
                    pick.fresh,
                    pick.seen.unwrap_or(0),
                    pick.as_of.unwrap_or(0),
                )
        };
        if !better {
            return;
        }
    }
    *pick = Pick {
        pct: Some(pct),
        epoch: Some(epoch),
        as_of,
        seen,
        source: source.into(),
        fresh,
    };
}
fn cache_pick(pick: &mut Pick, doc: &Value, key: &str, now: i64, limit: i64) {
    let entry = &doc[key];
    if let (Some(pct), Some(epoch)) = (number(&entry["used_percentage"]), int(&entry["resets_at"]))
    {
        let source = doc["source"].as_str().unwrap_or("");
        consider(
            pick,
            pct.clamp(0.0, 100.0).round() as i64,
            epoch,
            clock(doc, &["fetched_at", "ts"]),
            clock(doc, &["fetched_at", "activity_at", "ts"]),
            source,
            (now, limit),
        );
    }
}
fn state(pick: &Pick, now: i64, limit: i64) -> &'static str {
    if pick.epoch.is_none() {
        "missing"
    } else if pick.epoch.is_some_and(|e| e <= now) || pick.seen.is_none_or(|s| now - s > limit) {
        "stale"
    } else {
        "ok"
    }
}
fn data_row(
    label: &str,
    provider: &str,
    window: &str,
    pick: &Pick,
    active: Option<bool>,
    can_fetch: bool,
    timing: (i64, i64),
) -> Value {
    let (now, limit) = timing;
    let mut row = json!({"label":label,"provider":provider,"window":window,"state":state(pick,now,limit),"canFetch":can_fetch});
    if let Some(pct) = pick.pct {
        row["pct"] = json!(pct);
    }
    if let Some(epoch) = pick.epoch {
        row["reset"] = json!(human_reset(epoch, now));
    }
    if let Some(stamp) = pick.as_of {
        row["asOf"] = json!(stamp);
    }
    if let Some(stamp) = pick.seen {
        row["seenAt"] = json!(stamp);
    }
    if !pick.source.is_empty() {
        row["source"] = json!(pick.source);
    }
    if let Some(active) = active {
        row["active"] = json!(active);
    }
    row
}
fn status_row(label: &str, provider: &str, reason: &str, can_fetch: bool) -> Value {
    json!({"label":label,"provider":provider,"state":"offline","reason":reason,"canFetch":can_fetch})
}
pub(crate) fn label(cli: &str, row: &Row) -> String {
    let identity = row.email.as_deref().or(row.name.as_deref()).unwrap_or("");
    if cli == "claude" {
        format!("cl·{identity}")
    } else if identity.is_empty() {
        "cx".into()
    } else {
        format!("cx·{identity}")
    }
}
fn launcher(rows: &mut [Value], cli: &str, row: &Row) {
    let Some(name) = row.name.as_deref().filter(|s| profile::valid_name(s)) else {
        return;
    };
    let path = profile::home()
        .join(".local/bin")
        .join(format!("{cli}-{name}"));
    if path.is_symlink() {
        return;
    }
    let owned = fs::read_to_string(&path)
        .ok()
        .and_then(|s| s.lines().nth(1).map(str::to_owned))
        .is_some_and(|s| s == format!("# written by yelo setup launchers: account {name}"));
    if owned {
        for value in rows {
            value["launcher"] = json!(path);
        }
    }
}
fn claude_rows(row: &Row, active: Option<bool>, now: i64, limit: i64) -> Vec<Value> {
    let path = Path::new(&row.dir);
    let docs: Vec<_> = [API, API_FABLE, BASE, FABLE]
        .iter()
        .map(|name| profile::read_json(&path.join(name)).unwrap_or(Value::Null))
        .collect();
    let mut five = Pick::default();
    let mut seven = Pick::default();
    let mut fable = Pick::default();
    for doc in &docs {
        cache_pick(&mut five, doc, "five_hour", now, limit);
    }
    for doc in [&docs[0], &docs[2]] {
        cache_pick(&mut seven, doc, "seven_day", now, limit);
    }
    cache_pick(&mut fable, &docs[1], "seven_day", now, limit);
    let label = label("claude", row);
    let mut out = if five.epoch.is_none() && seven.epoch.is_none() && fable.epoch.is_none() {
        vec![status_row(&label, "claude", "no data", true)]
    } else {
        let mut rows = vec![
            data_row(&label, "claude", "5h", &five, active, true, (now, limit)),
            data_row(&label, "claude", "7d", &seven, active, true, (now, limit)),
        ];
        if fable.epoch.is_some() || path.join(API_FABLE).is_file() {
            rows.push(data_row(
                &label,
                "claude",
                "fb",
                &fable,
                active,
                true,
                (now, limit),
            ));
        }
        rows
    };
    launcher(&mut out, "claude", row);
    out
}
fn codex_parsed(
    limits: &Value,
    ts: Option<i64>,
    source: &str,
    now: i64,
    limit: i64,
    pick: &mut Pick,
) -> Option<Value> {
    let primary = &limits["primary"];
    let used = &primary["used_percent"];
    if used.is_null() || used == false || !primary["resets_at"].is_number() {
        return None;
    }
    let epoch = int(&primary["resets_at"])?;
    consider(pick, percent(used), epoch, ts, ts, source, (now, limit));
    Some(limits.clone())
}
fn rollout_limits(sessions: &Path) -> Option<(Value, i64)> {
    let mut paths = Vec::new();
    for year in profile::dirs(sessions) {
        for month in profile::dirs(&year) {
            for day in profile::dirs(&month) {
                paths.extend(profile::dirs(&day).into_iter().filter(|p| {
                    p.file_name()
                        .and_then(|s| s.to_str())
                        .is_some_and(|s| s.starts_with("rollout-") && s.ends_with(".jsonl"))
                }));
            }
        }
    }
    paths.sort_by(|a, b| {
        let stamp = |p: &PathBuf| {
            fs::metadata(p)
                .and_then(|m| m.modified())
                .unwrap_or(UNIX_EPOCH)
        };
        stamp(b).cmp(&stamp(a)).then(a.cmp(b))
    });
    for path in paths.into_iter().take(5) {
        let mut found = None;
        for line in fs::read_to_string(&path).unwrap_or_default().lines() {
            let Ok(record) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let limits = if record["payload"]["rate_limits"].is_null() {
                &record["rate_limits"]
            } else {
                &record["payload"]["rate_limits"]
            };
            if number(&limits["primary"]["used_percent"]).is_some() {
                found = Some(limits.clone());
            }
        }
        if let Some(limits) = found {
            let stamp = fs::metadata(path)
                .and_then(|m| m.modified())
                .ok()
                .and_then(|t| t.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs() as i64)
                .unwrap_or(0);
            return Some((limits, stamp));
        }
    }
    None
}
fn codex_rows(row: &Row, can_fetch: bool, now: i64, limit: i64) -> Vec<Value> {
    let path = Path::new(&row.dir);
    let cache = path.join(CODEX);
    let sessions = path.join("sessions");
    let document = profile::read_json(&cache).unwrap_or(Value::Null);
    let mut pick = Pick::default();
    let api = if number(&document["rate_limits"]["primary"]["used_percent"]).is_some() {
        codex_parsed(
            &document["rate_limits"],
            int(&document["fetched_at"]).or(Some(0)),
            "api",
            now,
            limit,
            &mut pick,
        )
    } else {
        None
    };
    let rollout = rollout_limits(&sessions)
        .and_then(|(limits, ts)| codex_parsed(&limits, Some(ts), "rollout", now, limit, &mut pick));
    let label = label("codex", row);
    let mut out = if pick.epoch.is_none() {
        vec![status_row(
            &label,
            "codex",
            if !sessions.is_dir() && !cache.is_file() {
                "not set up"
            } else {
                "no data"
            },
            can_fetch,
        )]
    } else {
        let limits = if pick.source == "api" {
            api.as_ref()
        } else {
            rollout.as_ref()
        }
        .unwrap();
        let primary = &limits["primary"];
        let mut out = vec![data_row(
            &label,
            "codex",
            &window_label(&primary["window_minutes"], "5h"),
            &pick,
            None,
            can_fetch,
            (now, limit),
        )];
        let secondary = &limits["secondary"];
        if let (Some(pct), Some(epoch)) = (
            number(&secondary["used_percent"]),
            int(&secondary["resets_at"]),
        ) {
            let second = Pick {
                pct: Some(pct.clamp(0.0, 100.0).round() as i64),
                epoch: Some(epoch),
                ..pick.clone()
            };
            out.push(data_row(
                &label,
                "codex",
                &window_label(&secondary["window_minutes"], "7d"),
                &second,
                None,
                can_fetch,
                (now, limit),
            ));
        }
        // Credits belong to the account, so both rows carry them from the api cache even when
        // the rollout sample won the pick. A credit that expired since the fetch is dropped.
        let credits = &document["reset_credits"];
        if let Some(available) = int(&credits["available"]) {
            let mut expired = 0;
            let list = credits["expires_at"].as_array().map(|list| {
                let mut kept = Vec::new();
                for at in list {
                    match int(at) {
                        None if at.is_null() => kept.push(None),
                        Some(epoch) if epoch > now => kept.push(Some(epoch)),
                        Some(_) => expired += 1,
                        None => {}
                    }
                }
                // Ascending, with a credit that never expires last.
                kept.sort_by_key(|at| at.unwrap_or(i64::MAX));
                kept
            });
            for value in &mut out {
                value["resetCredits"] = json!((available - expired).max(0));
                if let Some(list) = &list {
                    value["resetCreditsExpireAt"] = json!(list);
                }
            }
        }
        out
    };
    launcher(&mut out, "codex", row);
    out
}
pub(crate) fn snapshot(now: i64) -> Result<Vec<Value>, PathBuf> {
    let root = profile::claude_root();
    if root.is_dir() && fs::read_dir(&root).is_err() {
        return Err(root);
    }
    let limit = env::var("USAGE_HUD_STALE_AFTER")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(900);
    let claude = profile::rows("claude", true);
    let codex = profile::rows("codex", true);
    let mut active = None;
    let mut best = 0;
    for (i, row) in claude.iter().enumerate() {
        for name in [BASE, FABLE] {
            let doc = profile::read_json(&Path::new(&row.dir).join(name)).unwrap_or(Value::Null);
            if let Some(stamp) = clock(&doc, &["activity_at", "ts"])
                && stamp > best
            {
                best = stamp;
                active = Some(i);
            }
        }
    }
    let can_fetch = codex_bin().is_some();
    let mut out = Vec::new();
    for (i, row) in claude.iter().enumerate() {
        out.extend(claude_rows(row, active.map(|a| a == i), now, limit));
    }
    for row in &codex {
        out.extend(codex_rows(row, can_fetch, now, limit));
    }
    Ok(out)
}
pub(crate) fn codex_bin() -> Option<PathBuf> {
    let name = env::var("CODEX_BIN")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "codex".into());
    if name.contains('/') {
        return Path::new(&name).is_file().then(|| PathBuf::from(name));
    }
    env::split_paths(&env::var_os("PATH").unwrap_or_default())
        .map(|dir| dir.join(&name))
        .find(|p| p.is_file())
}
fn show(json: bool) -> i32 {
    let rows = match snapshot(now()) {
        Ok(rows) => rows,
        Err(path) => {
            eprintln!(
                "yelo: usage show: cannot read the claude profile root ({})",
                path.display()
            );
            return 1;
        }
    };
    if json {
        println!("{}", serde_json::to_string(&rows).unwrap());
        return 0;
    }
    let body = rows
        .iter()
        .map(|row| {
            vec![
                row["label"].as_str().unwrap_or("").into(),
                row["window"].as_str().unwrap_or("-").into(),
                row["pct"].as_i64().map_or("-".into(), |n| format!("{n}%")),
                row["reset"].as_str().unwrap_or("-").into(),
                row["resetCredits"]
                    .as_i64()
                    .map_or("-".into(), |n| n.to_string()),
                row["state"].as_str().unwrap_or("").into(),
                row["source"].as_str().unwrap_or("-").into(),
            ]
        })
        .collect();
    print!(
        "{}",
        profile::render(
            vec![
                "LABEL", "WINDOW", "PCT", "RESET", "CREDITS", "STATE", "SOURCE",
            ],
            body
        )
    );
    0
}
pub fn run(args: &[String]) -> i32 {
    match args.get(1).map(String::as_str) {
        Some("show") => show(args.iter().any(|a| a == "--json")),
        Some("fetch") => fetch::run(),
        Some("doctor") => doctor::run(),
        _ => {
            eprintln!("yelo: usage: not ported yet");
            2
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{Pick, consider, label, window_label};
    use crate::profile::Row;
    use serde_json::json;

    #[test]
    fn window_label_cases() {
        for (minutes, expected) in [
            (300, "5h"),
            (10080, "7d"),
            (4320, "3d"),
            (120, "2h"),
            (90, "2h"),
            (0, "fallback"),
        ] {
            assert_eq!(window_label(&json!(minutes), "fallback"), expected);
        }
    }

    #[test]
    fn pick_window_order_and_slack() {
        let now = 1_000_000;
        let mut pick = Pick::default();
        consider(
            &mut pick,
            90,
            now + 100,
            Some(now - 10),
            Some(now - 10),
            "statusline",
            (now, 900),
        );
        consider(
            &mut pick,
            10,
            now + 5000,
            Some(now - 10),
            Some(now - 10),
            "api",
            (now, 900),
        );
        assert_eq!(pick.pct, Some(10));
        let mut pick = Pick::default();
        consider(
            &mut pick,
            10,
            now + 5000,
            Some(now - 10),
            Some(now - 10),
            "api",
            (now, 900),
        );
        consider(
            &mut pick,
            90,
            now + 100,
            Some(now - 10),
            Some(now - 10),
            "statusline",
            (now, 900),
        );
        assert_eq!(pick.pct, Some(10));
        let mut pick = Pick::default();
        consider(
            &mut pick,
            10,
            now + 5000,
            Some(now - 10),
            Some(now - 10),
            "api",
            (now, 900),
        );
        consider(
            &mut pick,
            80,
            now + 5119,
            Some(now - 400),
            Some(now - 400),
            "statusline",
            (now, 900),
        );
        assert_eq!(pick.pct, Some(80));
        let mut pick = Pick::default();
        consider(
            &mut pick,
            10,
            now + 5000,
            Some(now - 10),
            Some(now - 10),
            "api",
            (now, 900),
        );
        consider(
            &mut pick,
            1,
            now + 5121,
            Some(now - 400),
            Some(now - 400),
            "statusline",
            (now, 900),
        );
        assert_eq!(pick.pct, Some(1));
        let mut pick = Pick::default();
        consider(
            &mut pick,
            50,
            now + 5000,
            Some(now - 5000),
            Some(now - 5000),
            "statusline",
            (now, 900),
        );
        consider(
            &mut pick,
            50,
            now + 5000,
            Some(now - 5000),
            Some(now - 10),
            "api",
            (now, 900),
        );
        assert_eq!(pick.source, "api");
        let mut pick = Pick::default();
        consider(
            &mut pick,
            50,
            now + 5000,
            Some(now - 100),
            Some(now - 100),
            "statusline",
            (now, 900),
        );
        consider(
            &mut pick,
            50,
            now + 5000,
            Some(now - 10),
            Some(now - 10),
            "api",
            (now, 900),
        );
        assert_eq!(pick.source, "api");
    }

    fn row(name: Option<&str>, email: Option<&str>) -> Row {
        Row {
            name: name.map(str::to_owned),
            dir: String::new(),
            email: email.map(str::to_owned),
            plan: None,
            signed_in: None,
            aliases: Vec::new(),
            usage: None,
            remaining: None,
            urgency: None,
            fable_exhausted: None,
        }
    }

    #[test]
    fn hud_label_prefers_email_and_falls_back_to_name() {
        assert_eq!(
            label("claude", &row(Some("pri"), Some("person@example.test"))),
            "cl·person@example.test"
        );
        assert_eq!(label("claude", &row(Some("pri"), None)), "cl·pri");
        assert_eq!(
            label("codex", &row(Some("work"), Some("person@example.test"))),
            "cx·person@example.test"
        );
        assert_eq!(label("codex", &row(Some("work"), None)), "cx·work");
        assert_eq!(label("codex", &row(None, None)), "cx");
    }
}
