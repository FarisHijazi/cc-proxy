# Fleet acceptance evidence

This matrix separates real sessions from simulated failures. Passing a protocol
fixture does not establish that a particular Claude/Codex version or T3 UI handles
the same interaction. The latest candidate must retain source identities and must
never replace a worker or retry an uncertain write.

## Native engine and connection acceptance (2026-11-17)

The existing Linux service is on `0.1.10-ccfleet.20261117.6`, activated at
21:08:53 UTC from source `556d84aa10429b8c0d76030e80bad7b6d106d683`. Its 19
isolated and 12 public protection checks passed again; original bindings/history
remain preserved. The eight reproducible patches and archive checksum are in
[the patch README](../../patches/t3/README.md). The native engine checks below
were performed on .5 at 20:48–20:58 UTC. The only .6 production change is to show
Session connections on remote-only clients using the existing environment selector;
its real Chromium regression failed before and passed after the correction.
Fleet is a discovery connection; each attached conversation carries its validated
Claude Code or Codex engine, observed model and supported controls. Opaque routing
bindings remain unchanged. The adapter does not launch replacement native drivers.

| Check | Live evidence |
| --- | --- |
| Catalog identity | All 49 advertised routes carried valid native identity: 32 Claude Code and 17 Codex, including all three VMs, Mac and Remote Control routes. These are routes, not a claim of 49 unique processes. |
| Automatic enrichment | The existing discovery owner populated identities into existing sidebar records; no manual import or database write was used. Historical records absent from the live catalog remain preserved without guessed identities. |
| Binding/history preservation | All 96 original conversation/provider-thread bindings and message IDs survived; the later 98-record baseline also survived activation. The same source UUIDs and existing run IDs remained. |
| Ended Codex history | The exact selected archive retained its native message ID and text hash, original binding and zero new runs. All six native action capabilities were false. Its runtime reader is ready with no lastError; external unavailable maps to provider-thread error because the native worker is ended. |
| Desktop .6 acceptance | Matching ARM64 build installed with a reversible app/profile backup; ad-hoc signature and packaged payload checks passed. Actual desktop Settings showed CC fleet Available under Session connections, a Connected remote and Local environment off. Providers listed Codex, Claude and routed Claude without fleet. Existing Claude history displayed the exact prior continuation marker and Claude Code identity; an existing Codex history displayed Codex and gpt-6-astra. This final read check sent no new prompt. |
| Desktop .6 saved sign-in | Startup initially stalled in macOS Keychain decryption. After it resumed, the same grant renewed at 21:25:46.282 UTC and WebSocket reconnected at 21:25:46.709 UTC; the encrypted catalog saved and no local backend listener appeared. Grant counts stayed unchanged. The actual action that cleared the OS prompt was not observed. |
| Desktop .5 saved sign-in | Installed the signed local build and reopened after its previous access expired. The same native grant refreshed at 20:54:38.956 UTC, reconnected WebSocket at 20:54:39.315 UTC and saved credentials; no new sign-in grant or local backend was created. |
| Desktop .5 native identity | Actual Providers settings contained Codex, Claude and routed Claude, with fleet excluded. The existing Dev fixture displayed Claude Code and Model unknown because the owner supplied no model. |
| Desktop .5 continuation | Exactly one prompt through the native composer returned `NATIVE_ENGINE_READY_20261117`. Native transcript contains one user prompt and one assistant reply, with unchanged native UUID and T3 binding. Persisted source IDs/text match byte-for-byte and the session returned idle. Delivered runtime_info correctly identifies the Claude Code harness. |
| Final adapter regression command | The CI-equivalent ACP/MCP command passed 155 tests with one intentional opt-in live skip; Ruff passed. The actual reserved native continuation above is separate from that skip. |
| Packaged authentication | 19 isolated checks passed in a fresh home; the tracked child and process group stopped. |
| Public protection | All 12 public checks passed: UI/current asset redirect to Authentik; API and real WebSocket Upgrade reject anonymous, spoofed identity, forged loopback and invalid bearer requests. Auth metadata remains public and no-store. |
| Activation scope | Existing service updated through the official installer/service owner. Its cgroup contained three service processes and zero protected Claude/Codex workers. ACP activation changed only the existing instance command path; verification sent zero native actions and revoked its temporary local credential. |

