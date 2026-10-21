# Engineering handoff — Signal HomeBot (Claude Code + Home Assistant)

**Status:** 0.5.0 hardened prototype; **NOT deployment-ready** until the on-device checklist in DOCS.md passes.  
**Prepared:** 2026-10-21 · **Updated:** 2026-10-21 (0.5.0, see section 0 and CHANGELOG.md)  
**Intended next developer:** Codex (or a human engineer) working **on this repository**.  
**Important distinction:** The deployed bot should **run Claude Code**, not Codex. Codex is only the development/review assistant for this hardening effort.

## 0. Status after the 0.4.0/0.5.0 hardening passes

Phases A–C are done and Phase D is partly done; CHANGELOG.md has details.
Sections 2–3 below describe the **0.3.0 starting point** and are kept for
history. Risks 1, 2, 3 and 6 are addressed in code and covered by tests. Risks
4 and 5 are verified against upstream source and the pinned binaries, but not
against live Signal or HA traffic. Risks 7 and 8 are partly addressed. Remaining
work:

- Run the on-device checklist (DOCS.md). Nothing has touched a real Signal
  account, the HA built-in MCP server, HA REST writes or the Supervisor yet.
- Container separation (risk 7): addressed in 0.5.0 within the single
  add-on container: signal-cli (`signal`), the bot (`homebot`) and a Claude
  broker plus Claude (`node`) run as separate users without capabilities or
  shared secrets (DOCS.md → Process isolation). Still to confirm on HAOS:
  Supervisor's security options must allow `setpriv`.
- Trusted accounts: 0.5.0 also requires safety-number-verified identity keys
  (`setup_mode: trust`) and refuses to post in the group unless its roster is
  exactly bot + two users.
- ha-mcp: 0.5.0 refuses connections not in Read Only Mode. A `/readonly`
  alias URL still reveals the instance's admin secret, so a separate read-only
  instance remains the recommendation.
- Policy: 0.5.0 loads `/config/policy.json` (add-on config folder); edits
  need a restart, not a rebuild.

## 1. Objective and scope

Build an inexpensive, self-hosted Home Assistant OS (HAOS, amd64) add-on for a household Signal assistant:

- The bot has its **own Signal identity**, registered as a **primary device in signal-cli** using a spare phone number (no Android device required).
- Exactly **two authorized Signal Account IDs (ACIs)** can DM it; it also participates in **one allowlisted Signal group** containing those two users.
- It receives and **stores authorized group messages even when nobody addresses it**, so an explicitly triggered response has context. Passive messages must **not** trigger AI calls or Home Assistant actions.
- Trigger Claude on DMs and group messages explicitly addressed via `/ha`, `!ha`, or `@HomeBot`. The bot should respond in the originating conversation.
- Claude Code (`claude -p`) uses **subscription authentication** where permitted/available. There must be no accidental fallback to usage-billed API credentials. Apply provider terms/limits; do not assume a personal subscription authorizes servicing multiple users.
- The bot retrieves live HA information from the **Home Assistant MCP server**. In this prototype the Python service obtains a `GetLiveContext` snapshot: Claude itself does **not** have arbitrary MCP tool access.
- Claude may *propose* structured writes, but only a deterministic, restricted gateway may execute them. Eventually support per-sender authorization and optional or mandatory approval for writes.
- n8n is **optional**, used for proactive HA-triggered Signal notifications through a private `/notify` webhook; it is not needed for core message processing.
- Store Signal account keys, Claude credentials, and SQLite history persistently within HAOS add-on `/data`.

The priority is **safe, reliable household operation**, not more agent capabilities. Do not introduce shell, browser, unrestricted REST, unrestricted HA MCP, or filesystem-write tools to the model.

## 2. Starting point — what is actually implemented

