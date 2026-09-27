mod common;

use common::{Bench, mkdir, stderr, write};
use std::fs;
use std::os::unix::fs::symlink;

const LEGACY: &str = include_str!("fixtures/old_shell.zsh");

#[test]
fn test_setup_migrates_known_shell_and_keeps_backup() {
    let bench = Bench::new();
    let path = bench.temp.home.join(".config/yelo/shell.sh");
    write(&path, LEGACY);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    let shell = fs::read_to_string(&path).unwrap();
    assert!(shell.contains("command yelo profile \"$@\""));
    assert!(!shell.contains("profiles.pyz"));
    assert_eq!(
        fs::read_to_string(path.with_file_name("shell.sh.before-profile-integration")).unwrap(),
        LEGACY
    );
    assert_eq!(
        Bench::rows(&bench.run(&["doctor"], &[]))
            .get("launchers")
            .map(String::as_str),
        Some("ok")
    );
    let before = common::tree_digest(&bench.temp.home);
    assert_eq!(
        bench.run(&["setup", "launchers"], &[]).status.code(),
        Some(0)
    );
    assert_eq!(common::tree_digest(&bench.temp.home), before);
}

#[test]
fn test_custom_shell_or_symlink_is_preserved() {
    for is_symlink in [false, true] {
        let bench = Bench::new();
        let path = bench.temp.home.join(".config/yelo/shell.sh");
        mkdir(path.parent().unwrap());
        if is_symlink {
            let target = path.with_file_name("owned-elsewhere");
            write(&target, LEGACY);
            symlink(target, &path).unwrap();
        } else {
            write(&path, &(LEGACY.to_owned() + "# user change\n"));
        }
        let before = fs::read_to_string(&path).unwrap();
        let result = bench.run(&["setup", "launchers"], &[]);
        assert_eq!(result.status.code(), Some(1), "is_symlink={is_symlink}");
        assert!(
            stderr(&result).contains("another owner"),
            "is_symlink={is_symlink}"
        );
        assert_eq!(
            fs::read_to_string(&path).unwrap(),
            before,
            "is_symlink={is_symlink}"
        );
        assert_eq!(
            fs::symlink_metadata(&path)
                .unwrap()
                .file_type()
                .is_symlink(),
            is_symlink
        );
    }
}
