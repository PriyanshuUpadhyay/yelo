mod common;

use common::{Fixture, mkdir, stderr, stdout};
use std::fs;

#[test]
fn test_profile_sync_reports_drift() {
    let fixture = Fixture::new();
    let home = &fixture.temp.home;
    mkdir(&home.join(".claude/projects"));
    let drift = home.join(".claude/.profiles/pri/projects");
    mkdir(&drift);
    let result = fixture.run(&["profile", "sync", "--cli", "claude"]);
    assert_eq!(result.status.code(), Some(1), "{}", stderr(&result));
    assert!(stdout(&result).contains("synced 2 Claude profiles\n"));
    assert!(stdout(&result).contains(&format!("drift\t{}\n", drift.display())));
    assert_eq!(
        fs::read_link(home.join(".claude/.profiles/work/projects"))
            .unwrap()
            .to_str(),
        Some("../../projects")
    );
}
