"""The Usage HUD's data pipeline: `jello usage show`, `fetch`, and `doctor`.

`snapshot` reads local cache files only and never the network or the Keychain; `fetch` is
the only writer of the API cache family; `doctor` reads both and writes nothing. Every
module here takes its account census from `jello.profile.core` (law L2) and none of them
is imported by `jello.profile.core`.
"""
