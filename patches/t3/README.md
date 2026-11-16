# T3 external session support

`0001-observe-external-acp-sessions.patch` applies to official T3 Code nightly
`v0.1.10-nightly.20261116.4774`, commit
`16695c812fffe75e85f8cc3084d2c561aa3ca93f`. Apply `0002-wait-for-persisted-history.patch` next. The resulting source commit is
`bbcfa1652bf4edcf68d2c72cb1c4e2e521d97456`. The second patch waits for the
existing event pump to persist loaded history before sending the first thread
snapshot; load errors remain errors instead of becoming empty conversations.

Upstream already imports existing history. This patch adds selected-session
observation, native transcript identity, external Stop, and pending native
questions/permissions without creating a replacement agent or synthetic run.
Only providers explicitly advertising the versioned external-session capability
are observed when opened. Ordinary ACP providers retain their existing lifecycle.

Apply with `git am` from a clean worktree at the pinned base. Use T3's documented
toolchain and release scripts: `scripts/update-release-package-versions.ts`,
`vp run --filter t3 build`, `apps/server/scripts/cli.ts build-exe`, and
`scripts/build-cli-archive.ts`. The archive needs the resource-monitor bundle
from that exact official nightly. Restore the release manifests/lockfile after
packaging. Do not copy a new binary into an older runtime tree.

The Linux archive version is `0.1.10-ccfleet.20261116.2`; its SHA-256 is
`d99dca6779d95566bdbe03fba6321a6de6e654f6bb0148d0af24dbff69e9998d`.
It is installed through the official installer and existing `t3 update` service
path. The user's stable-channel preference is unchanged. A later official update
may replace this patch; rebase/retest it or remove it when upstream covers these
behaviors. No second T3 service is required.

Validation: `check:changed` passed 760 tests, mobile compiler checks, affected
typechecks, formatting, lint, and dependency checks. The final request-withdrawal
delta passed the 57-test ACP lifecycle suite, focused lint, and server typecheck.
The history barrier passed its failing-before regression and stream-failure case,
then `check:changed` (112 tests and 27 affected typechecks) and the 57-test ACP suite.
The packaged runtime started against an isolated migrated data copy, served its
web client, rejected anonymous protected API access, and shut down cleanly.
Live acceptance evidence belongs in
[the fleet test matrix](../../docs/testing/fleet-e2e-matrix.md).

Before a live upgrade, back up the database with SQLite's online backup API,
settings, service configuration, and original runtime outside the updater's
version-pruning directory. The official nightly includes schema migrations;
rollback requires the matching pre-upgrade data backup, not just an older binary.
T3's `server-owner.sqlite` is an active ownership lock, contains no user data,
and must not be copied or unlinked while the service runs.
