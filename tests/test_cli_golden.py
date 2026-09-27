"""The yelo binary (`YELO_CMD`, default target/debug/yelo) matches the CLI contract
in tests/golden/cli/."""

import json
import pathlib

import pytest

from cli_golden import CASES, run

GOLDEN = pathlib.Path(__file__).parent / "golden" / "cli"


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_matches_golden(case):
    expected = json.loads((GOLDEN / f"{case[0]}.json").read_text())
    assert run(case) == expected
