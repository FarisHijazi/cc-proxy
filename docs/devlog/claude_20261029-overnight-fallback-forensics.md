# Overnight fallback forensics

## Verified observations

Read-only traffic and client-transcript investigation, followed by the user's explicit request to remove Cursor from automatic fallback. No sessions or services restarted. No credential writes or refresh calls were performed by the diagnostic scripts.

At 2026-10-29 10:46 UTC, direct one-output-token `claude-opus-5-5` probes found:

| Runtime slot | Result |
| --- | --- |
| 1 | HTTP 429, weekly utilization 100%, reset November 1 at 08:00 UTC. |
| 2 | HTTP 429, weekly utilization 100%, reset November 1 at 12:00 UTC. |
| 3 | HTTP 401, access token revoked. Quota unknown. |
| 4 | HTTP 429, weekly utilization 100%, reset October 29 at 17:00 UTC. |
| 5 | HTTP 200, weekly utilization 89%, five-hour utilization 0%. |

Therefore not every Claude account is exhausted. Slots 3 through 5 contain inference-only setup tokens; usage/profile endpoint errors do not establish their inference availability. The dashboard was displaying old reply-header snapshots for them.

## Fixed: active setup token excluded

The deployed `account_inject.py` matches the local file SHA-256 `58aec4ffa460092b65cdca296dc23cbb792b7dbda1b7c771395b9fb8c0531626`.

`slot_token()` recognizes no-expiry/no-refresh setup tokens only inside its inactive-slot branch, lines 63-68. An active setup token takes lines 58-61, reaches the expiry check at line 73, and raises `ValueError`. `pool()` then omits that account for 300 seconds. The actual active identity is slot 5. Its direct inference probe succeeded, but a pinned request through the proxy selected Codex.

A deterministic diagnostic extracted the real `slot_token()` AST and supplied toy credential-store modules. It reproduced rejection when slot 5 was active and acceptance of the same token when inactive, without network or refresh. Existing `test_setup_tokens_are_used_as_is_and_never_refreshed` covers only the inactive case.

Correction: the setup-token check now follows selection of either live or backup credential data, before expiry handling. Backup refresh remains gated on no live access token. Regression coverage includes active and inactive setup tokens and confirms that expired active OAuth credentials never consume a refresh grant.

## Premature completion mechanism

The affected metrics session requested Opus 5.5, but its October 29 09:54-09:56 UTC responses came from `cursor/cursor:claude-opus-5`. A captured reply said it would inspect git history, appended a `toolu_...` identifier as plain text, emitted no `tool_use` block, and ended with `end_turn`. The client transcript records the same model and text-only completion.

Recorded flow `48dc33fb-6df7-44f3-894e-18de993c7b1e` was replayed locally with assertions for the responding provider, terminal reason, literal identifier, and absent tool-use block. They passed. This was not an agent deciding the task was complete: the tool request was not represented as executable protocol data.

Cursor's bridge recognizes only its XML tool recovery format. With no recovered call, `tool_bridge.rs:589-606` finalizes; `sse.rs:350-354` synthesizes `end_turn`. Cursor also accepts empty/incomplete upstream streams, so an outer HTTP 200 is not adequate success evidence.

Across the captured window October 28 18:00 through October 29 10:28 UTC, 119 Cursor replies all ended with `end_turn`, none had a tool-use block, 53 had no content blocks, and 21 contained a tool identifier as text. First Cursor fallback was October 28 21:41 UTC. Last recorded successful Codex Sol reply before the sustained overnight failure was October 29 02:23:47 UTC; explicit Codex HTTP 429 logs continued through 10:27 UTC. Codex returned successful replies again after the user's top-up.

An additional 108 inference HTTP 400 replies in that window contained invalid thinking signatures. These are separate from the text-only Cursor completion; successful thread-history resend refusals must not be counted as an outage.

## Authorized configuration change

Used the existing dashboard `POST /api/config` owner, preserving trigger switches and the threshold:

- Claude family: `accounts`, then `codex`.
- Codex family: `codex`, then `accounts`.

Removed Cursor targets from configured chains. Verified persisted configuration and zero recorded Cursor responses after 10:47 UTC. Installed Cursor tools and logins were not removed. The proxy service start timestamp is unchanged.

Detailed diagnostic scripts and evidence are private session scratch files, not committed traffic dumps. Evidence includes `account-quota-probes.jsonl`, `metrics-transcript-evidence.jsonl`, `verified-incident.txt`, `proxy-account-probes.jsonl`, and `reproduce_active_setup.py` in this session's scratchpad.

## Fix verification and approved deployment

The user explicitly approved fixing and deploying, including a proxy restart.

- Before the fix, the added active-token regression failed with `ValueError: cswap slot 3 has no usable token`; 37 cases passed and one failed.
- After the fix, all 38 injector tests passed, including an empty-HOME run.
- Full Python suite: 221 passed, one skipped. Existing dependency deprecation warnings remain.
- Independent read-only review approved the narrow two-file source/test diff.
- Production Python syntax compilation and `git diff --check` passed.
- Backed up the previous addon under `~/.local/state/claude-codex/before-active-setup-token-20261029T1055/`.
- Atomically installed only `account_inject.py`, preserving deployed mode 0700. New SHA-256: `6a02b8ab284b2f3e010b7f4425df8ebdd60419d797b974fe258899b547c5950c`.
- Restarted only `claude-account-proxy.service` at October 29 10:56:11 UTC. The dashboard and HQ remain active; HQ retains its original process and start time. No client sessions were restarted or given synthetic input.
- After restart, a pinned slot-5 request returned HTTP 200 from slot 5 with the actual `claude-opus-5-5` model and weekly utilization 89%.
- A second request using the metrics session's original account placeholder also fell through to slot 5 and returned the same actual Claude model, rather than Codex or Cursor.
- The probes intentionally used `max_tokens: 1`, so their `max_tokens` stop reason is expected and is not the incident's erroneous `end_turn`.
- Cursor remains excluded from both automatic fallback chains.

No commit or push was requested for this follow-up. Source, tests, and documentation remain local and the tested addon is deployed. The revoked slot still needs a replacement credential before it can serve requests; no credentials were changed. Existing signature-history errors and the disabled Cursor translator were not changed in this narrow fix.
