# Fleet acceptance evidence

This matrix separates real sessions from simulated failures. Passing a protocol
fixture does not establish that a particular Claude/Codex version or T3 UI handles
the same interaction. The latest candidate must retain source identities and must
never replace a worker or retry an uncertain write.

## Current candidate checks

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

The existing T3 service runs `0.1.10-ccfleet.20261116.2`, built from the two
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

The native command/subscription tests above are distinct from browser-composer
testing. Public browser authentication, history loading and passive updates have
passed; the final browser-composer send/reply check is still pending.

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