Live testing first caught missing catalog capability negotiation: selected-session
loads requested native metadata, while management listings did not. The seventh
patch fixes the existing management handshake; its real JSON-RPC regression failed
before and passed afterward, including malformed metadata rejection and absence
of session load/start calls. Both failed activation attempts were rolled back and
retained privately before the successful .5 activation.

The same live work caught a lost native refresh response at 20:11 UTC, before .4
activation. A retry after the old five-second cache expired revoked the device
grant. The .5 owner keeps one exact, committed-successor receipt for 60 seconds in
the existing private SecretStore. Failing-before regressions cover response loss
after the client deadline, owner restart, expired replay and revocation. The full
focused auth suite passed 50 tests. A real desktop callback also exposed a
transient sidebar-provider crash while Settings remained mounted; a real Chromium
test reproduced it and passed after root layout selection followed committed route
matches. These regressions do not substitute for native client acceptance.

Metadata-only discovery did not reorder conversations. One selected archive's
activity timestamp changed during the verification because its existing attach
path emits a fresh historyLoaded event. Earlier server-start recovery also updated
two activity times without changing messages/runs. Those attachment/recovery
behaviors are distinct from identity enrichment and remain unchanged.

All six HQ readers moved to 0.9.7 using their existing service owners; ftower MCP
remained 0.9.5. Reader activation checks preserved 55 protected process identities
and 52 tmux panes. A later broad T3 census found two departed process identities;
one observed replacement was a search helper, and the old exits cannot both be
attributed conclusively. No native shutdown/input action was sent by this rollout;
do not claim every process in that long-lived census survived.

Private Dev evidence: `t3-identity-acp-live-proof-20261117.json`,
`t3-identity-preservation-20261117.json`,
`t3-identity5-metadata-preservation-20261117.json`,
`t3-identity-public-auth-20261117-5.json`,
`t3-identity-public-auth-20261117-6.json`, and the isolated .5/.6 reports under
`~/.local/state/cc-proxy/t3-external-identity-20261117/`. These observations are
separate from the earlier dated continuation, control and iPhone checks below.

## Self-hosted Authentik acceptance (2026-11-17)

At the earlier authentication stage, the existing service ran `0.1.10-ccfleet.20261117.3`, source
`da4e1eb262edb862fc08328431b9f2b0a403183a`; systemd reports activation at
18:51:14 UTC. The required OIDC configuration and port remain in the existing
`service.env`. That stage used the first five patches; the current maintained series and archive
checksum are recorded in [the patch README](../../patches/t3/README.md).

The first real browser attempt on `.2` exposed a failure that negative auth tests
had not found: Authentik sign-in and HTTP sidebar loading succeeded, but opening a
conversation reported the environment offline. A manual reload recovered it.
The fifth patch reconnects the existing primary supervisor when sign-in or renewal
changes its credentials; it does not create another connection owner. Its
regression exercises the actual supervisor lifecycle. A separate positive server
test covers a persisted OIDC browser cookie through HTTP/WebSocket authentication
and guarded RPC, then rejects it after grant revocation.

The corrected `.3` build passed an actual Chrome sequence: logout, Authentik
sign-in, callback, then opening the existing Dev discovery fixture **without a
manual reload**. Its exact `AUTO_DISCOVERY_READY_20261117` assistant message was
visible. Existing histories on the other two VMs also opened without the offline
error. Those final history checks were read-only; they did not send worker input.
This establishes browser sign-in and existing-session reading, not every native
control or every client surface.

