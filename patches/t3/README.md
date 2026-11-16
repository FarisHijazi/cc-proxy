# T3 external session support

`0001-observe-external-acp-sessions.patch` applies to official T3 Code nightly
`v0.1.10-nightly.20261116.4774`, commit
`16695c812fffe75e85f8cc3084d2c561aa3ca93f`. Apply `0002-wait-for-persisted-history.patch` and then
`0003-discover-external-sessions.patch`. The resulting source commit is
`d4b098aac07af7059484752f9dd23d3360a3491a`. The second patch waits for the
existing event pump to persist loaded history before sending the first thread
snapshot; load errors remain errors instead of becoming empty conversations.

Upstream already imports existing history. This patch adds selected-session
observation, native transcript identity, external Stop, and pending native
questions/permissions without creating a replacement agent or synthetic run.
Only providers explicitly advertising the versioned external-session capability
are observed when opened. Ordinary ACP providers retain their existing lifecycle.

The third patch fixes the gap between the import picker and the sidebar. An
explicit provider `autoImportProjectId` enables discovery inside the existing
server, with a 30-second delay after each completed pass. It creates deterministic thread records without loading
agents; selection imports history through the existing owner. Archived/deleted
views and temporarily offline sessions are preserved. The destination is local
view storage, not a mapping of remote workdirs onto local files.

Apply with `git am` from a clean worktree at the pinned base. Use T3's documented
toolchain and release scripts: `scripts/update-release-package-versions.ts`,
`vp run --filter t3 build`, `apps/server/scripts/cli.ts build-exe`, and
`scripts/build-cli-archive.ts`. The archive needs the resource-monitor bundle
from that exact official nightly. Restore the release manifests/lockfile after
packaging. Do not copy a new binary into an older runtime tree.

The Linux archive version is `0.1.10-ccfleet.20261117.1`; its SHA-256 is
`bbe91260598d46f5ecf955982dbcb3c3d3fb2418d0b4ae7c2c95f4d48ee91bc5`.
It is installed through the official installer and existing `t3 update` service
path. The user's stable-channel preference is unchanged. A later official update
may replace this patch; rebase/retest it or remove it when upstream covers these
behaviors. No second T3 service is required.

Validation: `check:changed` passed 760 tests, mobile compiler checks, affected
typechecks, formatting, lint, and dependency checks. The final request-withdrawal
delta passed the 57-test ACP lifecycle suite, focused lint, and server typecheck.
The history barrier passed its failing-before regression and stream-failure case,
then `check:changed` (112 tests and 27 affected typechecks) and the 57-test ACP suite.
Automatic discovery passed failing-before regressions, 39 focused WS/discovery
checks, and `check:changed` (62 related tests and 27 package typechecks).
The three exported patches reproduce the source tree exactly. The new archive
started in an isolated fresh home, served its UI, rejected anonymous protected
API requests, and exited cleanly.
The prior packaged runtime started against an isolated migrated data copy, served its
web client, rejected anonymous protected API access, and shut down cleanly.
Live acceptance evidence belongs in
[the fleet test matrix](../../docs/testing/fleet-e2e-matrix.md).

Before a live upgrade, back up the database with SQLite's online backup API,
settings, service configuration, and original runtime outside the updater's
version-pruning directory. The official nightly includes schema migrations;
rollback requires the matching pre-upgrade data backup, not just an older binary.
T3's `server-owner.sqlite` is an active ownership lock, contains no user data,
and must not be copied or unlinked while the service runs.
