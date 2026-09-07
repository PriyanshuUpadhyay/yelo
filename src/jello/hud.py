"""`jello hud`: build the Usage HUD app and run it as a LaunchAgent.

Three commands, one target each:

  install  `swift build -c release`, assemble ~/Applications/UsageHUD.app, ad-hoc sign it,
           and write ~/Library/LaunchAgents/<label>.plist. It never calls launchctl, so
           the whole install path is provable under a temporary HOME and starting the job
           stays a separate, explicit action.
  start    bootstrap the LaunchAgent into gui/<uid>; already running is success.
  stop     boot it out; already stopped is success.

launchd hands the job no user PATH, so the plist carries the absolute path of the jello
launcher in JELLO_BIN and the app reaches `jello usage show --json` through it.

A target another package owns is never overwritten. Ownership is setup's rule and setup's
code: a symlink whose realpath lands inside ~/dotfiles. `CHECKS` hands `jello doctor` the
same two verdicts, re-derived from the filesystem alone -- doctor never calls launchctl,
so its answer cannot depend on whether the job happens to be up.

JELLO_HUD_LABEL and JELLO_HUD_PACKAGE relocate the label and the Swift package for tests
and for the rehearsal; JELLO_DOTFILES_ROOT relocates the ownership root, as in setup.
JELLO_HUD_BUNDLE names an already-built .app to copy instead of compiling, and JELLO_BIN
names the launcher to record: a Homebrew install builds the app once and reaches jello
through a stable wrapper, not through the versioned Cellar path sys.argv[0] would show.
"""

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile

from . import __version__, setup

APP_NAME = "UsageHUD"
DEFAULT_LABEL = "io.github.priyanshuupadhyay.jello-hud"
LABEL = os.environ.get("JELLO_HUD_LABEL") or DEFAULT_LABEL
# The dotfiles job. jello never addresses it; it only reads its plist path for the verdict.
LEGACY_LABEL = "work.example.usage-hud"
BUNDLE_RELATIVE = os.path.join("Applications", APP_NAME + ".app")
AGENTS_RELATIVE = os.path.join("Library", "LaunchAgents")
LOGS_RELATIVE = os.path.join("Library", "Logs")
LOCAL_BIN_RELATIVE = os.path.join(".local", "bin")
USAGE_LINK_PREFIX = "usage-hud-"
MINIMUM_MACOS = "14.0"
WRITTEN, UNCHANGED = "written", "unchanged"


class HudError(Exception):
    """A refusal that becomes the CLI error shape; `path` is the target it names."""

    def __init__(self, message, path=None):
        super().__init__(message)
        self.path = path


# --- paths -----------------------------------------------------------------------------

def package_path():
    """The Swift package inside this checkout. The editable install is the only supported
    install, so `apps/UsageHUD` sits two directories above this module."""
    override = os.environ.get("JELLO_HUD_PACKAGE")
    if override:
        return override
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "apps", APP_NAME)


def bundle_path(home):
    return os.path.join(home, BUNDLE_RELATIVE)


def binary_path(home):
    return os.path.join(bundle_path(home), "Contents", "MacOS", APP_NAME)


def plist_path(home):
    return os.path.join(home, AGENTS_RELATIVE, LABEL + ".plist")


def legacy_plist_path(home):
    return os.path.join(home, AGENTS_RELATIVE, LEGACY_LABEL + ".plist")


def dotfiles_owned(path):
    """setup's ownership rule, unchanged: the target itself is a symlink into ~/dotfiles."""
    return os.path.islink(path) and setup.into_dotfiles(path)


def launcher_path():
    """The absolute `jello` launchd must run, because it inherits no PATH. JELLO_BIN wins:
    a wrapper install knows its own stable path, where `sys.argv[0]` would name a versioned
    one. Otherwise `sys.argv[0]` is the entry point uv installed when the command was typed;
    a module run (`python -m jello.cli`) leaves a different basename there, so PATH answers
    instead."""
    explicit = os.environ.get("JELLO_BIN")
    if explicit:
        return explicit
    candidate = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(candidate) == "jello":
        return candidate
    found = shutil.which("jello")
    if found:
        return found
    raise HudError("cannot locate the jello launcher for JELLO_BIN")


# --- documents -------------------------------------------------------------------------

def info_plist_document():
    """LSUIElement keeps the app out of the Dock: it is a menu-bar surface with no window
    of its own. The identifier is the label, so the signature, the bundle, and the job all
    name the same thing."""
    return {
        "CFBundleName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": LABEL,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "LSUIElement": True,
        "LSMinimumSystemVersion": MINIMUM_MACOS,
        "NSHighResolutionCapable": True,
    }


