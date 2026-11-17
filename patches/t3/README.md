# T3 fleet sessions and self-hosted sign-in

Apply the five patches in filename order with `git am` from a clean worktree at
official T3 Code nightly `v0.1.10-nightly.20261116.4774`, commit
`16695c812fffe75e85f8cc3084d2c561aa3ca93f`. The maintained source commit is
`da4e1eb262edb862fc08328431b9f2b0a403183a`.

| Patch | Purpose |
| --- | --- |
| `0001-observe-external-acp-sessions.patch` | Observe selected external sessions, preserve native transcript identity, and handle external Stop and pending native questions/permissions. |
| `0002-wait-for-persisted-history.patch` | Wait for the existing event pump to persist imported history before the first snapshot; preserve load failures as errors. |
| `0003-discover-external-sessions.patch` | Discover sessions into the sidebar through the existing server/import owners. |
| `0004-self-hosted-oidc.patch` | Authentik sign-in and renewable device sessions for the served web client and matching native client builds. |
| `0005-reconnect-oidc-session.patch` | Recover the existing browser connection after sign-in or renewal without a manual reload; cover cookie HTTP/WebSocket authorization and guarded RPC. |

External observation requires the versioned provider/client capability; ordinary
ACP providers retain their existing lifecycle. Provider `autoImportProjectId`
opts into discovery with a 30-second delay after each pass. Discovery creates
stable sidebar records without starting or resuming workers. Selection imports
history through the existing owner; archived/deleted views remain preserved.
The local destination project stores conversation views, not remote workdirs.

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

The Linux archive version is `0.1.10-ccfleet.20261117.3`; SHA-256:
`d6afc14aa37572e3177b1c87daede485af7859db027d8763cb153eeb7d33d3cb`.
Use the official installer to install the verified archive, then the exact cached
runtime's `t3 service install --base-dir <existing-t3-home>` to reconcile the
existing service. `service.env` and userdata stay with that home. This does not
require a second T3 deployment or a systemwide update policy.

T3's server does not update itself automatically. A manual official update can
replace these patches, including the code that enforces the OIDC configuration;
rebase and retest before updating. Keep the user's release-channel preference.

## Verification and recovery

The five exported patches reproduce the committed source tree exactly. The OIDC
integration `check:changed` passed 299 tests, 23 React Compiler checks and
typechecks across 41 affected packages. The reconnect correction passed 14 tests,
2 React Compiler checks and 9 package typechecks. Both runs passed formatting,
lint and dependency checks. Authentication coverage includes PKCE/state/redirect binding, exact identity validation,
revocation, concurrent refresh/replay, transient provider failure, persistence
failure and a requesting client disconnecting during rotation.

These source/build checks do not establish live acceptance. Packaged-runtime,
public ingress and browser sign-in results belong in the
[fleet test matrix](../../docs/testing/fleet-e2e-matrix.md).

Before activation, back up the database through SQLite's online backup API,
settings, service configuration and old runtime outside the updater's
version-pruning directory. Schema changes require the matching pre-upgrade data
backup for rollback, not just an older binary. T3's `server-owner.sqlite` is an
active ownership lock, contains no user data, and must not be copied or unlinked
while the service runs. Preserve running fleet workers during service maintenance.
