#!/usr/bin/env python3
"""Render the `profile list` goldens ONCE, from the reference script.

The goldens are the output of the script jello was ported from, so the port is compared
against the thing it replaces rather than against itself. Run it by hand only:

    uv run --with pytest python tests/golden/render.py

It reads the reference script and writes tests/golden/list-<cli>.txt; it never writes
outside this directory and never touches the real HOME.
"""

import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from conftest import build_fixture_home, fixture_env  # noqa: E402

REFERENCE = pathlib.Path(
    "/Users/me/dotfiles/home/.claude/scripts/agent-profiles.py"
)
REFERENCE_SHA256 = "f9e5dd08d80cb3c9cbb378b0ed6988ee1549411850b5f0efe157abcc1edb201d"
CLIS = ("claude", "codex", "prime")


def main():
    digest = hashlib.sha256(REFERENCE.read_bytes()).hexdigest()
    if digest != REFERENCE_SHA256:
        raise SystemExit(f"reference script changed: {digest}")
    with tempfile.TemporaryDirectory() as tmp:
        home = build_fixture_home(os.path.join(tmp, "home"))
        env = fixture_env(home)
        for cli in CLIS:
            result = subprocess.run(
                [sys.executable, str(REFERENCE), "list", "--cli", cli],
                capture_output=True, text=True, env=env,
            )
            if result.returncode != 0:
                raise SystemExit(f"{cli}: exit {result.returncode}: {result.stderr}")
            HERE.joinpath(f"list-{cli}.txt").write_text(result.stdout)
            print(f"wrote list-{cli}.txt ({len(result.stdout)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
