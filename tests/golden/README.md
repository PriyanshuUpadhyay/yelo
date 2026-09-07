# Golden `profile list` output

`list-claude.txt`, `list-codex.txt`, and `list-prime.txt` are the exact stdout of the
script yelo was ported from:

    /Users/me/dotfiles/home/.claude/scripts/agent-profiles.py list --cli <cli>
    sha256 f9e5dd08d80cb3c9cbb378b0ed6988ee1549411850b5f0efe157abcc1edb201d

run against the fixture account layout `tests/conftest.py::build_fixture_home` builds
(two claude profiles, two codex homes, one prime home) with the environment
`tests/conftest.py::fixture_env` sets: `HOME` moved to the fixture, the Keychain probe
pinned to `/usr/bin/false`, and no usage feed.

`tests/test_cli.py::test_list_matches_golden` compares `yelo profile list` against these
bytes, so the port is judged against the reference rather than against itself.

They were rendered once, by hand:

    uv run --with pytest python tests/golden/render.py

The renderer refuses to run when the reference script's digest has changed. Re-render only
when a change to the reference output is intended — never to make a failing test pass.
The tables carry no directory paths, so the bytes are the same on any machine.
