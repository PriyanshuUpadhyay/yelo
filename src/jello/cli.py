"""The `jello` root command: one parser, five groups, one error shape.

Each group owns its own subcommand and registers it here. An ImportError raised while a
group loads is a broken install, so it surfaces as a traceback rather than as a command
that quietly goes missing.
"""

import argparse
import sys

from . import __version__, doctor, resume, setup, shell
from .profile import commands as profile_commands

# The order is the order in --help.
GROUPS = (profile_commands, resume, shell, setup, doctor)


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