| Component | Location | Current behavior / caveat |
|---|---|---|
| HAOS add-on manifest | `config.yaml` | Local amd64 add-on with registration, verification, inspection, Claude check, and run setup modes |
| Container/image | `Dockerfile` | Node 22 + Python venv + native `signal-cli` 0.14.9 + `@anthropic-ai/claude-code@latest`; **image not built or smoke-tested** |
| Supervisor entrypoint | `run.sh` | Register/verify Signal or start native signal-cli HTTP daemon and Python bot |
| Signal transport | `bot/server.py`, `bot/core.py` | JSON-RPC send and SSE receive, sender/group allowlists; **wire protocol has not been integration-tested** |
| Passive conversation context | `bot/core.py::Store` | SQLite deduplication; store authorized messages; last 60 messages and 7-day default retention; no durable outbox |
| AI | `bot/server.py::claude_plan` | Separate unprivileged Claude CLI subprocess, JSON-schema results, no Claude tools; **CLI authentication and flags not tested end-to-end** |
| HA reads | `bot/server.py::ha_context` | Pre-fetch only an MCP `GetLiveContext` snapshot; **no mocked or live MCP integration test** |
| HA writes | `bot/core.py::validate_action`; `bot/server.py::ha_write` | Explicit action/service/entity validation; writes use HA REST; **write intent and per-sender roles not independently enforced** |
| Confirmations | `bot/core.py::Store`; `bot/server.py::handle` | Single-use private confirmation codes for policy entries marked `confirmation` |
| Proactive messages | `bot/server.py::notify` | Authenticated private HTTP webhook; **not tested with a real n8n/HA event** |
| Tests | `tests/test_core.py` | Only **five isolated unit tests**, not system coverage |
| Deployment instructions | `DOCS.md` | Intended HAOS setup flow; **untested on actual Supervisor** |

`README.md` and `AGENTS.md` contain shorter developer notes. `policy.json` is currently a sample policy: the listed entities may not exist on the user's HA instance. Do not grant new HA writes merely to make a test pass.

### Verified vs unverified

The prior session reported passing five unit tests plus Python/shell syntax checks. **No live HAOS add-on installation, Docker image build, Signal registration, Signal daemon protocol, Claude subscription login, HA MCP call, or end-to-end write/confirmation was verified.** Re-run checks independently rather than relying on that report. A previous suspicion about `httpx2`/MCP SDK imports was later withdrawn; evaluate against the actual pinned MCP SDK before labeling it a defect.

## 3. Known defects and major risks (prioritize)

1. **Stale message execution (confirmed by code inspection).** `Store.context()` purges expired history, but `HomeBot.handle()` inserts and can execute a just-delivered old message without rejecting its timestamp/expiration first (`bot/server.py` around `handle`, `bot/core.py::Store.add`). A replayed command from long ago can trigger a new response or write. Implement a bounded event-age and clock-skew policy, with tests for expired/disappearing messages and recovery replay. Determine an explicit policy for backlog messages after downtime.
2. **LLM output is not authorization.** In `handle()`, policy-allowed writes with `confirmation: false` execute immediately based on model-proposed JSON. The sender being on an allowlist and the action being valid do **not** establish that the *current* request authorized that specific action. Old group discussion, entity names, and MCP snapshot can contain prompt injections. Initially **disable all automatic writes** or require a separate confirmation for every write until a tested current-request authorization design exists.
3. **No durable action/outgoing transaction boundary.** SQLite deduplication and SSE cursor are persisted, but replies and HA writes are not recorded as durable operations. Crashes or transport failures between action execution, Signal send, and cursor commit can result in missing replies, duplicate actions, or unknown outcomes. Define retry/idempotency/unknown-outcome behavior. Avoid blindly repeating physical actions after ambiguous HA responses.
4. **Signal and Claude contracts are unverified.** Confirm the native signal-cli 0.14.9 binary archive/checksum, exact daemon health/RPC/SSE endpoints, event shapes, ACI/group IDs, and send parameters. Check pinned Claude Code CLI flags, `--json-schema` envelope, headless OAuth auth, timeouts, and actual behavior with tool disabling. `@latest` makes builds nonreproducible.
5. **HA MCP integration unverified.** Verify installed Python `mcp` SDK API, HTTP auth, transport, endpoint, and exact `GetLiveContext` name/result contract. Do not mock the client so heavily that compatibility cannot be assessed. Error handling must fail closed.
6. **Coarse permissions.** `policy.json` does not presently distinguish user 1/user 2/group contexts; policy depends only on action/entity. Confirmations are linked to sender and action but need comprehensive expiry, replay, crash, and group-context tests. Start with no lock/alarm/garage write access.
7. **HAOS security/deployment.** `signal-cli` and Python bot run in the same container, and HA service credentials are held by the Python process. Review file ownership, environment visibility, network listeners (`NOTIFY_BIND=0.0.0.0`), secret handling, Supervisor metadata, backup retention, resource consumption and graceful shutdown. Never log credentials or whole private group transcripts.
8. **Message privacy.** All allowed group messages are stored locally and sent to Anthropic only when a model turn includes them. Enforce disappearing-message retention accurately and document informed consent from both participants.