def plist_document(home):
    # launchd gives the job no user PATH, and a fetch resolves `codex` (and any other CLI)
    # only through the PATH `jello hud install` ran with.
    environment = {"JELLO_BIN": launcher_path()}
    if os.environ.get("PATH"):
        environment["PATH"] = os.environ["PATH"]
    return {
        "Label": LABEL,
        "ProgramArguments": [binary_path(home)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": os.path.join(home, LOGS_RELATIVE, "jello-hud.out.log"),
        "StandardErrorPath": os.path.join(home, LOGS_RELATIVE, "jello-hud.err.log"),
        "EnvironmentVariables": environment,
    }


# --- launchd ---------------------------------------------------------------------------

def job_target():
    return f"gui/{os.getuid()}/{LABEL}"


def parse_pid(text):
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "pid":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


def job_state(home=None):
    """(loaded, pid) as launchd reports it. The label already carries the domain, so `home`
    is unused; it stays optional because `usage.doctor` still passes it. A launchctl that
    cannot run at all is the same answer as a job that is not there: nothing is loaded."""
    try:
        result = subprocess.run(
            ["launchctl", "print", job_target()], capture_output=True, text=True
        )
    except OSError:
        return False, None
    if result.returncode != 0:
        return False, None
    return True, parse_pid(result.stdout)


# --- install ---------------------------------------------------------------------------

def build(package):
    """Release build, with swift's own output left on the terminal: a compile error is the
    user's to read, not something to summarize into one line."""
    if not os.path.isfile(os.path.join(package, "Package.swift")):
        raise HudError("swift package not found", package)
    try:
        result = subprocess.run(["swift", "build", "-c", "release", "--package-path", package])
    except OSError as error:
        raise HudError(str(error), package) from None
    if result.returncode != 0:
        raise HudError("swift build failed", package)


def sign(bundle):
    try:
        result = subprocess.run(
            ["codesign", "--force", "--sign", "-", "--identifier", LABEL, bundle]
        )
    except OSError as error:
        raise HudError(str(error), bundle) from None
    if result.returncode != 0:
        raise HudError("codesign failed", bundle)


def swap(final, staged):
    """Two renames, never a write in place: a running app keeps its mapped binary because
    the inode survives a rename, where copying over it would truncate the file underneath
    the live process."""
    retired = None
    if os.path.lexists(final):
        retired = f"{final}.old.{os.getpid()}"
        shutil.rmtree(retired, ignore_errors=True)
        os.rename(final, retired)
    os.rename(staged, final)
    if retired is not None:
        shutil.rmtree(retired, ignore_errors=True)


def stage_bundle(home):
    """(final, staged): an empty sibling of ~/Applications/UsageHUD.app to build inside, so
    nothing half-built is ever reachable at the final path."""
    final = bundle_path(home)
    parent = os.path.dirname(final)
    os.makedirs(parent, exist_ok=True)
    staged = tempfile.mkdtemp(dir=parent, prefix=f".{APP_NAME}.app.new.")
    os.chmod(staged, 0o755)
    return final, staged


def assemble_bundle(home, package):
    """Assemble the bundle in a sibling directory, sign it there, then swap it in."""
    source = os.path.join(package, ".build", "release", APP_NAME)
    if not os.path.isfile(source):
        raise HudError("swift build produced no binary", source)
    final, staged = stage_bundle(home)
    try:
        contents = os.path.join(staged, "Contents")
        os.makedirs(os.path.join(contents, "MacOS"))
        executable = os.path.join(contents, "MacOS", APP_NAME)
        shutil.copy2(source, executable)
        os.chmod(executable, 0o755)
        with open(os.path.join(contents, "Info.plist"), "wb") as handle:
            plistlib.dump(info_plist_document(), handle)
        sign(staged)
        swap(final, staged)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return final


def copy_bundle(home, prebuilt):
    """Install an .app somebody else already built -- Homebrew compiles it once, at
    `brew install` time -- through the same stage, sign, swap path as a fresh build."""
    if not os.path.isfile(os.path.join(prebuilt, "Contents", "MacOS", APP_NAME)):
        raise HudError("prebuilt bundle not found", prebuilt)
    final, staged = stage_bundle(home)
    try:
        shutil.copytree(prebuilt, staged, symlinks=True, dirs_exist_ok=True)
        sign(staged)
        swap(final, staged)
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    return final


def write_plist(home, document):
    """`written` or `unchanged`; identical bytes are not rewritten, so a second install
    leaves the file's mtime alone as well as its content."""
    path = plist_path(home)
    wanted = plistlib.dumps(document)
    try:
        with open(path, "rb") as handle:
            if handle.read() == wanted:
                return UNCHANGED
    except OSError:
        pass
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=f".{LABEL}.plist.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(wanted)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return WRITTEN


def install(args):
    # cli imports this module to register the group, so the error shape is imported here
    # rather than at the top -- the same cycle setup.run and profile.commands break.
    from .cli import fail

    home = os.path.expanduser("~")
    plist, bundle = plist_path(home), bundle_path(home)
    try:
        # Both refusals come before the build: a target dotfiles owns costs no compile.
        if dotfiles_owned(plist):
            raise HudError("LaunchAgent is owned by dotfiles", plist)
        if dotfiles_owned(bundle):
            raise HudError("bundle is owned by dotfiles", bundle)
        document = plist_document(home)
        prebuilt = os.environ.get("JELLO_HUD_BUNDLE")
        if prebuilt:
            copy_bundle(home, prebuilt)
            print(f"bundle installed from {prebuilt}")
        else:
            package = package_path()
            build(package)
            assemble_bundle(home, package)
            print("bundle rebuilt")
        print(f"plist {write_plist(home, document)}")
    except HudError as error:
        return fail("hud install", str(error), error.path)
    except OSError as error:
        return fail("hud install", str(error))
    print("next: jello hud start")
    return 0


# --- start and stop --------------------------------------------------------------------

def start(args):
    from .cli import fail

    home = os.path.expanduser("~")
    plist = plist_path(home)
    if not os.path.isfile(plist):
        return fail("hud start", "LaunchAgent not installed", plist)
    loaded, _ = job_state()
    if loaded:
        print("already running")
        return 0
    try:
        result = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist],
            capture_output=True, text=True,
        )
    except OSError as error:
        return fail("hud start", str(error), plist)
    if result.returncode != 0:
        return fail("hud start", "launchctl bootstrap failed", plist)
    # A bootstrap that returns 0 is not proof: launchd accepts the job and the process can
    # still fail on its first run, so `launchctl print` decides whether `start` succeeded.
    loaded, pid = job_state()
    if not loaded:
        return fail("hud start", "job did not stay loaded after bootstrap", plist)
    print("running" if pid is None else f"running pid {pid}")
    return 0


