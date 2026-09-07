"""C16-C18 and matrix M3: `jello hud` builds the app, installs it, and runs it.

Every test runs the real command against a temporary HOME with fake `swift`, `codesign`,
`launchctl`, and `jello` executables first on `PATH`. Nothing compiles, nothing is signed,
and launchd is never asked anything: the fake `launchctl` keeps the loaded label in a file,
which is enough to walk every cell of matrix M3 and is the only way to prove the start and
stop flows without touching the live domain (law L3).

`JELLO_HUD_LABEL` pins a throwaway label, `JELLO_HUD_PACKAGE` points at a stub package the
fake `swift` writes into, and `JELLO_DOTFILES_ROOT` points at a fake dotfiles tree, so the
ownership refusal is decided by a symlink the test made rather than by this machine.
"""

import os
import plistlib
import stat

import pytest

import jello as jello_package
from test_setup import env_for, run_jello, tree_digest  # noqa: F401 - shared helpers

LABEL = "io.github.priyanshuupadhyay.jello-hud-test"
BUNDLE_RELATIVE = ("Applications", "UsageHUD.app")
UID = os.getuid()

# Each fake writes one tab-separated line per invocation, so a recorded argv survives a
# path with a space in it and the calls stay in order.
RECORDER = 'printf \'%s\\t\' "$@" >> "$LOG"; printf \'\\n\' >> "$LOG"\n'

FAKE_SWIFT = """#!/bin/sh
LOG="$JELLO_TEST_SWIFT_LOG"
""" + RECORDER + """
package=""
while [ $# -gt 0 ]; do
  case "$1" in
    --package-path) package="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$package" ] || exit 2
mkdir -p "$package/.build/release"
printf 'fake UsageHUD binary\\n' > "$package/.build/release/UsageHUD"
chmod 755 "$package/.build/release/UsageHUD"
exit 0
"""

FAKE_CODESIGN = """#!/bin/sh
LOG="$JELLO_TEST_CODESIGN_LOG"
""" + RECORDER + """exit 0
"""

# `print` answers from the state file, `bootstrap` writes the label the plist names into
# it, and `bootout` removes it -- the smallest thing that behaves like a launchd domain.
FAKE_LAUNCHCTL = """#!/bin/sh
LOG="$JELLO_TEST_LAUNCHCTL_LOG"
""" + RECORDER + """
state="$JELLO_TEST_LAUNCH_STATE"
case "$1" in
  print)
    label="${2##*/}"
    if [ -f "$state" ] && [ "$(cat "$state")" = "$label" ]; then
      printf 'service = {\\n\\tstate = running\\n\\tpid = 4242\\n}\\n'
      exit 0
    fi
    printf 'Could not find service "%s"\\n' "$label" >&2
    exit 113
    ;;
  bootstrap)
    if [ -n "$JELLO_TEST_BOOTSTRAP_FAIL" ]; then
      printf 'Bootstrap failed: 5: Input/output error\\n' >&2
      exit 5
    fi
    # launchd took the job and it left again: bootstrap succeeds, the state file stays
    # empty, so the next `print` cannot find the label.
    [ -n "$JELLO_TEST_BOOTSTRAP_VANISH" ] && exit 0
    base="${3##*/}"
    printf '%s' "${base%.plist}" > "$state"
    exit 0
    ;;
  bootout)
    rm -f "$state"
    exit 0
    ;;
esac
exit 9
"""


def executable(path, text):
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def hud(tmp_path):
    """A temporary HOME, a fake dotfiles tree, a stub Swift package, and a PATH whose
    first entry holds the four executables `jello hud` is allowed to reach."""
    home = tmp_path / "home"
    home.mkdir()
    dotfiles = tmp_path / "dotfiles"
    (dotfiles / "home").mkdir(parents=True)
    package = tmp_path / "package"
    package.mkdir()
    (package / "Package.swift").write_text("// swift-tools-version:5.9\n")

    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable(binaries / "swift", FAKE_SWIFT)
    executable(binaries / "codesign", FAKE_CODESIGN)
    executable(binaries / "launchctl", FAKE_LAUNCHCTL)
    # Never run: `hud install` only needs to find it, to write JELLO_BIN into the plist.
    executable(binaries / "jello", "#!/bin/sh\nexit 0\n")

    logs = tmp_path / "logs"
    logs.mkdir()

    class Hud:
        def __init__(self):
            self.home = home
            self.dotfiles = dotfiles
            self.package = package
            self.jello_binary = binaries / "jello"
            self.bundle = home.joinpath(*BUNDLE_RELATIVE)
            self.binary = self.bundle / "Contents" / "MacOS" / "UsageHUD"
            self.info = self.bundle / "Contents" / "Info.plist"
            self.plist = home / "Library" / "LaunchAgents" / (LABEL + ".plist")
            self.swift_log = logs / "swift"
            self.codesign_log = logs / "codesign"
            self.launchctl_log = logs / "launchctl"
            self.state = logs / "launch-state"
            # The PATH the last `run` handed the command, which install copies into the plist.
            self.path = None

        def run(self, *argv, **overrides):
            env = env_for(home, dotfiles, **overrides)
            env["PATH"] = f"{binaries}:{env['PATH']}"
            env["JELLO_HUD_LABEL"] = LABEL
            env["JELLO_HUD_PACKAGE"] = str(package)
            env["JELLO_TEST_SWIFT_LOG"] = str(self.swift_log)
            env["JELLO_TEST_CODESIGN_LOG"] = str(self.codesign_log)
            env["JELLO_TEST_LAUNCHCTL_LOG"] = str(self.launchctl_log)
            env["JELLO_TEST_LAUNCH_STATE"] = str(self.state)
            env.update(overrides)
            self.path = env["PATH"]
            return run_jello(list(argv), env)

        def calls(self, log):
            if not log.exists():
                return []
            return [line.split("\t")[:-1] for line in log.read_text().splitlines()]

        def digest(self):
            return tree_digest(home)

    return Hud()


