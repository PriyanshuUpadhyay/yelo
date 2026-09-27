#!/usr/bin/env python3
"""Re-record the CLI contract from the binary under test.

Run it by hand only, from the checkout root, after building the binary:

    uv run --with pytest python tests/golden/record-cli.py

Each case runs twice in fresh fixture homes; a case whose two runs differ is refused,
because a golden that moves on its own would fail the port for nothing. It writes only
tests/golden/cli/ and never touches the real HOME. Re-record only when a change to the
contract is intended, never to make a failing test pass.
"""

import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from cli_golden import CASES, run  # noqa: E402


def main():
    out = HERE / "cli"
    out.mkdir(exist_ok=True)
    for case in CASES:
        runs = []
        for _ in range(2):
            runs.append(run(case))
        if runs[0] != runs[1]:
            raise SystemExit(f"{case[0]}: two runs differ, so it cannot be a golden")
        (out / f"{case[0]}.json").write_text(json.dumps(runs[0], indent=2, ensure_ascii=False) + "\n")
        print(f"{case[0]}: exit {runs[0]['exit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