## 4. Non-negotiable security properties

- **Fail closed** on missing sender metadata, unrecognized group, stale events, unknown service/entity/arguments, or invalid Claude output.
- Signal metadata from the received envelope is authoritative for sender/group identification; the model's text is not.
- Group messages not addressed to the bot may inform future responses **but can never authorize tool execution**.
- Do not let Claude access the HA token, Signal keys, local service credentials, raw `/data`, or direct write-capable HA MCP/REST tools.
- Keep user-provided text, transcript snippets, sensor values, and tool results as **untrusted data** rather than higher-priority instructions.
- Approval must bind to authenticated sender, exact action and arguments, destination, creation time, and short expiry; consume once. In a shared group, send approval requests to the authorized user's private DM.
- Define a safe read-only default; all writes must be explicitly enabled. Treat security devices (locks, garage, alarms/cameras), automation editing, shell, and HA administration as out of scope until separately designed.
- Do not use `--dangerously-skip-permissions`, inherit a paid API key, or silently widen access to pass tests.
- Do not perform real HA actions or real Signal registration during automated tests.

## 5. Work plan for the next Codex session

**Phase A — reproduce and audit**

1. Unpack/open the repository and read `HANDOFF.md`, `AGENTS.md`, `DOCS.md` and source; create a concise list of concrete defects vs unverified assumptions.
2. Run existing tests and syntax/static checks. Reproduce stale message handling and demonstrate expected behavior with a failing regression test before patching it.
3. Pin direct Python/npm/system dependencies and validate the checksum and native signal-cli artifact. Run a real **amd64 Docker build** and command/CLI smoke checks if the environment supports them. Clearly report if Docker/online dependencies are unavailable.

**Phase B — safe core behavior**

4. Fix stale replay, disappearing-message expiry, duplicate handling, and impossible/future timestamps; decide what should happen after prolonged bot downtime.
5. Make writes disabled by default; implement policy-driven per-sender permissions, optional group-specific restrictions, and confirmation for all writes in the initial release. Ensure raw transcript text can never bypass the gateway.
6. Improve persistence of incoming checkpoints, outbound messages, pending confirmations, and HA action status; define safe retry semantics and single-worker processing/locking.
7. Keep Claude isolated and subscription-aware; add clear errors for missing/expired auth and plan exhaustion without revealing tokens.

**Phase C — realistic tests**

8. Add integration tests using realistic mock servers/fake subprocesses for: signal-cli HTTP JSON-RPC/SSE receive/send/reconnect, Claude subprocess invocation/schema/timeouts, HA MCP `GetLiveContext`, HA REST writes, and `/notify` authentication.
9. Add adversarial tests: forged sender/group, malformed events, unauthorized DM/group, history-only commands, prompt injections, stale and duplicate commands, out-of-order events, expired confirmation, wrong sender confirming, HA partial failure, crash before/after sending, and duplicate webhook delivery.
10. Assert that unauthorized scenarios cause **zero HA write calls**, and every eligible write requires the intended approval mechanism. Record branch coverage for critical policy/authorization code; avoid optimizing for one arbitrary overall coverage number.

**Phase D — deployment proof**

11. Produce a repeatable Docker/HAOS build and compatibility report; verify add-on configuration and persistent `/data` behavior.
12. Provide a step-by-step **manual** smoke-test checklist (using a test light or harmless climate entity): register spare number, identify both sender ACIs, verify one group and two DMs, receive passive group history, trigger Claude, fetch live HA state, confirm a harmless write, receive a proactive notification, restart and recover.
13. Update `README.md`, `DOCS.md`, `AGENTS.md`, changelog/version as appropriate. Document known limits and actual test evidence, not presumed functionality.