| Check | Final `.3` evidence |
| --- | --- |
| Isolated packaged runtime | 19 checks passed in a fresh temporary home using the real OIDC configuration; tracked child and process group stopped. |
| Anonymous UI and current JavaScript asset | Public GET returned 302 to Authentik; Cloudflare reported DYNAMIC, including the exact deployed JS filename. |
| Protected HTTP and real WebSocket Upgrade | Public requests returned 401 for anonymous access, spoofed Authentik headers, forwarded-loopback claims and invalid bearer tokens. |
| Public auth metadata | OIDC config returned 200 with `enabled=true` and `no-store`; environment descriptor returned 200. These do not expose session content. |
| Browser Origin and CORS | Isolated packaged token/logout requests rejected missing, foreign and `null` Origins with 400. Preflight returned 204 with wildcard origin but no credential allowance. |
| Real logout and new sign-in | Read-only SQLite metadata shows old grant/session revoked at 18:54:24 UTC; new session issued at 18:54:49.708 UTC and WebSocket connected at 18:54:54.080 UTC. |
| Durable authorization | New grant/session are active, exactly linked, match the configured issuer/subject allowlist and carry 16 standard client scopes. No tokens or subject values were recorded in the evidence. |
| Automatic browser renewal on `.3` | Same Chrome grant renewed naturally at 19:08:52.567 UTC, extending access expiry to 19:23:52.567 UTC. The old session was revoked and authenticated WebSocket reconnected at 19:08:55.454 UTC. No manual reload/sign-in, forced expiry or grant manipulation was used. |
| Desktop native app | Matching `.3` macOS app completed its native OIDC callback and displayed the exact Dev discovery marker plus existing histories from both other VMs without auth errors. A separate Electron grant was issued at 19:00:20.183 UTC and connected over authenticated WebSocket at 19:00:24.110 UTC. Pending permission dialogs are not inferred from these read checks. |
| Automatic native renewal on `.3` | Same Electron grant renewed naturally at 19:14:30.723 UTC, extending access expiry to 19:29:30.723 UTC. Its old session was revoked and authenticated WebSocket reconnected at 19:14:31.282 UTC. No user reauthentication or forced expiry was used. |
| Native credential restoration | Quit/reopen required no sign-in prompt; the encrypted catalog read succeeded at 19:04:48 UTC and the Dev fixture history reopened. No second local backend/listener on port 3774 was observed. |
| Native composer continuation | One prompt to the reserved Dev toy produced exact assistant marker `DESKTOP_AUTH_READY_20261117` and returned idle around 19:06 UTC. All 11 Dev baseline workers retained their original PID/birth identities afterward. No ordinary user conversation received test input. |
| Cross-device history | The existing Chrome login, without reload, displayed exact `DESKTOP_AUTH_READY_20261117` from the native desktop prompt in the same Dev fixture. |
| Active Mac root | Actual Chrome opened native Codex root `01a1e076…`, matched the verified active root identity and displayed current conversation content without a syncing or auth error. No input was sent to that live root. |
| Ended Mac history | Mac HQ 0.9.6 and the updated ACP adapter loaded the exact ended diagnostic subagent’s saved message through the existing native reader. Chrome displayed that history without a syncing state. Native UUID remained `notLoaded`, current root remained active, and T3 run IDs stayed empty. The backend rejects every send/control; the .3 T3 UI still permitted an unsent draft and could enable Submit. The .4 identity UI subsequently disabled submission for this exact ended session. The verification draft was cleared without submission. |
| iPhone Safari | User completed Authentik sign-in on their iPhone and confirmed that existing VM conversations open. This is user-observed physical-device acceptance, not automation. Home Screen installation and stock native mobile OIDC are not claimed. |
| Worker identity audit at 18:55 UTC | One baseline PID differed; the corresponding route is an HQ scheduled routine. Journal records its existing reaper at 18:25:01 UTC and next scheduled spawn at 18:40:02 UTC, both before `.3` activation. The other 11 workers on that VM retained exact PID/birth identities. |

