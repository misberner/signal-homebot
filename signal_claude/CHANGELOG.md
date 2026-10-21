# Changelog

## 0.7.0 — 2026-10-22

**Breaking:** setup moved to the new setup page. The old `setup_mode`,
`signal_*`, `users`, `groups`, `allow_writes` and similar options are gone.
An already-registered number is adopted automatically; add people and groups
again in the setup page.

### Claude reads Home Assistant itself
- The fixed state snapshot is replaced by read tools that Claude calls as
  needed. A local MCP proxy in the bot (127.0.0.1, per-request token) offers:
  - ha-mcp: `search_entities`, `get_states`, `get_overview`, `get_history`;
  - HA's built-in server: `get_live_context`.

  It forwards calls upstream with constrained arguments, after the read-only
  check. Claude never holds HA credentials.
- Claude Code flags: `--safe-mode` dropped, because it silently ignores
  `--mcp-config` (verified). Now `--tools ''`, `--setting-sources ''`,
  `--disable-slash-commands`, `--strict-mcp-config`, and only the proxy's
  tools in `--allowedTools`. The MCP config is written to a private file,
  not argv.
- Verified with real Claude Code 2.1.306: it called `get_states` through the
  proxy and answered from the result.

### Generic actions
- Policy entries are `{service, entities, fields, senders, contexts}`, with
  per-field limits: number ranges, value lists, booleans. Services come from
  a fixed list of controllable domains. Locks, alarms, cameras, notify,
  automations, shell/REST commands and administration can never be
  controlled.

### Setup page (HA ingress)
- Covers registration (captcha, SMS/voice code, PIN), people and groups the
  bot has recently seen (IDs only), safety-number verification, the action
  editor, settings, and Home Assistant/Claude tests.
- Reachable only through Supervisor's ingress proxy; changes require a
  custom header.
- Household settings live in `/data/homebot/settings.json`; the
  Configuration tab keeps only secrets and connections. The `policy.json`
  file and the config-folder mapping are gone.
- signal-cli runs in multi-account mode so the page can register numbers.

### Confirmations
- Confirm with "yes" or a 👍 reaction to the bot's question; "no" or 👎
  cancels. A reaction or quoted reply targets that exact question. A bare
  "yes" works when exactly one question is open.
- Still private-chat only, by the requester only, fresh only, single use,
  2-minute expiry.

### Tests: 75 → 85
Covers the read-tool proxy (real MCP on both sides; tokens; argument
limits), generic actions, reply and reaction confirmations, the setup-page
flow (register → verify → add → trust → actions), household settings
validation, and account adoption.


## 0.6.0 — 2026-10-21
### Changed (options)
- **Any number of users and groups.** The fixed `signal_user1_aci`,
  `signal_user2_aci`, `signal_userN_safety_number` and `signal_group_id` options
  are replaced by two lists:
  - `users: [{name, aci, safety_number}]` (at least one)
  - `groups: [{name, id}]` (may be empty: DMs only)

  Names are what `policy.json` `senders`, `/notify` targets and Claude's
  transcript labels use. Startup fails on duplicate ACIs, the bot's own ACI,
  user/group name clashes, or policy senders that aren't configured users.
- Group roster rule: every member and pending invitee must be a configured
  user. This replaces "exactly bot + two users", so a group may contain any
  subset of them.
- `trust` verifies each configured user independently; unverified users are
  ignored until verified.


## 0.5.2 — 2026-10-21
- `register` and `verify` print an explicit OK/FAILED line and the next step.
  signal-cli prints nothing on success, which looked like a hang.
- `inspect` prints the bot account ID to copy into `signal_bot_aci`.


## 0.5.1 — 2026-10-21

### Fixed
- **signal-cli failed on the first real HA box** with "CPU ISA level is
  lower than required": the Linux-native build needs an x86-64-v3 CPU. The
  app now uses signal-cli's Java distribution (same version, checksum
  verified) on Debian's `openjdk-25-jre-headless`. libsignal's x86_64 library
  targets baseline x86-64.
- The image build now loads libsignal and generates a key pair. Supervisor
  builds on the HA box, so an incompatible CPU fails the install instead of
  the running bot.
- `inspect` shows the CPU model, the supported x86-64 levels and the
  libsignal check.
- Config folder mapping renamed `addon_config` → `app_config` (current
  Supervisor name).

### Verified on the HA box (0.5.0 install)
Supervisor build, the policy file being created in the config folder,
Claude Code 2.1.306, and `setpriv` user switching under HA's default
AppArmor profile all work.


## 0.5.0 — 2026-10-21 (closing the 0.4.0 open issues; still not production-verified)

### Trusted accounts
- Each sender's identity key is checked with `listIdentities` before a message
  is used. By default (`require_verified_identity: true`) only keys verified
  by safety number (`TRUSTED_VERIFIED`) are accepted. Unverified keys, changed
  keys and failed lookups are ignored, and the message is not stored.
- New `setup_mode: trust` shows the safety numbers. With
  `signal_userN_safety_number` set, it runs `signal-cli trust -v`, which
  rejects numbers that don't match.
- Group roster guard: the bot answers and posts in the group only while it
  contains exactly the bot and the two users, with no pending invitations.
  Otherwise the requester is told by DM, and queued group messages (including
  `/notify`) are dead-lettered rather than leaked.

### Privilege separation (single add-on container)
- `run.sh` (root) only prepares `/data` and supervises. Each service runs
  under `setpriv` with no capabilities, `no_new_privs` and its own minimal
  environment:
  - signal-cli as `signal`
  - the bot as `homebot`
  - the new Claude broker (`bot/claude_broker.py`) and Claude Code as `node`
