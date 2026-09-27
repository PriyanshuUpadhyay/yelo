mod common;

use common::{
    Env, TestHome, env_for, mkdir, read_plist, run_yelo, set_mode, stderr, stdout, tree_digest,
    write, write_plist,
};
use serde_json::json;
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::{Path, PathBuf};
use std::process::Command;

const LABEL: &str = "io.github.priyanshuupadhyay.yelo-hud-test";
const FAKE_SWIFT: &str = "#!/bin/sh\nLOG=\"$YELO_TEST_SWIFT_LOG\"\nprintf '%s\\t' \"$@\" >> \"$LOG\"; printf '\\n' >> \"$LOG\"\npackage=\nwhile [ $# -gt 0 ]; do\n  case \"$1\" in\n    --package-path) package=\"$2\"; shift 2;;\n    *) shift;;\n  esac\ndone\n[ -n \"$package\" ] || exit 2\nmkdir -p \"$package/.build/release\"\nprintf 'fake UsageHUD binary\\n' > \"$package/.build/release/UsageHUD\"\nchmod 755 \"$package/.build/release/UsageHUD\"\nexit 0\n";
const FAKE_CODESIGN: &str = "#!/bin/sh\nLOG=\"$YELO_TEST_CODESIGN_LOG\"\nprintf '%s\\t' \"$@\" >> \"$LOG\"; printf '\\n' >> \"$LOG\"\nexit 0\n";
const FAKE_LAUNCHCTL: &str = "#!/bin/sh\nLOG=\"$YELO_TEST_LAUNCHCTL_LOG\"\nprintf '%s\\t' \"$@\" >> \"$LOG\"; printf '\\n' >> \"$LOG\"\nstate=\"$YELO_TEST_LAUNCH_STATE\"\ncase \"$1\" in\n  print)\n    label=\"${2##*/}\"\n    if [ -f \"$state\" ] && [ \"$(cat \"$state\")\" = \"$label\" ]; then\n      printf 'service = {\\n\\tstate = running\\n\\tpid = 4242\\n}\\n'\n      exit 0\n    fi\n    printf 'Could not find service \"%s\"\\n' \"$label\" >&2\n    exit 113;;\n  bootstrap)\n    if [ -n \"$YELO_TEST_BOOTSTRAP_FAIL\" ]; then\n      printf 'Bootstrap failed: 5: Input/output error\\n' >&2\n      exit 5\n    fi\n    [ -n \"$YELO_TEST_BOOTSTRAP_VANISH\" ] && exit 0\n    base=\"${3##*/}\"\n    printf '%s' \"${base%.plist}\" > \"$state\"\n    exit 0;;\n  bootout) rm -f \"$state\"; exit 0;;\nesac\nexit 9\n";

fn version() -> String {
    fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
        .unwrap()
        .lines()
        .find_map(|line| {
            line.strip_prefix("version = \"")
                .and_then(|tail| tail.strip_suffix('"'))
        })
        .unwrap()
        .to_string()
}
fn uid() -> String {
    let output = Command::new("/usr/bin/id").arg("-u").output().unwrap();
    assert!(output.status.success());
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}
fn executable(path: &Path, body: &str) {
    write(path, body);
    set_mode(path, 0o755);
}

struct Hud {
    temp: TestHome,
    dotfiles: PathBuf,
    package: PathBuf,
    bin: PathBuf,
    bundle: PathBuf,
    binary: PathBuf,
    info: PathBuf,
    plist: PathBuf,
    swift_log: PathBuf,
    codesign_log: PathBuf,
    launchctl_log: PathBuf,
    state: PathBuf,
    path: String,
}