The routine audit found an existing 105-minute reaper, present since October 12,
and no HQ restart during the interval. The old PID-to-pane record is unavailable;
timing and the sole replacement support the correlation, so this is not a claim
that every original PID was unchanged. The audit sent no worker input.

The ended-history correction passed 529 HQ tests, 107 ACP tests and an independent
security review. Mac HQ alone moved to 0.9.6; the other five readers and ftower MCP
remain on 0.9.5. The Mac swap preserved 21 protected process identities and 31 tmux
panes. ACP activation changed only the existing provider command path through the
supported settings API, with no T3 restart or native worker action. Verification
waited for actual capability enrichment on the server-config stream: the immediate
provider refresh response is a readiness placeholder. The exact source message ID
and text hash matched T3’s persisted projection. Private evidence:
`~/.local/state/cc-proxy/t3-archived-history-live-proof-20261117.json` on Dev and
`~/.codex/tmp/fleet-mcp/archived-codex-096-post-t3-proof.json` on Mac.

A final read-only census at 19:27 UTC found 25 of the 28 original VM process
identities. Two VMs retained all 16 original processes; the third retained 9/12.
Besides the known scheduled routine, its oncall conversation resumed the same
UUID through the existing account router in a new process and remained busy.
The initiator is not known. A separate task process had ended without a replacement;
its exit cause is unknown. Both owner services on that VM retained their prior
PIDs/start times and zero restarts. That authentication stage performed no service or worker action
on that VM, and the audit did not restart anything. Do not equate a long-lived
census difference with a deployment-caused termination. Detailed identity evidence
remains private under `~/.codex/tmp/fleet-mcp/` on Mac.

Public probes ran at 18:57:24–26 UTC. The SQLite observation at 18:57:26 UTC is
metadata-only; `last_connected_at` is written by the authenticated WebSocket path.
Private Dev evidence is retained with restricted permissions under
`~/.local/state/cc-proxy/t3-oidc-acceptance-20261117/`:
`t3-public-auth-final3-20261117.json`,
`isolated-smoke-0.1.10-ccfleet.20261117.3.json`, and separate browser/Electron grant
snapshots `t3-auth-grants-20261117T190243Z.json` and
`t3-auth-grants-20261117T190916Z.json`. That observation confirms natural browser
rotation and reconnection; `t3-auth-grants-20261117T191439Z.json` subsequently
confirms independent Electron renewal and reconnection. Earlier
continuation/Stop and native-control results below retain their own versions and
dates; they are not silently relabelled as fresh `.3` tests.

## Sidebar and runtime identity recovery (2026-11-17)

The prior 40-route audit checked T3's import picker, not its sidebar. Only five
fleet threads had actually been imported. The third maintained T3 patch adds
explicitly configured automatic discovery to the existing server, with a
30-second delay after each pass, deterministic lazy imports and no extra service. The existing provider now
has a destination project configured; manual import is no longer required.

An initial manual bulk import restored the existing sidebar entries. Subsequent
production verification matched all 44 then-current catalog routes to sidebar
threads; that initial batch alone does not prove automatic discovery. All 21 interactive Claude workers across three VMs (9, 9, 3) were opened
through the actual thread-subscription path. Every current HQ message ID, role
and text matched the T3 projection; original route bindings, run IDs and process
PID/birth identities were preserved. No prompt, approval, interrupt or worker
restart was used. Existing T3 history is retained, so this verifies current
source membership rather than destructive history replacement.

HQ 0.9.4 fixes stale launch UUIDs after an in-process resume. The existing Claude
publisher's current UUID is accepted only after process birth/domain and tmux
window/pane validation. Six live workers had changed UUIDs; five had appeared
empty and one had returned a stale nonempty history. All 21 now return current,
nonempty history. The reader change passed 510 tests and Ruff; seven regressions
failed before the fix. The ACP title follow-up passed 91 tests and includes exact
tmux names to distinguish sessions sharing a project title.

