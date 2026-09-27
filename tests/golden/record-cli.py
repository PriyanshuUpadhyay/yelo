#!/usr/bin/env python3
"""Record the CLI goldens ONCE, from the Python yelo, before the Rust port replaces it.

Run it by hand only, from the checkout root, while `src/yelo` is still the Python package:

    uv run --with pytest python tests/golden/record-cli.py

Each case runs twice in fresh fixture homes; a case whose two runs differ is refused,
because a golden that moves on its own would fail the port for nothing. It writes only
tests/golden/cli/ and never touches the real HOME. Re-record only when a change to the
contract is intended, never to make a failing test pass.
"""

import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from cli_golden import CASES, run  # noqa: E402


def main():
    out = HERE / "cli"
    out.mkdir(exist_ok=True)
    for case in CASES:
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as root:
                runs.append(run(case, root))
        if runs[0] != runs[1]:
            raise SystemExit(f"{case[0]}: two runs differ, so it cannot be a golden")
        (out / f"{case[0]}.json").write_text(json.dumps(runs[0], indent=2, ensure_ascii=False) + "\n")
        print(f"{case[0]}: exit {runs[0]['exit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