def test_install_bundle_and_plist(hud):
    """C16: one build, one signed bundle, one LaunchAgent, and a second run that says so."""
    result = hud.run("hud", "install")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "bundle rebuilt", "plist written", "next: jello hud start"
    ]
    assert hud.calls(hud.swift_log) == [
        ["build", "-c", "release", "--package-path", str(hud.package)]
    ]

    assert hud.binary.read_text() == "fake UsageHUD binary\n"
    assert stat.S_IMODE(hud.binary.stat().st_mode) == 0o755
    assert plistlib.loads(hud.info.read_bytes()) == {
        "CFBundleName": "UsageHUD",
        "CFBundleExecutable": "UsageHUD",
        "CFBundleIdentifier": LABEL,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": jello_package.__version__,
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHighResolutionCapable": True,
    }

    # The signature is applied to the staged sibling, before the swap, so a codesign
    # failure can never replace a good bundle with an unsigned one.
    (signed,) = hud.calls(hud.codesign_log)
    assert signed[:5] == ["--force", "--sign", "-", "--identifier", LABEL]
    staged = signed[5]
    assert os.path.dirname(staged) == str(hud.home / "Applications")
    assert os.path.basename(staged).startswith(".UsageHUD.app.new.")
    assert not os.path.exists(staged), "the staged bundle was renamed into place"

    assert plistlib.loads(hud.plist.read_bytes()) == {
        "Label": LABEL,
        "ProgramArguments": [str(hud.binary)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(hud.home / "Library" / "Logs" / "jello-hud.out.log"),
        "StandardErrorPath": str(hud.home / "Library" / "Logs" / "jello-hud.err.log"),
        # launchd gives the job no PATH, so the absolute launcher and the installing
        # shell's PATH (a fetch resolves `codex` through it) are both carried across.
        "EnvironmentVariables": {"JELLO_BIN": str(hud.jello_binary), "PATH": hud.path},
    }
    assert hud.calls(hud.launchctl_log) == [], "install never calls launchctl"

    before = hud.plist.read_bytes(), hud.plist.stat().st_mtime_ns
    again = hud.run("hud", "install")
    assert again.returncode == 0, again.stderr
    assert again.stdout.splitlines() == [
        "bundle rebuilt", "plist unchanged", "next: jello hud start"
    ]
    assert (hud.plist.read_bytes(), hud.plist.stat().st_mtime_ns) == before


@pytest.mark.parametrize("target", ["plist", "bundle"])
def test_install_refuses_dotfiles_owned(hud, target):
    """C17 and M3 rows 4 and 5: a target dotfiles owns is reported, never replaced, and
    the refusal comes before the build -- a compile the command will not use is waste."""
    if target == "plist":
        owned = hud.dotfiles / "home" / "Library" / "LaunchAgents" / (LABEL + ".plist")
        owned.parent.mkdir(parents=True)
        owned.write_bytes(plistlib.dumps({"Label": LABEL}))
        link, message = hud.plist, "LaunchAgent is owned by dotfiles"
    else:
        owned = hud.dotfiles / "home" / "Applications" / "UsageHUD.app"
        owned.mkdir(parents=True)
        link, message = hud.bundle, "bundle is owned by dotfiles"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(owned)
    before = tree_digest(hud.dotfiles)

    result = hud.run("hud", "install")

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"jello: hud install: {message} ({link})\n"
    assert hud.calls(hud.swift_log) == [], "no build for a target we will not write"
    assert hud.calls(hud.codesign_log) == []
    assert os.path.islink(link), "the link itself is untouched"
    assert tree_digest(hud.dotfiles) == before


def test_start_stop_matrix(hud):
    """C18 and matrix M3: the start and stop columns, in the order a user meets them."""
    missing = hud.run("hud", "start")
    assert missing.returncode == 1
    assert missing.stderr == f"jello: hud start: LaunchAgent not installed ({hud.plist})\n"

    absent_stop = hud.run("hud", "stop")
    assert absent_stop.returncode == 0
    assert absent_stop.stdout == "already stopped\n"

    assert hud.run("hud", "install").returncode == 0

    started = hud.run("hud", "start")
    assert started.returncode == 0
    assert started.stdout == "running pid 4242\n"
    assert ["bootstrap", f"gui/{UID}", str(hud.plist)] in hud.calls(hud.launchctl_log)

    again = hud.run("hud", "start")
    assert again.returncode == 0
    assert again.stdout == "already running\n"

    stopped = hud.run("hud", "stop")
    assert stopped.returncode == 0
    assert stopped.stdout == "stopped\n"
    assert ["bootout", f"gui/{UID}/{LABEL}"] in hud.calls(hud.launchctl_log)

    assert hud.run("hud", "stop").stdout == "already stopped\n"


def test_start_reports_a_failed_bootstrap(hud):
    """M3: launchd refusing the job is the error shape, not a silent `running`."""
    assert hud.run("hud", "install").returncode == 0

    result = hud.run("hud", "start", JELLO_TEST_BOOTSTRAP_FAIL="1")

    assert result.returncode == 1
    assert result.stderr == f"jello: hud start: launchctl bootstrap failed ({hud.plist})\n"
    assert result.stdout == ""


def test_start_reports_a_job_that_does_not_stay_loaded(hud):
    """M3 and board D8: bootstrap 0 is not enough -- `launchctl print` decides."""
    assert hud.run("hud", "install").returncode == 0

    result = hud.run("hud", "start", JELLO_TEST_BOOTSTRAP_VANISH="1")

    assert result.returncode == 1
    assert result.stderr == (
        f"jello: hud start: job did not stay loaded after bootstrap ({hud.plist})\n"
    )
    assert result.stdout == ""
    assert ["bootstrap", f"gui/{UID}", str(hud.plist)] in hud.calls(hud.launchctl_log)


def test_install_refuses_a_package_it_cannot_find(hud):
    """R10: the one error line, naming the path, when the checkout has no Swift package."""
    missing = hud.home / "no-package"

    result = hud.run("hud", "install", JELLO_HUD_PACKAGE=str(missing))

    assert result.returncode == 1
    assert result.stderr == f"jello: hud install: swift package not found ({missing})\n"
    assert hud.calls(hud.swift_log) == []


def test_install_copies_prebuilt_bundle(hud, tmp_path):
    """The Homebrew path: the .app is already compiled, so install copies it and never
    reaches `swift build`."""
    prebuilt = tmp_path / "prebuilt" / "UsageHUD.app"
    (prebuilt / "Contents" / "MacOS").mkdir(parents=True)
    executable(prebuilt / "Contents" / "MacOS" / "UsageHUD", "prebuilt UsageHUD binary\n")
    (prebuilt / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleName": "UsageHUD"}))

    result = hud.run("hud", "install", JELLO_HUD_BUNDLE=str(prebuilt))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"bundle installed from {prebuilt}", "plist written", "next: jello hud start"
    ]
    assert hud.calls(hud.swift_log) == [], "a prebuilt bundle is never compiled again"
    assert hud.binary.read_text() == "prebuilt UsageHUD binary\n"
    assert plistlib.loads(hud.info.read_bytes()) == {"CFBundleName": "UsageHUD"}


def test_install_refuses_missing_prebuilt_bundle(hud, tmp_path):
    """A JELLO_HUD_BUNDLE with no executable inside is the one error line, naming it."""
    empty = tmp_path / "empty.app"
    empty.mkdir()

    result = hud.run("hud", "install", JELLO_HUD_BUNDLE=str(empty))

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"jello: hud install: prebuilt bundle not found ({empty})\n"
    assert hud.calls(hud.swift_log) == []
    assert not hud.bundle.exists()


def test_plist_records_explicit_launcher(hud):
    """A wrapper install names its own stable path in JELLO_BIN; the plist takes it as
    given, rather than resolving the launcher that happened to run."""
    result = hud.run("hud", "install", JELLO_BIN="/opt/x/bin/jello")

    assert result.returncode == 0, result.stderr
    document = plistlib.loads(hud.plist.read_bytes())
    assert document["EnvironmentVariables"]["JELLO_BIN"] == "/opt/x/bin/jello"