The packaged T3 runtime passed an isolated startup/authentication smoke check.
The actual public browser completed Authentik Google sign-in and opened existing
Claude histories from each of the three VMs without sending input. At that earlier
stage, anonymous root/API/WebSocket probes redirected to Authentik; the final
OIDC boundary above instead returns 401 from protected API/WebSocket routes. A Cloudflare cache bypass scoped
to the T3 hostname also gates static assets that had previously been cached.

Private evidence: `t3-sidebar-live-proof-20261117.json`,
`vm-readiness-after-094-20261117.json`, and `t3-auth-audit-after-20261117.json`.
Automatic discovery/lazy history has failing-before regressions, 39 focused
WS/discovery checks, and a passing `check:changed` (62 related tests and 27 package
typechecks). A separate new Claude fixture was then launched through the existing
wrapper after a read-only baseline confirmed its absence. The background reconcile
created its sidebar record about 109 seconds after startup, without an import RPC.
Opening it loaded the exact native UUID and both source messages, including the
exact assistant marker. It had zero synthetic runs before and after opening;
server traces link creation to `AcpRegistrySessions.reconcile`. All original
workers still present at the fixture baseline retained their PID/birth identity.
Evidence: `t3-auto-discovery-real-new-session-20261117.json`. The test also exposed
a separate Remote Control alias for the same conversation and slow fleet listing;
those observations are distinct from the passing automatic-discovery assertion.

## Earlier protocol regression baseline

`tests/account_acp_test.py`, `tests/account_acp_transport_test.py`, and
`tests/fleet_acp_live_test.py`: **87 passed, 1 skipped; live acceptance is opt-in**.
The ordinary live test is skipped without its manifest. The existing CI workflow
also runs the 22 relay checks, with pinned protocol dependencies and Ruff.
The complete CI command passed **109 tests, 1 skipped**, with Ruff clean. That test requires
an explicit private manifest of owner-reserved live fixtures. The harness itself
passes against the isolated protocol owner, including refusal of a second attempt.

The transport suite starts the actual ACP SDK subprocess and a temporary loopback
HQ/MCP fixture on the development host. It exercises real JSON-RPC serialization,
HTTP, SDK permissions and forms; it does not invoke a real Claude/Codex model.

| Scenario | Candidate regression or protocol evidence | Fresh real-session acceptance |
| --- | --- | --- |
| Original-session continuation | One write; original opaque route; reply after own input and confirmed idle | Passed on reserved Claude and Codex sessions on Dev and second Linux VM; exact reply markers and original identity retained |
| Initial history and reload | Source message IDs, distinct identical messages, growth, reload | Actual ACP initial source IDs/text and post-turn reload passed on all four fixtures; updated T3 verification is separate |
| Passive external activity | Explicit opt-in state notifications; initial/changed running, idle, requires-action, unavailable | Actual public T3 browser displayed current Mac commentary and existing second Linux VM Codex history/activity without sending either a prompt |
| Active Stop | One interrupt; waits for a subsequent positive idle observation | Passed Dev Claude, Dev Codex, second Linux VM Codex. second Linux VM Claude interruption reached the original process; an idle-status bug required the owner fix described below |
| External Stop | No local prompt required; rearmed only after confirmed stop then new work | Actual T3 stopped an externally started Claude question: one Stop, cancelled card, native idle, zero local runs and no answer sent |
| Stop with an open control | Actual stdio proves owner interrupt, observed request withdrawal, then exact transport cancellation; no answer posted | Passed actual Codex approval already pending before ACP attachment: one Stop, request withdrawn, client callback cancelled, idle, no file created |
| Permission allow and deny | Actual ACP request_permission round trip; exact pending native request | Both passed on actual Codex CommandExecution requests already pending before attach; denied file absent, allowed exact reserved file created |
| Question choices, Other and multi-select | Native oneOf/array forms and original question IDs; actual stdio custom/array answers | Actual Claude multi-select Blue + custom Purple passed through ACP; T3 accepted Other=Purple on a preexisting native question. Codex-specific question UI remains untested |
| Question accept, decline and cancel | Actual elicitation/create session-scoped forms; declined requests stay unanswered | Actual Claude Decline preserved the same unanswered request; T3 Other acceptance returned the exact native reply; external Stop withdrew a separate pending question |
| Request already pending before attach | Actual stdio permission allow/deny and question accept/decline/cancel with no local prompt | Actual Codex Allow, Deny and Stop; actual Claude question Decline; none required an ACP prompt |
| Terminal menu choices | Unit coverage for exact option, current revision, decline suppression | Pending real Claude menu |
| Stale revision | Confirmed pre-delivery rejection can refresh with a new receipt ID | Isolated fault injection; production faults intentionally not induced |
| Definitive rejection | Releases local prompt fence; requires an explicit new attempt | Isolated fault injection |
| Unknown delivery | Lost receipt after fixture accepted write; no resend; pending fence retained | Isolated fault injection |
| Offline and reconnect | Observer reports unavailable; no write retry; explicit reload reads same source | Isolated HTTP outage and SDK process reconnect |
| Dead or restarted worker | Actual stdio/HTTP owner faults return 404/409 after a running prompt; observer reports unavailable, old route cannot reload, no retarget or resend | Isolated protocol faults; no real user worker termination |
| Bounded history | Explicit recent-history notice when owner marks truncation | Current Mac Codex read is not truncated; large real history not exercised |
| Cross-thread identity | Native IDs and exact request correlation, no text-based durable aliases | Both T3 projects matched all 40 current MCP routes with no missing/extra/duplicate IDs; real Mac/second Linux VM imports and toy reload retained their original sources |
| Unsupported native dialog | Visible error and no permission grant | No real dialog attempted |

