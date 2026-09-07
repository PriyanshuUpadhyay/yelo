#!/usr/bin/env python3
"""Render the `usage show` goldens ONCE, the JSON one from the reference script.

`usage-show.json` is the output of the bash+jq data feed yelo's snapshot was rewritten
from, so the rewrite is compared against the thing it replaces rather than against itself.
`usage-show.txt` is yelo's own table: the reference has no human mode (board A1), so that
one is rendered from yelo and reviewed by eye. Run it by hand only:

    cd /Users/me/work/jello/wt/main
    uv run --with pytest python tests/golden/render-usage.py

It builds the W1 fixture HOME in a temporary directory, writes only into this directory,
and never touches the real HOME. It refuses to run when the reference script's digest has
changed: re-render only when a change to the reference output is intended, never to make a
failing test pass.
"""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from conftest import build_usage_home, usage_env  # noqa: E402

REFERENCE = pathlib.Path("/Users/me/dotfiles/home/.local/bin/usage-hud-data")
REFERENCE_SHA256 = "16b0ce2742c47b6be3cfdcf7c648a522432a7d5f8358a6ccc68269a52abb74a1"
AGENT_PROFILES = "/Users/me/dotfiles/home/.claude/scripts/agent-profiles.py"
SOURCE = HERE.parent.parent / "src"


def reference_environment(home):
    """The reference asks agent-profiles.py for the account rows and its sibling
    usage-hud-fetch for the fetch capabilities; CODEX_BIN points at nothing, so the codex
    capability is false, which is what `shutil.which` answers in process too."""
    environment = usage_env(home)
    environment["USAGE_HUD_AGENT_PROFILES"] = AGENT_PROFILES
    return environment


def main():
    digest = hashlib.sha256(REFERENCE.read_bytes()).hexdigest()
    if digest != REFERENCE_SHA256:
        raise SystemExit(f"reference script changed: {digest}")
    with tempfile.TemporaryDirectory() as tmp:
        now = int(time.time())
        home = build_usage_home(os.path.join(tmp, "home"), now)
        reference = subprocess.run(
            [str(REFERENCE), "--json"],
            capture_output=True, text=True, env=reference_environment(home),
        )
        if reference.returncode != 0:
            raise SystemExit(f"reference: exit {reference.returncode}: {reference.stderr}")
        rows = json.loads(reference.stdout)
        HERE.joinpath("usage-show.json").write_text(json.dumps(rows, ensure_ascii=False,
                                                               indent=2) + "\n")
        print(f"wrote usage-show.json ({len(rows)} rows)")

        environment = usage_env(home)
        environment["PYTHONPATH"] = str(SOURCE)
        table = subprocess.run(
            [sys.executable, "-m", "yelo.cli", "usage", "show"],
            capture_output=True, text=True, env=environment,
        )
        if table.returncode != 0:
            raise SystemExit(f"yelo usage show: exit {table.returncode}: {table.stderr}")
        HERE.joinpath("usage-show.txt").write_text(table.stdout)
        print(f"wrote usage-show.txt ({len(table.stdout)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