def stop(args):
    from .cli import fail

    home = os.path.expanduser("~")
    loaded, _ = job_state()
    if not loaded:
        print("already stopped")
        return 0
    try:
        result = subprocess.run(
            ["launchctl", "bootout", job_target()], capture_output=True, text=True
        )
    except OSError as error:
        return fail("hud stop", str(error), plist_path(home))
    if result.returncode != 0:
        return fail("hud stop", "launchctl bootout failed", plist_path(home))
    print("stopped")
    return 0


# --- doctor steps ----------------------------------------------------------------------

def check_usage(home):
    """The four ~/.local/bin/usage-hud-* commands are dotfiles' until the owner cuts over,
    and a link into dotfiles is the whole verdict. There is no `missing` answer here: jello
    does not install those links, so their absence is not a fault."""
    directory = os.path.join(home, LOCAL_BIN_RELATIVE)
    try:
        names = sorted(
            entry.name for entry in os.scandir(directory)
            if entry.name.startswith(USAGE_LINK_PREFIX)
        )
    except OSError:
        names = []
    for name in names:
        path = os.path.join(directory, name)
        if dotfiles_owned(path):
            return setup.OWNED, f"symlink into {setup.dotfiles_root()}: {path}"
    return setup.OK, f"no {USAGE_LINK_PREFIX}* link in {directory}"


def check_hud(home):
    """The dotfiles LaunchAgent answers first: while it is there the HUD is still dotfiles'
    whether or not the jello pair also exists, which is exactly the cutover state."""
    legacy = legacy_plist_path(home)
    if dotfiles_owned(legacy):
        return setup.OWNED, f"symlink into {setup.dotfiles_root()}: {legacy}"
    plist = plist_path(home)
    if not os.path.isfile(plist):
        return setup.MISSING, f"absent: {plist}"
    binary = binary_path(home)
    if not os.path.isfile(binary):
        return setup.MISSING, f"absent: {binary}"
    return setup.OK, plist


# `apply` is None: these two are reported, never installed -- `hud install` compiles, and a
# compile does not belong in `jello setup` (board D9).
CHECKS = (
    setup.Step("usage", None, check_usage),
    setup.Step("hud", None, check_hud),
)


def register(subparsers):
    parser = subparsers.add_parser(
        "hud",
        help="build the Usage HUD app and run it as a LaunchAgent",
        description=__doc__.splitlines()[0],
    )
    commands = parser.add_subparsers(dest="hud_command", required=True)
    for name, handler, help_text in (
        ("install", install, "build the app, assemble the bundle, write the LaunchAgent"),
        ("start", start, f"bootstrap {LABEL} into this user's launchd domain"),
        ("stop", stop, f"boot {LABEL} out of this user's launchd domain"),
    ):
        commands.add_parser(name, help=help_text).set_defaults(run=handler)
    return parser
