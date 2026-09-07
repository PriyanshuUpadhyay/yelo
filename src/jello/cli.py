"""The Jello CLI: Claude and Codex profiles, usage, and UsageHUD."""

import argparse
import sys

from . import __version__, doctor, hud, maintenance, setup
from .profile import commands as profile_commands
from .usage import commands as usage_commands

# The order is the order in --help.
GROUPS = (profile_commands, usage_commands, setup, doctor, hud, maintenance)


def fail(command, message, path=None):
    """The one error shape: `jello: <command>: <message> (<path>)`, stderr, exit 1."""
    text = f"jello: {command}: {message}"
    if path is not None:
        text = f"{text} ({path})"
    print(text, file=sys.stderr)
    return 1


def build_parser():
    parser = argparse.ArgumentParser(
        prog="jello", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--version", action="version", version=f"jello {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in GROUPS:
        module.register(subparsers)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    handler = getattr(args, "run", None)
    if handler is None:
        return fail(args.command, "no handler registered")
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