## 6. Acceptance criteria for a first safe deployment

Status as of 0.5.0: [x] = evidence exists (see DOCS.md → Test evidence), [~] = covered by realistic fakes only, [ ] = open.

- [x] Reproducible Docker image builds for `amd64`; base digest, signal-cli checksum, Claude Code version and Python lock are fixed. (Built under emulation on arm64, not yet on the NUC.)
- [x] Existing and new tests pass (73, including inside the image). Security-critical authorization branches have explicit coverage (`bot/core.py` 98% branch coverage).
- [~] An end-to-end test confirms receiving Signal messages and replying from the intended account; two DMs and exactly one group work; other senders and groups are rejected. (Fake daemon modelled on upstream v0.14.9 source; real daemon endpoints smoke-tested without an account.)
- [~] Unaddressed group messages are retained for context but never invoke Claude or issue HA commands.
- [~] Old, replayed or disappeared messages cannot result in a new HA write; duplicate Signal events do not cause duplicate actions.
- [~] Claude runs headlessly with the chosen auth method and produces valid structured results without other tools or credential exposure. (Flags and envelope verified on 2.1.306 with a real login on the dev machine; the auth-failure path was verified inside the image as `node`.)
- [~] Home Assistant reads work through the supported MCP SDK and endpoint without granting unrestricted model tooling. (ha-mcp: one live read verified. Built-in HA MCP: fake only.)
- [~] All writes default to denied until configured, and configured low-risk writes require authenticated confirmation in v1; denial, expiry and replay paths verified.
- [~] Failure and restart paths avoid dangerous blind retries and report ambiguous action outcomes clearly.
- [~] Private proactive `/notify` webhook accepts authenticated, authorized targets only; unauthorized callers fail.
- [ ] Actual Supervisor install, restart, backup and first-run behavior tested on the user's HAOS device before enabling regular operation.

**First live rollout:** read-only for several days; then explicitly allow a test light action with confirmation. Do not interpret green CI as proof that signal-cli registration and HAOS networking will work without on-device checks.

## 7. Commands and constraints for Codex

From the app directory (`signal_claude/` in this repository):

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q bot scripts tests
bash -n run.sh
# When Docker is available, build the add-on image and smoke-test CLI binaries.
# Example: docker build -t signal-homebot-claude:dev .
```

No real tokens, accounts or phone numbers are included in this archive. Secrets are configured via HAOS app options. **Do not ask the user to paste live OAuth/HA tokens into a coding chat.** If external services are inaccessible, substitute realistic mocks and explicitly record the missing live evidence.

## 8. Copy-paste task for Codex

> Take ownership of hardening this repository from an experimental Signal/Claude/HAOS prototype into a verifiably safe first release. Read `HANDOFF.md`, `AGENTS.md`, and all application code first. Work through the priorities and acceptance criteria in `HANDOFF.md`. Do not merely increase unit-test counts: reproduce and fix stale-event execution, design an explicit authorization boundary for the *current* request, make all writes confirmation-gated by default, and test the actual Signal/Claude/MCP interfaces through realistic integration tests. Keep Claude Code as the deployed model; you are Codex only as the development agent. Preserve registration through signal-cli using a spare phone number as the primary device, passive shared-group context, two DM allowlisted users, and optional n8n notifications. Pin dependencies, execute all tests and a Docker build if available, document any unverified external integrations, and provide a detailed changelog and on-device smoke-test checklist. Never exercise real HA writes in automated tests. Do not claim production-readiness without live evidence.

## 9. Design decisions to preserve unless discussed with the user

- Prefer this small bot service over OpenClaw or an n8n-only AI agent; n8n remains optional for notifications.
- No extra phone/SIM beyond the user's spare number; no Android required for ordinary Signal registration.
- Claude Code subscription CLI is the intended AI runtime. Consider billing/terms explicitly, especially where requests originate from the second person.
- Three separate conversation contexts: DM for user 1, DM for user 2, and shared group. A group history must never leak into a private DM except via independently authorized, deliberate sharing.
- Physical-device control is lower priority than robust messaging and accurate read-only answers.