impl Hud {
    fn new() -> Self {
        let temp = TestHome::new();
        mkdir(&temp.home);
        let dotfiles = temp.root.join("dotfiles");
        mkdir(&dotfiles.join("home"));
        let package = temp.root.join("package");
        mkdir(&package);
        write(
            &package.join("Package.swift"),
            "// swift-tools-version:5.9\n",
        );
        let bin = temp.root.join("bin");
        mkdir(&bin);
        executable(&bin.join("swift"), FAKE_SWIFT);
        executable(&bin.join("codesign"), FAKE_CODESIGN);
        executable(&bin.join("launchctl"), FAKE_LAUNCHCTL);
        executable(&bin.join("yelo"), "#!/bin/sh\nexit 0\n");
        let logs = temp.root.join("logs");
        mkdir(&logs);
        let bundle = temp.home.join("Applications/UsageHUD.app");
        let binary = bundle.join("Contents/MacOS/UsageHUD");
        let info = bundle.join("Contents/Info.plist");
        let plist = temp
            .home
            .join(format!("Library/LaunchAgents/{LABEL}.plist"));
        let path = format!("{}:{}", bin.display(), common::sealed_path());
        Self {
            swift_log: logs.join("swift"),
            codesign_log: logs.join("codesign"),
            launchctl_log: logs.join("launchctl"),
            state: logs.join("launch-state"),
            temp,
            dotfiles,
            package,
            bin,
            bundle,
            binary,
            info,
            plist,
            path,
        }
    }
    fn env(&self, overrides: &[(&str, &str)]) -> Env {
        let mut env = env_for(&self.temp.home, &self.dotfiles, None);
        env.insert("PATH".into(), self.path.clone());
        env.insert("YELO_HUD_LABEL".into(), LABEL.into());
        env.insert(
            "YELO_HUD_PACKAGE".into(),
            self.package.display().to_string(),
        );
        env.insert(
            "YELO_TEST_SWIFT_LOG".into(),
            self.swift_log.display().to_string(),
        );
        env.insert(
            "YELO_TEST_CODESIGN_LOG".into(),
            self.codesign_log.display().to_string(),
        );
        env.insert(
            "YELO_TEST_LAUNCHCTL_LOG".into(),
            self.launchctl_log.display().to_string(),
        );
        env.insert(
            "YELO_TEST_LAUNCH_STATE".into(),
            self.state.display().to_string(),
        );
        for (key, value) in overrides {
            env.insert((*key).into(), (*value).into());
        }
        env
    }
    fn run(&self, args: &[&str], overrides: &[(&str, &str)]) -> std::process::Output {
        run_yelo(args, &self.env(overrides), None, "")
    }
    fn calls(&self, log: &Path) -> Vec<Vec<String>> {
        fs::read_to_string(log)
            .unwrap_or_default()
            .lines()
            .map(|line| {
                line.split('\t')
                    .filter(|field| !field.is_empty())
                    .map(str::to_string)
                    .collect()
            })
            .collect()
    }
}

#[test]
fn test_install_bundle_and_plist() {
    let hud = Hud::new();
    let result = hud.run(&["hud", "install"], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        stdout(&result).lines().collect::<Vec<_>>(),
        ["bundle rebuilt", "plist written", "next: yelo hud start"]
    );
    assert_eq!(
        hud.calls(&hud.swift_log),
        [vec![
            "build",
            "-c",
            "release",
            "--package-path",
            hud.package.to_str().unwrap()
        ]]
    );
    assert_eq!(
        fs::read_to_string(&hud.binary).unwrap(),
        "fake UsageHUD binary\n"
    );
    assert_eq!(
        fs::metadata(&hud.binary).unwrap().permissions().mode() & 0o777,
        0o755
    );
    assert_eq!(
        read_plist(&hud.info),
        json!({
            "CFBundleName":"UsageHUD", "CFBundleExecutable":"UsageHUD", "CFBundleIdentifier":LABEL,
            "CFBundlePackageType":"APPL", "CFBundleShortVersionString":version(), "LSUIElement":true,
            "LSMinimumSystemVersion":"14.0", "NSHighResolutionCapable":true
        })
    );
    let calls = hud.calls(&hud.codesign_log);
    assert_eq!(calls.len(), 1);
    assert_eq!(
        calls[0][..5],
        ["--force", "--sign", "-", "--identifier", LABEL]
    );
    let staged = Path::new(&calls[0][5]);
    assert_eq!(
        staged.parent(),
        Some(hud.temp.home.join("Applications").as_path())
    );
    assert!(
        staged
            .file_name()
            .unwrap()
            .to_string_lossy()
            .starts_with(".UsageHUD.app.new.")
    );
    assert!(!staged.exists());
    assert_eq!(
        read_plist(&hud.plist),
        json!({
            "Label":LABEL, "ProgramArguments":[hud.binary.display().to_string()], "RunAtLoad":true,
            "KeepAlive":{"SuccessfulExit":false},
            "StandardOutPath":hud.temp.home.join("Library/Logs/yelo-hud.out.log").display().to_string(),
            "StandardErrorPath":hud.temp.home.join("Library/Logs/yelo-hud.err.log").display().to_string(),
            "EnvironmentVariables":{"YELO_BIN":hud.bin.join("yelo").display().to_string(),"PATH":hud.path}
        })
    );
    assert!(hud.calls(&hud.launchctl_log).is_empty());
    let before = (fs::read(&hud.plist).unwrap(), common::mtime_ns(&hud.plist));
    let again = hud.run(&["hud", "install"], &[]);
    assert_eq!(again.status.code(), Some(0), "{}", stderr(&again));
    assert_eq!(
        stdout(&again).lines().collect::<Vec<_>>(),
        ["bundle rebuilt", "plist unchanged", "next: yelo hud start"]
    );
    assert_eq!(
        (fs::read(&hud.plist).unwrap(), common::mtime_ns(&hud.plist)),
        before
    );
}

