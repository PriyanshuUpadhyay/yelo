"""The yelo under test (`YELO_CMD`, default the Python package) answers every case in
tests/golden/cli/ exactly as the Python yelo did when the goldens were recorded."""

import json
import pathlib

import pytest

from cli_golden import CASES, run

GOLDEN = pathlib.Path(__file__).parent / "golden" / "cli"


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_matches_golden(case):
    expected = json.loads((GOLDEN / f"{case[0]}.json").read_text())
    assert run(case) == expected
