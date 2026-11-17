# T3 fleet sessions and self-hosted sign-in

Apply the eight patches in filename order with `git am` from a clean worktree at
official T3 Code nightly `v0.1.10-nightly.20261116.4774`, commit
`16695c812fffe75e85f8cc3084d2c561aa3ca93f`. The maintained source commit is
`556d84aa10429b8c0d76030e80bad7b6d106d683`.

| Patch | Purpose |
| --- | --- |
| `0001-observe-external-acp-sessions.patch` | Observe selected external sessions, preserve native transcript identity, and handle external Stop and pending native questions/permissions. |
| `0002-wait-for-persisted-history.patch` | Wait for the existing event pump to persist imported history before the first snapshot; preserve load failures as errors. |
| `0003-discover-external-sessions.patch` | Discover sessions into the sidebar through the existing server/import owners. |
| `0004-self-hosted-oidc.patch` | Authentik sign-in and renewable device sessions for the served web client and matching native client builds. |
| `0005-reconnect-oidc-session.patch` | Recover the existing browser connection after sign-in or renewal without a manual reload; cover cookie HTTP/WebSocket authorization and guarded RPC. |
| `0006-separate-native-engine-and-connection.patch` | Preserve native Claude Code/Codex identity, classify fleet as a discovery connection, gate native controls and keep metadata enrichment out of activity timestamps. |
| `0007-stabilize-discovery-and-sign-in.patch` | Negotiate identity on the catalog connection, recover a lost refresh response through the existing secret owner, and retain the displayed route's sidebar provider during OIDC callbacks. |
| `0008-show-remote-session-connections.patch` | Use the existing environment selector for Session connections, including saved remotes when the desktop local backend is disabled. |

External observation requires the versioned provider/client capability; ordinary
ACP providers retain their existing lifecycle. Provider `autoImportProjectId`
opts into discovery with a 30-second delay after each pass. Discovery creates
stable sidebar records without starting or resuming workers. Selection imports
history through the existing owner; archived/deleted views remain preserved.
The local destination project stores conversation views, not remote workdirs.

Fleet appears under Connections; the native owner supplies Claude Code or Codex
identity and observed model/capabilities. Existing opaque transport bindings stay
unchanged. Discovery and selected-session observation both negotiate the same
versioned extension; sparse catalog metadata does not replace richer selected
detail. Metadata enrichment preserves activity timestamps; normal startup
runtime-recovery events are separate from that guarantee.
Session connections uses the existing settings panel's environment selector,
so a remote-only desktop can configure its saved server without enabling a local
backend. Local network/WSL controls remain scoped to the app-owned backend.

OIDC reuses T3's environment, session, credential and persistence owners. It is
optional for other deployments; a public self-hosted deployment must require its
configuration explicitly. See [the operator guide](../../docs/self-hosted-t3-auth.md)
for Authentik, client compatibility and ingress configuration.

## Build and activate

Use T3's documented toolchain and release scripts:
`scripts/update-release-package-versions.ts`, `vp run --filter t3 build`,
`apps/server/scripts/cli.ts build-exe`, and `scripts/build-cli-archive.ts`.
The archive needs the resource-monitor bundle from the pinned official nightly.
Restore release manifests/lockfile after packaging. Never replace a binary inside
an older runtime directory.

The completed Linux x64 archive version is `0.1.10-ccfleet.20261117.6`;
SHA-256: `f150f7a927a794d25a2c47cf999ecb3d00ecff2b9b45bd3faf07701d8a03605c`.
Its release manifest records the maintained source commit above and no configured
hosted Connect service. The matching macOS ARM64 build has these SHA-256 checksums:

- DMG: `45d6a684468f4f778af9b666a5bdc2cb033ad722aae613a1619c5283f4ad788d`
- ZIP: `f707c6c06e908104fb7589aabd2d5f42b071450afc11dc07767046fd1bcfc4f4`

These artifact identities do not establish live deployment acceptance.
Use the official installer to install the verified archive, then the exact cached
runtime's `t3 service install --base-dir <existing-t3-home>` to reconcile the
existing service. `service.env` and userdata stay with that home. This does not
require a second T3 deployment or a systemwide update policy.

T3's server does not update itself automatically. A manual official update can
replace these patches, including the code that enforces the OIDC configuration;
rebase and retest before updating. Keep the user's release-channel preference.

## Verification and recovery

Replaying all eight patches from the pinned base reproduces the maintained
source tree `c26335aed41316ab41988f856477024bc200dfa5`. Source validation records
are separate runs, not a sum of unique tests:

| Change | Tests passed | Package typechecks | React Compiler checks |
| --- | ---: | ---: | ---: |
| Original OIDC integration | 299 | 41 | 23 |
| Browser reconnect correction | 14 | 9 | 2 |
| Native engine / connection separation (`fd7572e7`) | 825 in 44 files | 37 | 22 |
| Catalog negotiation, refresh receipt and callback layout (`97d15c9d`) | 22 in 4 files | 10 | 1 |
| Remote-only Session connections (`556d84aa`) | 26 in 2 files | 2 | 1 |

The identity run's initial test-import type error was corrected and all five
unfinished typecheck targets subsequently passed; the 825 tests and 22 compiler
checks had already passed. The .5 and .6 `check:changed` runs passed formatting,
lint and both dependency checks; the .5 run also validated GraphQL. The auth owner separately passed
50 focused tests covering the renewal change; that overlaps the integrated run.
Authentication coverage includes PKCE/state/redirect binding, exact identity
validation, revocation, concurrent refresh/replay, transient provider failure,
persistence failure and a requesting client disconnecting during rotation.
The catalog regression uses a real JSON-RPC subprocess and permits only
initialize/list. The callback regression reproduces Settings-to-sign-in
navigation in Chromium with the real root route and a delayed callback.
The remote-only regression renders the real Connections settings and selection
panel with a saved bearer environment and no primary environment; it failed on
the missing Session connections section before the fix and passed afterward.

These source/build checks do not establish live acceptance. Packaged-runtime,
public ingress and browser sign-in results belong in the
[fleet test matrix](../../docs/testing/fleet-e2e-matrix.md).

Before activation, back up the database through SQLite's online backup API,
settings, service configuration and old runtime outside the updater's
version-pruning directory. Schema changes require the matching pre-upgrade data
backup for rollback, not just an older binary. T3's `server-owner.sqlite` is an
active ownership lock, contains no user data, and must not be copied or unlinked
while the service runs. Preserve running fleet workers during service maintenance.
