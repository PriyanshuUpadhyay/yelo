"""`jello doctor`: what is installed, judged from the filesystem alone.

One row per setup step, `step<TAB>state<TAB>detail`, where the state is `ok`, `missing`, or
`owned-by-dotfiles`. Exit 1 only when a step is `missing`: `owned-by-dotfiles` is a correct
answer during the cutover, not a fault.

Doctor calls only the `check` half of each step, so it never creates, writes, or repairs
anything (law L4). The verdict pattern is stow-drift-check.py's: ask the filesystem where
the target actually points, never a marker file or setup's own record of what it did.
"""

import json
import os

from . import setup


def rows(home):
    return [(step.name, *step.check(home)) for step in setup.STEPS]


def run(args):
    report = rows(os.path.expanduser("~"))
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
