"""Report profile setup and HUD installation without changing files."""

import json
import os

from . import hud, setup

CHECKS = (*setup.STEPS, *hud.CHECKS)


def rows(home):
    return [(step.name, *step.check(home)) for step in CHECKS]


def run(args):
    from .cli import fail

    try:
        report = rows(os.path.expanduser("~"))
    except setup.SetupError as error:
        # A checkout that does not carry an asset a row compares against is a broken
        # install: one error line naming the asset, never a traceback (F26).
        return fail("doctor", str(error), error.path)
    if args.json:
        print(json.dumps(
            [{"step": name, "state": state, "detail": detail} for name, state, detail in report],
            indent=2,
        ))
    else:
        for name, state, detail in report:
            print(f"{name}\t{state}\t{detail}")
    return 1 if any(state == setup.MISSING for _, state, _ in report) else 0


def register(subparsers):
    parser = subparsers.add_parser(
        "doctor",
        help="report each setup step as ok, missing, or owned-by-dotfiles",
        description=__doc__.splitlines()[0],
    )
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(run=run)
    return parser
