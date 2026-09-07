"""argparse wiring for the `usage` group, and the one human view the reference never had.

`show` prints the HUD's rows, `fetch` refreshes them, `doctor` audits them. `--json` is
the exact array the HUD polls; without it the same rows render as a six-column table
(board A1), which is the only surface here with no reference to be judged against.
"""

from __future__ import annotations

import json

from .. import cli
from ..profile import core
from . import doctor, fetch, snapshot

HEADER = ("LABEL", "WINDOW", "PCT", "RESET", "STATE", "SOURCE")


def cell(row, key, suffix=""):
    value = row.get(key)
    return "-" if value is None else f"{value}{suffix}"


def render_table(rows):
    """One line per row, in snapshot order. A status row (offline, logged_out) has no
    window, percentage, reset, or source, so those cells read `-`."""
    body = [[row["label"], cell(row, "window"), cell(row, "pct", "%"),
             cell(row, "reset"), row["state"], cell(row, "source")] for row in rows]
    return core.render(list(HEADER), body)


def command_show(args):
    try:
        rows = snapshot.snapshot_rows(core.HOME)
    except snapshot.SnapshotError as error:
        return cli.fail("usage show", str(error), error.path)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    for line in render_table(rows):
        print(line)
    return 0


def register(subparsers):
    parser = subparsers.add_parser(
        "usage",
        help="usage meters for the claude and codex accounts",
        description=__doc__.splitlines()[0],
    )
    group = parser.add_subparsers(dest="usage_command", required=True)
    showing = group.add_parser("show", help="print every account's usage rows")
    showing.add_argument("--json", action="store_true")
    showing.set_defaults(handler=command_show)
    group.add_parser("fetch", help="refresh every account's API cache").set_defaults(
        handler=fetch.run
    )
    group.add_parser("doctor", help="audit the usage pipeline, read-only").set_defaults(
        handler=doctor.run
    )
    parser.set_defaults(run=run)
    return parser


def run(args):
    return args.handler(args)