#[test]
fn test_install_refuses_dotfiles_owned() {
    for target in ["plist", "bundle"] {
        let hud = Hud::new();
        let (owned, link, message) = if target == "plist" {
            (
                hud.dotfiles
                    .join(format!("home/Library/LaunchAgents/{LABEL}.plist")),
                hud.plist.clone(),
                "LaunchAgent is owned by dotfiles",
            )
        } else {
            (
                hud.dotfiles.join("home/Applications/UsageHUD.app"),
                hud.bundle.clone(),
                "bundle is owned by dotfiles",
            )
        };
        if target == "plist" {
            write_plist(&owned, &json!({"Label": LABEL}));
        } else {
            mkdir(&owned);
        }
        mkdir(link.parent().unwrap());
        symlink(&owned, &link).unwrap();
        let before = tree_digest(&hud.dotfiles);
        let result = hud.run(&["hud", "install"], &[]);
        assert_eq!(result.status.code(), Some(1), "{target}");
        assert!(result.stdout.is_empty(), "{target}");
        assert_eq!(
            stderr(&result),
            format!("yelo: hud install: {message} ({})\n", link.display()),
            "{target}"
        );
        assert!(hud.calls(&hud.swift_log).is_empty(), "{target}");
        assert!(hud.calls(&hud.codesign_log).is_empty(), "{target}");
        assert!(
            fs::symlink_metadata(&link)
                .unwrap()
                .file_type()
                .is_symlink(),
            "{target}"
        );
        assert_eq!(tree_digest(&hud.dotfiles), before, "{target}");
    }
}

#[test]
fn test_start_stop_matrix() {
    let hud = Hud::new();
    let missing = hud.run(&["hud", "start"], &[]);
    assert_eq!(missing.status.code(), Some(1));
    assert_eq!(
        stderr(&missing),
        format!(
            "yelo: hud start: LaunchAgent not installed ({})\n",
            hud.plist.display()
        )
    );
    let absent_stop = hud.run(&["hud", "stop"], &[]);
    assert_eq!(absent_stop.status.code(), Some(0));
    assert_eq!(stdout(&absent_stop), "already stopped\n");
    assert_eq!(hud.run(&["hud", "install"], &[]).status.code(), Some(0));
    let started = hud.run(&["hud", "start"], &[]);
    assert_eq!(started.status.code(), Some(0), "{}", stderr(&started));
    assert_eq!(stdout(&started), "running pid 4242\n");
    assert!(hud.calls(&hud.launchctl_log).contains(&vec![
        "bootstrap".into(),
        format!("gui/{}", uid()),
        hud.plist.display().to_string()
    ]));
    let again = hud.run(&["hud", "start"], &[]);
    assert_eq!(again.status.code(), Some(0));
    assert_eq!(stdout(&again), "already running\n");
    let stopped = hud.run(&["hud", "stop"], &[]);
    assert_eq!(stopped.status.code(), Some(0));
    assert_eq!(stdout(&stopped), "stopped\n");
    assert!(
        hud.calls(&hud.launchctl_log)
            .contains(&vec!["bootout".into(), format!("gui/{}/{LABEL}", uid())])
    );
    assert_eq!(stdout(&hud.run(&["hud", "stop"], &[])), "already stopped\n");
}

#[test]
fn test_start_reports_a_failed_bootstrap() {
    let hud = Hud::new();
    assert_eq!(hud.run(&["hud", "install"], &[]).status.code(), Some(0));
    let result = hud.run(&["hud", "start"], &[("YELO_TEST_BOOTSTRAP_FAIL", "1")]);
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: hud start: launchctl bootstrap failed ({})\n",
            hud.plist.display()
        )
    );
    assert!(result.stdout.is_empty());
}

#[test]
fn test_start_reports_a_job_that_does_not_stay_loaded() {
    let hud = Hud::new();
    assert_eq!(hud.run(&["hud", "install"], &[]).status.code(), Some(0));
    let result = hud.run(&["hud", "start"], &[("YELO_TEST_BOOTSTRAP_VANISH", "1")]);
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: hud start: job did not stay loaded after bootstrap ({})\n",
            hud.plist.display()
        )
    );
    assert!(result.stdout.is_empty());
    assert!(hud.calls(&hud.launchctl_log).contains(&vec![
        "bootstrap".into(),
        format!("gui/{}", uid()),
        hud.plist.display().to_string()
    ]));
}