- The bot no longer holds the Claude token, runs as root or drops privileges
  itself. It reaches Claude only through the broker's Unix socket. The
  broker has a fixed command line, checks the peer UID, runs one request at a
  time, and enforces size limits and a timeout.
- The bot refuses to start as root.
- The database moved to `/data/homebot/` (0600), migrated automatically.
- Fixed shutdown: SIGTERM now stops every child. Previously `$!` pointed at a
  wrapper shell.

### Configuration
- The policy is loaded from the add-on config folder (`/config/policy.json`,
  `map: addon_config`) and created with no entities on first start. Edits
  need a restart, not a rebuild.
- ha-mcp: Read Only Mode is enforced. Connections whose catalog lists write
  tools, or whose overview lacks `read_only_mode: true`, are refused. Verified
  live: a real ha-mcp 8.7.0 admin URL is refused and its `/readonly` alias
  accepted.

### Tests: 62 → 73
Added tests for the broker (malformed requests, busy, peer UID on Linux,
broker down), identity trust levels, the group roster (extra member, pending
invite, roster change while a reply is queued, `/notify`), ha-mcp read-only
enforcement and runtime policy loading. Also added a container smoke test of
the real `run.sh` process layout (see DOCS.md → Test evidence).


## 0.4.0 — 2026-10-21 (hardening; still not production-verified)

### Defects fixed (blocking: the 0.3.0 bot could not have worked)
- **Signal receive:** signal-cli 0.14.9's HTTP SSE stream emits
  `event:receive` / `data:{"account","envelope"}`, not JSON-RPC
  `{"method":"receive","params":…}`, so 0.3.0 dropped every message. Checked
  against upstream `HttpServerHandler`, `SseEventBuffer` and
  `JsonReceiveMessageHandler` at v0.14.9.
- **Claude schema:** Claude Code rejected `schema.json` (`$schema` draft
  2020-12 key: "not a valid JSON Schema"), so every Claude call failed. The
  schema is now generated from the policy, without `$schema`.
- **Poison SSE event:** a malformed event raised inside the stream loop and
  was replayed forever on reconnect. Events are now handled one at a time,
  and the cursor always advances.

### Security
- **Stale/replayed commands** (HANDOFF risk 1): explicit age policy based on
  the older of the sender and server timestamps. Stale requests are context
  only and the sender is told. Future-dated, expired-disappearing and
  out-of-retention messages are dropped.
- **LLM output is not authorization** (risk 2): every write needs a
  single-use, 2-minute, sender-bound confirmation in the requester's DM.
  Writes are off unless `allow_writes: true`. The shipped policy grants no
  entities.
- **Per-sender/context policy** (risk 6): `senders` (user1/user2) and
  `contexts` (dm/group) per action. Strict policy parsing fails closed.
  `lock.unlock` was removed from the service allowlist.
- Real Signal @mentions are detected via `mentions[].uuid`.
- "Delete for everyone" removes messages from the archive.
- Claude sees roles (`user1`/`user2`) instead of ACIs. Untrusted sections are
  fenced with per-request random markers.
- `/notify`: a non-ASCII `Authorization` header no longer crashes
  `compare_digest`. Strict JSON body, optional idempotency `id`. The listener
  is disabled when `notify_token` is empty (n8n is optional).
- `umask 077` for the bot and `run.sh`; the SQLite file is chmod 600.
- A single-instance lock on the database.
- HTTP client loggers are silenced, because they log full URLs (an ha-mcp URL
  contains its secret).

### Reliability (risk 3)
- Durable transaction boundaries in SQLite:
  - jobs (request in progress)
  - approvals (pending/consumed/cancelled)
  - action journal (`executing → succeeded | failed | unknown`)
  - outbox (retry with backoff, dead-letter after 8 attempts or 1 h; text
    wiped once sent)
- On restart, `executing` actions become `unknown` and are reported, never
  retried. Interrupted requests are reported as not completed.
- HA write outcomes are classified: 2xx succeeded, 4xx or connection refused
  failed, timeout/5xx/disconnect unknown.
- Claude errors are classified from the JSON envelope even on non-zero exit:
  `auth` (401/403), `limit` (429/usage limit), `unavailable`, `timeout`,
  `invalid`.

### Home Assistant reads
- New `ha_mcp_kind: ha_mcp` backend for the community ha-mcp server. It makes
  one fixed `ha_get_overview` call (no `system_info`/notifications), renders
  the result to `entity_id (name): state` lines, sends no HA token to ha-mcp,
  and fails closed on unexpected results.
- `hass` backend: exact tool-name allowlist (`homeassistant__GetLiveContext`,
  `GetLiveContext`).
- `ha_token` is only required for writes or the `hass` backend.

### Build
- Base image pinned by digest. Claude Code pinned to 2.1.306. The signal-cli
  checksum was checked against GitHub's published digest. Python deps fully
  locked in `requirements.lock`, generated in the base image.
- The Docker build runs the test suite. Tests are not shipped in the image.

### Tests: 5 → 62
- Core unit tests (standard library only).
- Integration tests against realistic fakes:
  - a signal-cli SSE/JSON-RPC server with Last-Event-ID replay
  - a real MCP SDK server (uvicorn) with HA and ha-mcp tool sets, including
    write tools that must never be called
  - an HA REST server with failure modes
  - a fake `claude` executable that records argv, env and prompt
- Adversarial cases: forged senders/groups, injection via history and entity
  names, stale/duplicate/out-of-order events, wrong-sender/expired/replayed
  confirmations, HA partial failures, crashes before and after the HA call,
  and webhook duplicates.
- Branch coverage: `bot/core.py` 98%. `bot/server.py` ~80%; the uncovered
  parts are process lifecycle and root-only privilege drop, which were
  exercised in the Docker smoke test.

### Verified vs not verified
See DOCS.md → *Test evidence*.