The bridge accepts text prompts. Image/file input, arbitrary terminal execution,
provider/model changes, and unsupported native dialogs are not advertised as
supported fleet operations.

## Fresh real-session evidence (2026-11-16)

Host aliases and host-bearing marker filenames are redacted below; `*` denotes
the host component in private evidence filenames.

The actual ACP SDK launched the final bridge subprocess against reserved live
sessions. These are live model/process checks, distinct from the simulated owner
used for deterministic faults. No real user conversation received test input.

| Reserved fixture | Continuation and source history | Active Stop | Identity |
| --- | --- | --- | --- |
| Dev Claude | Exact `ACP_DEV_CLAUDE_REPLY_20261116_A`, reload retained source IDs | `cancelled` after positive idle, history reload | Original UUID `0e9ecc6d…`, PID 3528213 |
| Dev Codex | Exact `ACP_DEV_CODEX_REPLY_20261116_A`, reload retained source IDs | `cancelled` after positive idle, history reload | Original UUID `01a1e467…`, PID 3421781 |
| second Linux VM Claude | Exact `ACP_*_CLAUDE_REPLY_20261116_A`, source history/reload | Interrupt delivered once; original wait timed out on stale status, then owner 0.9.2 verified idle without another Stop | Original UUID `206a953e…`, PID 1346627 |
| second Linux VM Codex | Exact `ACP_*_CODEX_REPLY_20261116_A`, reload retained source IDs | `cancelled` after positive idle, history reload | Original UUID `01a1e46a…`, PID 1376171 |

The second Linux VM Claude failure was an older prompt hook overriding a newer exact
publisher idle observation. HQ 0.9.2 fixed that precedence and preserved the same
worker. This is recorded as a failed original observation followed by verified
recovery, not a passing original Stop future. A stopped Claude prompt can also
remain restored as a local input draft. The canonical empty-prompt guard refuses
to overwrite that draft; test cleanup must identify only the exact reserved draft.

Actual native controls were exercised separately from prompt continuation:

- Codex pending approval Stop: `ACP_*_CODEX_PERMISSION_DENY_20261116_A.stop-result.json`.
  The client received the existing permission, sent no answer, then confirmed native
  request withdrawal, exact ACP callback cancellation and idle after one Stop.
