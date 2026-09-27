use crate::{profile, setup};
use std::{
    env, fs, io,
    os::unix::fs::{PermissionsExt, symlink},
    path::{Path, PathBuf},
    process::Command,
};

unsafe extern "C" {
    fn getuid() -> u32;
}

fn uid() -> u32 {
    unsafe { getuid() }
}
fn label() -> String {
    env::var("YELO_HUD_LABEL")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "io.github.priyanshuupadhyay.yelo-hud".into())
}
fn bundle(home: &Path) -> PathBuf {
    home.join("Applications/UsageHUD.app")
}
fn binary(home: &Path) -> PathBuf {
    bundle(home).join("Contents/MacOS/UsageHUD")
}
fn plist(home: &Path) -> PathBuf {
    home.join("Library/LaunchAgents")
        .join(format!("{}.plist", label()))
}
fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&apos;")
}
fn xml_string(s: &str) -> String {
    format!("<string>{}</string>", xml_escape(s))
}
fn xml_key(s: &str) -> String {
    format!("<key>{}</key>", xml_escape(s))
}
fn xml_dict(body: &str) -> String {
    format!("<dict>{body}</dict>")
}
fn xml_doc(body: &str) -> String {
    format!(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n<plist version=\"1.0\">{body}</plist>\n"
    )
}
fn pair(key: &str, value: &str) -> String {
    format!("{}{}", xml_key(key), xml_string(value))
}
fn info_xml() -> String {
    let fields = [
        pair("CFBundleName", "UsageHUD"),
        pair("CFBundleExecutable", "UsageHUD"),
        pair("CFBundleIdentifier", &label()),
        pair("CFBundlePackageType", "APPL"),
        pair("CFBundleShortVersionString", env!("CARGO_PKG_VERSION")),
        format!("{}<true/>", xml_key("LSUIElement")),
        pair("LSMinimumSystemVersion", "14.0"),
        format!("{}<true/>", xml_key("NSHighResolutionCapable")),
    ];
    xml_doc(&xml_dict(&fields.concat()))
}
fn launcher() -> Result<String, String> {
    if let Ok(path) = env::var("YELO_BIN")
        && !path.is_empty()
    {
        return Ok(path);
    }
    let path = env::var_os("PATH").unwrap_or_default();
    for directory in env::split_paths(&path) {
        let candidate = directory.join("yelo");
        if candidate.is_file() {
            return Ok(candidate.to_string_lossy().into_owned());
        }
    }
    Err("cannot locate the yelo launcher for YELO_BIN".into())
}
fn plist_xml(home: &Path) -> Result<String, String> {
    let mut environment = pair("YELO_BIN", &launcher()?);
    if let Ok(path) = env::var("PATH")
        && !path.is_empty()
    {
        environment.push_str(&pair("PATH", &path));
    }
    let fields = [
        pair("Label", &label()),
        format!(
            "{}<array>{}</array>",
            xml_key("ProgramArguments"),
            xml_string(&binary(home).to_string_lossy())
        ),
        format!("{}<true/>", xml_key("RunAtLoad")),
        format!(
            "{}{}",
            xml_key("KeepAlive"),
            xml_dict(&format!("{}<false/>", xml_key("SuccessfulExit")))
        ),
        pair(
            "StandardOutPath",
            &home.join("Library/Logs/yelo-hud.out.log").to_string_lossy(),
        ),
        pair(
            "StandardErrorPath",
            &home.join("Library/Logs/yelo-hud.err.log").to_string_lossy(),
        ),
        format!(
            "{}{}",
            xml_key("EnvironmentVariables"),
            xml_dict(&environment)
        ),
    ];
    Ok(xml_doc(&xml_dict(&fields.concat())))
}
fn report_error(action: &str, message: &str, path: Option<&Path>) -> i32 {
    if let Some(path) = path {
        eprintln!("yelo: hud {action}: {message} ({})", path.display());
    } else {
        eprintln!("yelo: hud {action}: {message}");
    }
    1
}
fn copy_tree(source: &Path, target: &Path) -> io::Result<()> {
    fs::create_dir_all(target)?;
    for entry in fs::read_dir(source)? {
        let entry = entry?;
        let src = entry.path();
        let dst = target.join(entry.file_name());
        let meta = fs::symlink_metadata(&src)?;
        if meta.is_dir() {
            copy_tree(&src, &dst)?;
        } else if meta.file_type().is_symlink() {
            symlink(fs::read_link(&src)?, dst)?;
        } else {
            fs::copy(src, dst)?;
        }
    }
    Ok(())
}
fn job_state() -> (bool, Option<i32>) {
    let target = format!("gui/{}/{}", uid(), label());
    let Ok(output) = Command::new("launchctl").args(["print", &target]).output() else {
        return (false, None);
    };
    if !output.status.success() {
        return (false, None);
    }
    let body = String::from_utf8_lossy(&output.stdout);
    let pid = body.lines().find_map(|line| {
        let (key, value) = line.split_once('=')?;
        (key.trim() == "pid")
            .then(|| value.trim().parse::<i32>().ok())
            .flatten()
    });
    (true, pid)
}
fn stage_bundle(
    home: &Path,
    source: &Path,
    prebuilt: bool,
) -> Result<(), (String, Option<PathBuf>)> {
    let bundle_path = bundle(home);
    let parent = bundle_path.parent().unwrap();
    fs::create_dir_all(parent).map_err(|error| (error.to_string(), None))?;
    let stage = parent.join(format!(".UsageHUD.app.new.{}", std::process::id()));
    if stage.exists() {
        let _ = fs::remove_dir_all(&stage);
    }
    let prepared = if prebuilt {
        copy_tree(source, &stage)
    } else {
        let dest = stage.join("Contents/MacOS");
        fs::create_dir_all(&dest)
            .and_then(|_| fs::copy(source, dest.join("UsageHUD")).map(|_| ()))
            .and_then(|_| {
                fs::set_permissions(dest.join("UsageHUD"), fs::Permissions::from_mode(0o755))
            })
            .and_then(|_| fs::write(stage.join("Contents/Info.plist"), info_xml()))
    };
    if let Err(error) = prepared {
        let _ = fs::remove_dir_all(&stage);
        return Err((error.to_string(), None));
    }
    let signed = Command::new("codesign")
        .args(["--force", "--sign", "-", "--identifier", &label()])
        .arg(&stage)
        .status();
    match signed {
        Ok(status) if status.success() => {}
        Ok(_) => {
            let _ = fs::remove_dir_all(&stage);
            return Err(("codesign failed".into(), Some(stage)));
        }
        Err(error) => {
            let _ = fs::remove_dir_all(&stage);
            return Err((error.to_string(), Some(stage)));
        }
    }
    let old = parent.join(format!(".UsageHUD.app.old.{}", std::process::id()));
    if old.exists() {
        let _ = fs::remove_dir_all(&old);
    }
    if bundle_path.exists() {
        fs::rename(&bundle_path, &old).map_err(|error| (error.to_string(), None))?;
    }
    fs::rename(&stage, &bundle_path).map_err(|error| (error.to_string(), None))?;
    if old.exists() {
        let _ = fs::remove_dir_all(old);
    }
    Ok(())
}
fn assemble_bundle(home: &Path, package: &Path) -> Result<(), (String, Option<PathBuf>)> {
    let source = package.join(".build/release/UsageHUD");
    if !source.is_file() {
        return Err(("swift build produced no binary".into(), Some(source)));
    }
    stage_bundle(home, &source, false)
}
fn install() -> i32 {
    let home = profile::home();
    let plist_path = plist(&home);
    let bundle_path = bundle(&home);
    if setup::dotfiles_owned(&plist_path) {
        return report_error(
            "install",
            "LaunchAgent is owned by dotfiles",
            Some(&plist_path),
        );
    }
    if setup::dotfiles_owned(&bundle_path) {
        return report_error("install", "bundle is owned by dotfiles", Some(&bundle_path));
    }
    let document = match plist_xml(&home) {
        Ok(s) => s,
        Err(s) => return report_error("install", &s, None),
    };
    let prebuilt = env::var("YELO_HUD_BUNDLE").ok().filter(|s| !s.is_empty());
    let package = env::var("YELO_HUD_PACKAGE")
        .ok()
        .filter(|s| !s.is_empty())
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("apps/UsageHUD"));
    let assembled = if let Some(path) = &prebuilt {
        let source = PathBuf::from(path);
        if !source.join("Contents/MacOS/UsageHUD").is_file() {
            return report_error("install", "prebuilt bundle not found", Some(&source));
        }
        stage_bundle(&home, &source, true)
    } else {
        if !package.join("Package.swift").is_file() {
            return report_error("install", "swift package not found", Some(&package));
        }
        let status = Command::new("swift")
            .args(["build", "-c", "release", "--package-path"])
            .arg(&package)
            .status();
        match status {
            Ok(status) if status.success() => {}
            Ok(_) => return report_error("install", "swift build failed", Some(&package)),
            Err(error) => return report_error("install", &error.to_string(), Some(&package)),
        }
        assemble_bundle(&home, &package)
    };
    if let Err((message, path)) = assembled {
        return report_error("install", &message, path.as_deref());
    }
    if let Some(path) = prebuilt {
        println!("bundle installed from {path}");
    } else {
        println!("bundle rebuilt");
    }
    let changed = fs::read(&plist_path).ok().as_deref() != Some(document.as_bytes());
    if changed {
        let parent = plist_path.parent().unwrap();
        if let Err(error) = fs::create_dir_all(parent) {
            return report_error("install", &error.to_string(), None);
        }
        let temp = parent.join(format!(".{}.plist.{}", label(), std::process::id()));
        if let Err(error) = fs::write(&temp, document)
            .and_then(|_| fs::set_permissions(&temp, fs::Permissions::from_mode(0o644)))
            .and_then(|_| fs::rename(&temp, &plist_path))
        {
            return report_error("install", &error.to_string(), None);
        }
    }
    println!("plist {}", if changed { "written" } else { "unchanged" });
    println!("next: yelo hud start");
    0
}
fn start() -> i32 {
    let path = plist(&profile::home());
    if !path.is_file() {
        return report_error("start", "LaunchAgent not installed", Some(&path));
    }
    if job_state().0 {
        println!("already running");
        return 0;
    }
    let target = format!("gui/{}", uid());
    let status = Command::new("launchctl")
        .args(["bootstrap", &target])
        .arg(&path)
        .output();
    match status {
        Ok(output) if output.status.success() => {}
        Ok(_) => return report_error("start", "launchctl bootstrap failed", Some(&path)),
        Err(error) => return report_error("start", &error.to_string(), Some(&path)),
    }
    let (loaded, pid) = job_state();
    if !loaded {
        return report_error(
            "start",
            "job did not stay loaded after bootstrap",
            Some(&path),
        );
    }
    if let Some(pid) = pid {
        println!("running pid {pid}");
    } else {
        println!("running");
    }
    0
}
fn stop() -> i32 {
    if !job_state().0 {
        println!("already stopped");
        return 0;
    }
    let target = format!("gui/{}/{}", uid(), label());
    let status = Command::new("launchctl")
        .args(["bootout", &target])
        .output();
    match status {
        Ok(output) if output.status.success() => {
            println!("stopped");
            0
        }
        Ok(_) => report_error(
            "stop",
            "launchctl bootout failed",
            Some(&plist(&profile::home())),
        ),
        Err(error) => report_error("stop", &error.to_string(), Some(&plist(&profile::home()))),
    }
}
pub fn run(args: &[String]) -> i32 {
    match args.get(1).map(String::as_str) {
        Some("install") => install(),
        Some("assemble") if args.len() == 4 => {
            match assemble_bundle(Path::new(&args[2]), Path::new(&args[3])) {
                Ok(()) => 0,
                Err((message, path)) => report_error("assemble", &message, path.as_deref()),
            }
        }
        Some("start") => start(),
        Some("stop") => stop(),
        _ => {
            eprintln!("usage: yelo hud {{install|assemble|start|stop}}");
            2
        }
    }
}
pub(crate) fn check_usage(
    home: &Path,
    _: bool,
) -> Result<(&'static str, String), (String, Option<PathBuf>)> {
    let directory = home.join(".local/bin");
    for path in profile::dirs(&directory) {
        if path
            .file_name()
            .and_then(|s| s.to_str())
            .is_some_and(|s| s.starts_with("usage-hud-"))
            && setup::dotfiles_owned(&path)
        {
            return Ok((
                "owned-by-dotfiles",
                format!(
                    "symlink into {}: {}",
                    setup::dotfiles_root().display(),
                    path.display()
                ),
            ));
        }
    }
    Ok((
        "ok",
        format!("no usage-hud-* link in {}", directory.display()),
    ))
}
pub(crate) fn check_hud(
    home: &Path,
    _: bool,
) -> Result<(&'static str, String), (String, Option<PathBuf>)> {
    let plist_path = plist(home);
    if !plist_path.is_file() {
        return Ok(("missing", format!("absent: {}", plist_path.display())));
    }
    let binary_path = binary(home);
    if !binary_path.is_file() {
        return Ok(("missing", format!("absent: {}", binary_path.display())));
    }
    Ok(("ok", plist_path.display().to_string()))
}