#[test]
fn test_install_refuses_a_package_it_cannot_find() {
    let hud = Hud::new();
    let missing = hud.temp.home.join("no-package");
    let result = hud.run(
        &["hud", "install"],
        &[("YELO_HUD_PACKAGE", &missing.display().to_string())],
    );
    assert_eq!(result.status.code(), Some(1));
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: hud install: swift package not found ({})\n",
            missing.display()
        )
    );
    assert!(hud.calls(&hud.swift_log).is_empty());
}

#[test]
fn test_install_copies_prebuilt_bundle() {
    let hud = Hud::new();
    let prebuilt = hud.temp.root.join("prebuilt/UsageHUD.app");
    executable(
        &prebuilt.join("Contents/MacOS/UsageHUD"),
        "prebuilt UsageHUD binary\n",
    );
    write_plist(
        &prebuilt.join("Contents/Info.plist"),
        &json!({"CFBundleName":"UsageHUD"}),
    );
    let result = hud.run(
        &["hud", "install"],
        &[("YELO_HUD_BUNDLE", &prebuilt.display().to_string())],
    );
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        stdout(&result).lines().collect::<Vec<_>>(),
        [
            format!("bundle installed from {}", prebuilt.display()),
            "plist written".into(),
            "next: yelo hud start".into()
        ]
    );
    assert!(hud.calls(&hud.swift_log).is_empty());
    assert_eq!(
        fs::read_to_string(&hud.binary).unwrap(),
        "prebuilt UsageHUD binary\n"
    );
    assert_eq!(read_plist(&hud.info), json!({"CFBundleName":"UsageHUD"}));
}

#[test]
fn test_install_refuses_missing_prebuilt_bundle() {
    let hud = Hud::new();
    let empty = hud.temp.root.join("empty.app");
    mkdir(&empty);
    let result = hud.run(
        &["hud", "install"],
        &[("YELO_HUD_BUNDLE", &empty.display().to_string())],
    );
    assert_eq!(result.status.code(), Some(1));
    assert!(result.stdout.is_empty());
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: hud install: prebuilt bundle not found ({})\n",
            empty.display()
        )
    );
    assert!(hud.calls(&hud.swift_log).is_empty());
    assert!(!hud.bundle.exists());
}

#[test]
fn test_plist_records_explicit_launcher() {
    let hud = Hud::new();
    let result = hud.run(&["hud", "install"], &[("YELO_BIN", "/opt/x/bin/yelo")]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        read_plist(&hud.plist)["EnvironmentVariables"]["YELO_BIN"],
        "/opt/x/bin/yelo"
    );
}

#[test]
fn test_assemble_builds_signed_bundle_without_launchagent() {
    let hud = Hud::new();
    let source = hud.package.join(".build/release/UsageHUD");
    write(&source, "built UsageHUD binary\n");
    let home = hud.temp.home.display().to_string();
    let package = hud.package.display().to_string();
    let result = hud.run(&["hud", "assemble", &home, &package], &[]);
    assert_eq!(result.status.code(), Some(0), "{}", stderr(&result));
    assert_eq!(
        fs::read_to_string(&hud.binary).unwrap(),
        "built UsageHUD binary\n"
    );
    assert_eq!(
        fs::metadata(&hud.binary).unwrap().permissions().mode() & 0o777,
        0o755
    );
    assert_eq!(read_plist(&hud.info)["CFBundleIdentifier"], LABEL);
    let calls = hud.calls(&hud.codesign_log);
    assert_eq!(calls.len(), 1);
    assert_eq!(
        calls[0][..5],
        ["--force", "--sign", "-", "--identifier", LABEL]
    );
    assert_eq!(
        Path::new(&calls[0][5]).parent(),
        Some(hud.temp.home.join("Applications").as_path())
    );
    assert!(!Path::new(&calls[0][5]).exists());
    assert!(hud.calls(&hud.swift_log).is_empty());
    assert!(!hud.plist.exists());
}

#[test]
fn test_assemble_missing_binary_creates_nothing() {
    let hud = Hud::new();
    let home = hud.temp.home.display().to_string();
    let package = hud.package.display().to_string();
    let result = hud.run(&["hud", "assemble", &home, &package], &[]);
    let source = hud.package.join(".build/release/UsageHUD");
    assert_ne!(result.status.code(), Some(0));
    assert_eq!(
        stderr(&result),
        format!(
            "yelo: hud assemble: swift build produced no binary ({})\n",
            source.display()
        )
    );
    assert!(!hud.temp.home.join("Applications").exists());
    assert!(hud.calls(&hud.codesign_log).is_empty());
}
