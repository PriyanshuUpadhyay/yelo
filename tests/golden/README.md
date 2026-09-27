# Goldens

The tests compare yelo against these files, so the Rust port is judged against the code it
replaced rather than against itself.

- `cli/*.json` are 36 CLI cases recorded from the Python yelo 0.5.11: argv, fixture HOME,
  exit code, stdout, stderr, and for writing commands a listing of HOME afterwards.
  `tests/golden.rs` runs each case and compares parsed JSON, so key order does not count.
  `YELO_RECORD_GOLDENS=1 cargo test --test golden` records them again.
- `list-claude.txt`, `list-codex.txt`, and `list-prime.txt` are the stdout of `list --cli
  <cli>` from the dotfiles `agent-profiles.py` that yelo was first ported from, run against
  the fixture layout in `tests/common/mod.rs`. `tests/cli.rs` compares against them.
- `usage-show.json` is the output of the bash and jq usage feed that `usage show` replaced,
  run against the usage fixture. That feed had no table, so `usage-show.txt` came from the
  binary and was reviewed by eye. `tests/usage_snapshot.rs` compares against both.

The last two sets were rendered once by Python scripts that went with Python in 0.6.0; git
history keeps them. Record or edit a golden only when a change to the output is intended,
never to make a failing test pass.
