# The local backend joins the repository

The router on the proxy host serves a `local` backend (an OpenAI-compatible inference server) that existed only in another
session's scratch copy of the source. A build from git would have dropped it. That copy was the repository before
`b179822` plus the backend, and none of the backend's files changed in git since. So `src/config.rs`,
`src/providers/gemini/{mod,client}.rs`, `src/providers/gemini/translate/request.rs` and `tests/local_backend.rs` were
copied whole. The two small hunks in `src/registry.rs` (register `local`) and `src/server.rs` (its context window in
`/v1/models`) were reapplied by hand. The README section was ported with a neutral example model name. The built-in
default alias is now `local-model`. The host sets `CCP_LOCAL_MODEL` explicitly, so its alias is unchanged.

Afterwards the repository source matches the deployed source except for features git already had on top of the scratch
base (the Cursor Messages broker and the loopback Anthropic client). `cargo test -- --test-threads=1` passes.
