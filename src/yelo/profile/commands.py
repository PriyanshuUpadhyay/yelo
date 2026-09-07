"""argparse wiring for the `profile` group.

The parser is the reference script's (agent-profiles.py:754-781) with `create` added and
the two flags the shell wrappers used to supply themselves: `sessions --cwd` replaces the
implicit process directory the zsh helper relied on.
"""

import os

from . import core, create
from ..usage import snapshot

CLI_CHOICES = ("claude", "codex")


def usage_rows():
    """The Usage HUD snapshot the usage column and the picker join against, read in process
    from the same cache files the HUD shows. A snapshot that cannot be taken at all leaves
    the column on its per-account cache fallback rather than failing the command."""
    try:
        return snapshot.snapshot_rows(core.HOME)
    except snapshot.SnapshotError:
        return None


def command_list(args):
    return core.command_list(args, usage_rows() if args.usage else None)


def command_menu(args):
    return core.command_menu(args, usage_rows())


def command_pick(args):
    return core.command_pick(args, usage_rows())


def command_sessions(args):
    """core.command_sessions narrows to the process directory, so --cwd moves it there."""
    if args.cwd:
        try:
            os.chdir(args.cwd)
        except OSError as error:
            from ..cli import fail

            return fail("profile sessions", str(error), args.cwd)
    return core.command_sessions(args)


def register(subparsers):
    parser = subparsers.add_parser(
        "profile",
        help="account profiles for claude and codex",
        description=core.__doc__.splitlines()[0],
    )
    group = parser.add_subparsers(dest="profile_command", required=True)

    def add(name, handler):
        sub = group.add_parser(name)
        sub.add_argument("--cli", choices=CLI_CHOICES, required=True)
        sub.set_defaults(handler=handler)
        return sub

    listing = add("list", command_list)
    listing.add_argument("--usage", action="store_true")
    listing.add_argument("--json", action="store_true")
    add("menu", command_menu).add_argument("--model", help="Claude startup model")
    # Sessions are a codex-only concept, so this one does not take the shared --cli choices.
    sessions = group.add_parser("sessions")
    sessions.add_argument("--cli", choices=("codex",), required=True)
    sessions.add_argument("--all", action="store_true")
    sessions.add_argument("--limit", type=int, default=core.SESSIONS_LIMIT)
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--cwd")
    sessions.set_defaults(handler=command_sessions)
    resolving = add("resolve", core.command_resolve)
    resolving.add_argument("query")
    resolving.add_argument("--json", action="store_true")
    picking = add("pick", command_pick)
    picking.add_argument("--json", action="store_true")
    picking.add_argument("--model", help="Claude startup model")
    creating = add("create", create.command_create)
    # The wrappers send `create --cli CLI [options] -- NAME`, so a name that starts with a
    # dash reaches create.py and gets the shell's invalid-name refusal. An absent name is
    # optional here for the same reason: create.py owns that usage line too.
    creating.add_argument("name", nargs="?")
    # A valueless --email keeps the shell's own "requires an address" refusal.
    creating.add_argument("--email", nargs="?", const="", default=None)
    creating.add_argument("--yes", "-y", action="store_true")

    parser.set_defaults(run=run)
    return parser


def run(args):
    return args.handler(args)
