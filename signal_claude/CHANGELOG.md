# Changelog

## 0.11.0 — 2026-10-22

### Added
- **Repeating actions**: an action with `at` can repeat (daily, weekdays,
  weekly); it is confirmed once and every run happens without asking (the
  switch, allowlist and denylist are re-checked each time).
- **Task grants**: a scheduled task lists the calls it may need
  (`grants`: service + entity). Such a task is confirmed once when set up
  (also in auto mode); when it runs, granted calls happen without asking
  (allowlist/denylist checked, the reviewer is told about the grant and can
  still deny). Other proposals are asked as before.

## 0.10.2 — 2026-10-22

### Fixed
- Updating from before 0.10 no longer widens what the bot may change: a
  non-empty old action table becomes an active allowlist of its entities
  (instead of being dropped).

## 0.10.1 — 2026-10-22

### Added
- **Allowlist** (setup page, "Only what's on the allowlist"): when on, only
  entities or services matching one of its patterns can be changed (empty:
  nothing); everything else is refused before the reviewer. Rules, denylist
  and reviewer still apply. Re-checked when a confirmed or scheduled change
  runs.

## 0.10.0 — 2026-10-22

### Changed
- **New change model: no action table.** Claude may propose any service call
  on one entity in the fixed writable domains (locks, alarms, cameras,
  notify, automations and admin stay impossible). Each proposal passes:
  fixed checks in code (service of the entity's domain, entity exists in HA,
  flat data without extra targets), the admin's **denylist** (glob patterns
  on entity or service), and a **reviewer**: a separate Claude call without
  tools or chat history that sees the admin's **Rules**, the requester, the
  request, the exact call and the entity's state, and answers allow / ask /
  deny. "ask" (and any reviewer failure) asks the requester to confirm as
  before; "allow" runs without asking only when **Auto mode** is on (off by
  default). Settings from earlier versions load (see 0.10.2 for the action
  table).

### Fixed
- **Replies sorted above the request** (seen on Signal Desktop): Signal
  orders by sender timestamps, so if the phone's clock was ahead, the bot's
  immediate reply got an earlier timestamp. The bot now waits (≤ 3 s) until
  its clock has passed the newest incoming message before sending.

## 0.9.0 — 2026-10-22

### Added
- **Scheduled tasks that run Claude.** Claude can schedule `tasks`
  (`at`, `prompt`, `repeat`: once/daily/weekdays/weekly). When one is due,
  Claude runs with a prompt that says a scheduled task is firing (not a new
  message), who set it up and when, the original request and the task's
  instruction; its answer is posted in the chat ("⏰ #12: …"), or nothing if
  it replies empty. Proposed actions still need confirmation; a running task
  can't create tasks or reminders. Repeats keep the local time across DST;
  missed or interrupted runs are reported and the series continues.

## 0.8.5 — 2026-10-22

### Added
- `get_image` also returns camera snapshots (`camera.*`, through ha-mcp's
  read-only `ha_get_camera_image`).

## 0.8.4 — 2026-10-22

### Added
- Read tools (ha-mcp): `get_dashboard` (list/read/search dashboards, plus
  screenshots when ha-mcp's screenshot beta is enabled),
  `get_automation_config` and `get_script_config` (sources), and
  `get_image` (the picture of an `image.*` entity, fetched through HA's
  image proxy with the entity's own short-lived token; only HA's own
  `/api/image_proxy/` links are followed). All read-only.

### Changed
- A dropped connection to the MCP server is retried once (reads only).

## 0.8.3 — 2026-10-22

### Added
- Setup page: members and invitees of the bot's groups who aren't users yet
  appear under "Recently seen" (with their Signal profile name and group),
  so they can be added without writing to the bot first.
- Setup page: added groups list their members again, marking anyone who
  isn't a configured user and pending invitations.

## 0.8.2 — 2026-10-22

### Fixed
- **"Can't reach Claude" whenever Claude read a lot of HA data (0.8.0).**
  The broker read Claude's stream-json output with asyncio's 64 KiB line
  limit; a large tool result is one line, so the read failed and the
  connection dropped. The limit is now 8 MiB, and the Claude process is
  always killed if the broker stops reading early.

## 0.8.1 — 2026-10-22

### Fixed
- **Answers never replaced the status message on iPhone.** Signal-iOS gives
  an edited message the edit's timestamp and drops later edits that target
  the original one, so only the first progress update appeared. Each edit
  now targets the latest revision.

## 0.8.0 — 2026-10-22

### Fixed
- **Message text in the app log (all earlier versions).** signal-cli printed
  every received message, including its text, to stdout and therefore the
  HA app log. The daemon now runs with `--no-receive-stdout`, and a test
  guards the flag.
- **Times were UTC.** `run.sh` cleared Supervisor's `TZ`. It is now passed to
  the bot and Claude, the image includes `tzdata`, and each request states
  the local date and time.
- **The bot forgot its own answers.** Sent replies are now part of the
  history (same retention, disappearing timer and `/reset`).
- **Unverified people heard nothing,** so their phone never showed a safety
  number. They now get one short "please verify me" note (rate-limited), and
  the setup page has **Send hello**.
- **"Control is not enabled" dead ends.** Claude now still names the entity
  and service it would use.
- **Presence and other custom data.** Search guidance now covers attributes
  and custom integrations (`pyscript.*`, Bermuda `*_area`).

### New
- **Scheduling:**
  - Actions with `at` (up to 7 days ahead) are confirmed now and run later,
    durably in SQLite, re-checked against the policy when due.
  - A job more than 10 minutes overdue is reported "missed", never run late.
  - Reminders (no confirmation).
  - `/scheduled` and `/cancel N`, or ask Claude.
- **Progress in one message:** "⏳ Looking into it…" is edited as Claude
  uses tools (Claude Code `stream-json`), then becomes the answer. Also read
  receipts and a typing indicator.
- **`/reset`** (`/clear`) forgets a chat's history.
- **Model choice** (`opus` by default, `fable`, `sonnet`, `haiku`); the model
  that answered is logged.
- **Notes for the assistant** in the setup page (trusted, in the system
  prompt).
- **Prompt caching:** a fixed working directory (Claude Code puts the cwd
  into its system prompt) and constant instructions via
  `--append-system-prompt`. Measured 2.8k instead of 1.1k cached tokens.


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