- Codex explicit Deny: `ACP_*_CODEX_PERMISSION_DENY_20261116_B.result.json`.
  Native 0.169 offered `cancel` rather than `decline`; HQ 0.9.3 now uses that offered
  rejection. The request ended and the reserved file was absent.
- Codex Allow once: `ACP_*_CODEX_PERMISSION_ALLOW_20261116_A.result.json`.
  Only the exact inspected command was accepted; its reserved file contained the
  expected marker. No persistent exec-policy amendment was granted.
- Claude question Decline: `ACP_*_CLAUDE_QUESTION_20261116_A.decline-result.json`.
  One real form was received, no native answer was sent, and the same request stayed
  waiting. The SDK harness explicitly enables the elicitation feature it advertises;
  a regression reproduces the disabled-feature failure and verifies a real SDK answer.

The Claude fixture's Remote Control connection had archived itself after earlier
owner timeouts. One guarded `/remote-control` on the same reserved CLI recovered it.
Its exact publisher bridge ID and seven original transcript UUIDs establish the
native linkage; title/workdir similarity was never used as identity evidence.

Public MCP separately completed one authenticated Dev Codex send/read round trip:
`MCP_DEV_CODEX_REPLY_20261116_A`, receipt
`ca56c79bdb47416a954fee1a9042b5bf`, same native UUID and original PID, ending idle.
That check used a machine bearer and is not Claude.ai browser/OAuth evidence.

A separate **actual Claude.ai browser/OAuth** run used the existing Authentik
connector to list Dev Codex, the current Mac Codex conversation, and the live
second Linux VM Codex conversation. It read only the reserved Dev toy, sent exactly one
`CLAUDE_AI_CODEX_OK_20261116_A` prompt, and read the exact assistant reply with
idle status. The real Mac/second Linux VM conversations received metadata-only reads
and no test input. Tool approvals were one-time choices. Browser proof is retained
privately at `~/.codex/tmp/fleet-mcp/claude-ai-codex-20261116.png` on the Mac.

Current ACP artifacts are on Dev under
`~/.local/state/cc-proxy/fleet-acp-e2e-20261116/`. Each write creates an exclusive
attempt record first; result records are written only after checks complete.
A missing result is not treated as proof that a write was never delivered.

## Deployed T3 acceptance (2026-11-16)

On 2026-11-16, the existing T3 service ran `0.1.10-ccfleet.20261116.2`, built from the two
patches in `patches/t3/`. No replacement Claude or Codex worker was launched.

- The final discovery audit matched all 40 current opaque MCP routes in both T3
  projects, with no missing, extra or duplicate IDs and four passing cursor checks.
  Counts change as sessions start and stop; the earlier 41-route audit also passed.
  Evidence: `t3-discovery-audit-20261116T144911Z.json` under the Dev state directory.
- T3 imported the current Mac Codex conversation and existing second Linux VM Codex
  conversation with 77 and 44 messages at import, zero synthetic runs, and no
  prompt or Stop sent to either. Actual Chrome through the public Authentik route
  and T3 pairing displayed both transcripts and live activity. While loading the
  second Linux VM transcript, the UI showed `Loading messages...` and disabled submission.
- An actual T3 command/subscription round trip produced
  `T3_SEAMLESS_OK_20261116_B` with one visible native user message and one assistant
  reply. Reconnect retained both. One subsequent T3 Stop cancelled its counter,
  observed native idle and preserved Dev Claude PID 3528213/birth 18791571.
  Evidence: `t3-seamless-live-b/{marker,reload,busy_stop}-result.json`.
- T3 imported a Claude question already waiting before attachment, showed its
  choice card with zero local runs, accepted one custom `Purple` answer, and
  persisted `ACP_*_CLAUDE_QUESTION_20261116_A: Purple` before native idle.
  Evidence: `t3-native-question-proof/answer-result.json`.
- A separate real ACP multi-select answer produced
  `ACP_*_CLAUDE_MULTI_20261116_A: Blue, Purple`; the native request disappeared
  and the original session returned idle.
