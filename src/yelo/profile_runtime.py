"""Entry point for the installed, self-contained account selector."""

import argparse
import sys

from .profile import core
from .usage import snapshot


def main():
    if sys.argv[1:2] == ["model"]:
        try:
            print(core.claude_model(sys.argv[2:]))
        except core.ResolveError as error:
            print(str(error), file=sys.stderr)
            return error.code
        return 0
    parser = argparse.ArgumentParser(prog="account profiles")
    parser.add_argument("action", choices=("list", "menu", "resolve", "pick", "sessions"))
    parser.add_argument("--cli", choices=("claude", "codex"), required=True)
    parser.add_argument("--usage", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--limit", type=int, default=core.SESSIONS_LIMIT)
    parser.add_argument("--model")
    parser.add_argument("query", nargs="?")
    args = parser.parse_intermixed_args()
    if args.action == "resolve":
        if args.query is None:
            parser.error("resolve requires an account name")
        return core.command_resolve(args)
    if args.action == "sessions":
        return core.command_sessions(args)
    usage = None
    if args.usage or args.action in ("menu", "pick"):
        try:
            usage = snapshot.snapshot_rows(core.HOME)
        except snapshot.SnapshotError:
            pass
    handler = {"list": core.command_list, "menu": core.command_menu, "pick": core.command_pick}[args.action]
    return handler(args, usage)