- T3 then stopped a separately seeded pending native question through its normal
  `thread.stop` command. The exact request became cancelled, no answer was posted,
  native status became idle, and no local run was created. Original second Linux VM Claude
  PID 1346627/start time remained unchanged. Evidence:
  `t3-external-question-stop/result.json`.

The original first T3 marker was rejected by the canonical local-draft guard;
it was not delivered or silently retried. Only that exact owned test draft was
cleared before the separately identified successful marker. The later Stop left
the canonical prompt empty. Real user drafts remain protected.

The final **actual public Chrome composer** check also passed: one prompt produced
`T3_BROWSER_OK_20261116_A`. Reload retained exactly one visible user message and
one exact assistant reply. The session ended idle, with the original Dev Claude
PID 3528213/birth 18791571. Evidence: `t3-browser-proof-20261116.json` on Dev and
`t3-browser-reply-20261116.ax.txt` in the Mac evidence directory. The screenshot
capture for this final reply was blank, so it is not counted as visual proof;
the live UI accessibility state and independent native/T3 projections were checked.
The earlier public history screenshots remain valid.

The public browser test exposed an existing ingress failure: the shared tunnel's
second connector in CT 108 stayed connected to Cloudflare while its LAN forwarding
path to ftower timed out. ftower selected Tailscale for return traffic to that CT.
Only the unusable connector was stopped and placed in an opt-in Compose `standby`
profile in the ingress source repository. The existing ftower connector continues
serving the same tunnel; public Authentik gates remain intact. No host network,
firewall, DNS, or other service was changed to work around it.

The implementation at cc-proxy commit `5fe1ace` passed both Full checks and Fleet
MCP/ACP jobs and the Nix flake build. The Nix-pinned compiler requires `fetch_update`,
so its deprecation allowance is scoped to the existing atomic sequence helper.

## Existing real evidence

These observations predate the expanded candidate and are retained as a baseline,
not substituted for its acceptance:

- Claude.ai authenticated through Authentik, listed and read the Dev Claude toy,
  sent `CLAUDE_AI_MCP_OK_20261115`, and read the exact assistant reply from the
  original process.
- Public MCP queued a message while that toy was busy and interrupted it through
  its original owner. The transcript recorded interruption and the queued reply;
  its process identity stayed the same.
- Official T3 0.1.8 with the earlier ACPv1 bridge persisted the exact
  `T3_STOCK_FIXED_OK_20261116` reply once and delivered it through the normal T3
  subscription. A native T3 Stop interrupted the reserved counter and preserved
  PID 3528213. This was API/subscription evidence, not browser reload evidence.
- A separate read-only inventory matched all 9 Dev and 7 second Linux VM Claude
  conversations against MCP and both T3 project lists. It proved discovery, not
  continuation on every conversation.

Existing native T3 artifacts are under Dev
`~/.local/state/cc-proxy/t3-stock-fixed-acceptance-20261116.json` and adjacent
attempt records. New live runs use `tests/fleet_acp_live_test.py`; each manifest
step creates an exclusive attempt file before writing and a result file only
after identity, response, and history checks pass. A failed attempt is never
silently retried.

## Access boundaries checked

On 2026-11-16, anonymous POSTs to canonical and legacy public MCP returned 401,
as did an invalid bearer. The advertised protected-resource metadata names the
existing Authentik issuer and OAuth scopes. The dashboard API still redirects to
Authentik. Public T3 at `https://t3code.fhijazi.com/` and its API redirect anonymous
requests to Authentik; the private T3 API separately rejects anonymous access with
401. The existing Mac SSH forward remains available at `http://127.0.0.1:3773`.
These checks were repeated after the 0.9.3 HQ rollout and final T3 upgrade.
Actual Chrome authenticated at the public T3 address and loaded the workspace.
The previously failing `http://<fleet-node>:8377/` also loaded its session list in
Chrome after refreshing the existing Tailscale profile; no new listener was added.
