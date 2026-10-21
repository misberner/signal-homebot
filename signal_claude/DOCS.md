# Installing Signal HomeBot (Claude Code) on Home Assistant OS

This is an **experimental local app**, not a published community app. It
packages native `signal-cli` 0.14.9, Claude Code CLI 2.1.306, Python 3.13 and
the bot inside one amd64 container. It is **read-only by default**. See
[Test evidence](#test-evidence) for what has and has not been verified.

## Install

1. **Settings → Apps → App store → ⋮ → Repositories**, add
   `https://github.com/misberner/signal-homebot`. (While the GitHub repository
   is private, Supervisor needs credentials in the URL; see the repository
   README.)
2. Install **Signal HomeBot (Claude)**. Supervisor builds the image on the HA
   box (amd64), fetching signal-cli and Claude Code; the build runs the test
   suite and fails if it fails. Updates arrive through the app store when
   `version` in `config.yaml` changes.
3. Leave **Start on boot** disabled until initialization is complete. Each
   setup mode exits when done; see its output under Logs.

## Signal primary-device registration (using your spare number)

1. Set `signal_phone` to `+49...` in E.164 form. Select `setup_mode: register`.
   Save and Start; complete the SMS challenge. If a captcha is requested, get
   `signalcaptcha://...` via https://signalcaptchas.org/registration/generate.html
   and set `signal_captcha` before retrying.
2. Set `setup_mode: verify`, enter the SMS in `verification_code`, Save and
   Start. Afterwards clear `verification_code` and `signal_captcha`.
3. Set `setup_mode: inspect` to display the bot account UUID (ACI). Add both
   users and the bot to one Signal group. Inspect again for the group's ID.
4. `setup_mode: listen`: have both people DM the bot during the 60-second
   listen period. Sender UUIDs appear in logs (not message text). Use these
   in `signal_user1_aci` and `signal_user2_aci`, and the group ID in
   `signal_group_id`. `user1`/`user2` are also the role names used in
   `policy.json`.
5. Do not use telephone numbers as ACI values. The CLI remains the primary
   Signal device; no Android phone is required.
6. **Verify both people's safety numbers** (required by default, see
   [Trusted accounts](#trusted-accounts)): `setup_mode: trust` logs each
   user's safety number. On each phone open the conversation with the bot →
   **View safety number** and compare. Only if they match, enter the number in
   `signal_user1_safety_number` / `signal_user2_safety_number` and run `trust`
   again; it must print `verified` and trust level `TRUSTED_VERIFIED`.
   signal-cli rejects a number that does not match its stored key.
7. Have both people enable **Registration Lock** (Signal → Settings → Account)
   so their numbers cannot be re-registered by someone else (SIM swap).

## Trusted accounts

The bot acts only for the two configured people, enforced in layers:

1. **Authenticated sender.** The sender's ACI comes from the decrypted Signal
   envelope (`sourceUuid`), which the Signal protocol authenticates; message
   text, display names and phone numbers are never used for identity. Anything
   not from `signal_user1_aci`/`signal_user2_aci` (or the bot itself) is
   dropped before storage.
2. **Pinned identity keys.** signal-cli rejects messages whose sender identity
   key changed since it was first seen (e.g. the number was re-registered on
   another phone) until someone re-trusts it. On top of that the bot checks
   each sender's key with `listIdentities` and, by default
   (`require_verified_identity: true`), accepts only keys verified by safety
   number (`TRUSTED_VERIFIED`). Unverified or changed keys are ignored and
   logged. If someone legitimately gets a new phone, re-run `setup_mode: trust`
   with their new safety number.
3. **One group, exactly three members.** Only `signal_group_id` is accepted.
   Before answering in the group and again before every group message is sent
   (including `/notify`), the bot checks the roster: exactly the bot and the two
   users, no pending invitations. Otherwise it does not post (a newcomer would
   see replies and history) and tells the requester in their DM.
4. **Fresh, explicit requests only**, and writes need the requester's private
   confirmation (below).

What this cannot protect against: someone who controls one of the two
people's Signal accounts or devices (an unlocked phone, or a Signal Desktop
linked to their account) *is* that person to the bot. Linked devices are
listed in Signal → Settings → Linked devices.

## Authenticate Claude Code using your subscription

Use Claude Code's automation token flow on your own computer:

1. Install Claude Code there and authenticate with your Claude Pro/Max
   subscription (not an Anthropic Console API key).
2. Run `claude setup-token` in a local terminal. Copy the returned OAuth token
   directly into a password manager. **Never post it in chat or commit it to git.**
3. Paste it into the app's `claude_oauth_token` password option, Save.
4. Select `setup_mode: claude_check`, Start, and inspect the authentication
   status in Logs. Then switch back to `inspect` or `run`.

The bot passes only `CLAUDE_CODE_OAUTH_TOKEN` to Claude, never
`ANTHROPIC_API_KEY`, so it cannot fall back to usage-billed API credentials.
An expired/invalid token or an exhausted plan is reported in Signal as such
("not logged in" / "usage limit reached"); the token itself is never shown.
Subscription usage is subject to Anthropic's limits and terms. Requests from
the second household member are served from your subscription; check the
terms apply to your situation.

## Home Assistant reads (MCP)

The bot itself (not Claude) makes **one fixed read-only MCP call** per
request and gives Claude the result as untrusted text. Claude has no MCP or
other tools. Choose a backend with `ha_mcp_kind`:

### `hass`: Home Assistant's built-in MCP Server (default)

1. Add the **Model Context Protocol Server** integration. Select the Assist
   API and enable its read-only option. Expose only safe entities to Assist.
2. Create a non-admin HA user and a long-lived token for it; set `ha_token`.
3. `ha_mcp_url`: `http://homeassistant:8123/api/mcp/assist`.

The bot calls only `homeassistant__GetLiveContext` (or `GetLiveContext` on
older HA) and fails closed if it isn't offered exactly once.

### `ha_mcp`: the community ha-mcp server (homeassistant-ai/ha-mcp)

1. **Read Only Mode is required.** The bot refuses an ha-mcp connection that
   lists write tools (`ha_call_service`, …) or whose overview lacks
   `read_only_mode: true`. Either append `/readonly` to the URL (a
   per-connection read-only alias) or, better, run a separate ha-mcp instance
   with Read Only Mode switched on. With `/readonly`, whoever obtains the URL can
   still drop the suffix and get full access; a separate read-only instance
   doesn't have that problem.
2. `ha_mcp_url`: when ha-mcp runs inside Home Assistant (the
   `ha_mcp_tools` "HA-MCP Server" integration, port 9584, bound to `0.0.0.0`),
   use the Supervisor-internal address with your secret path, e.g.
   `http://homeassistant:9584/private_…/readonly`. That's the same route as the
   default `http://homeassistant:8123`; don't use a Tailscale or LAN address.
   The `private_…` path is the credential, so the bot sends no HA token to it.
3. `ha_mcp_domains`: comma-separated domains to include (default
   `light,climate,cover,weather`). Each adds tokens to every Claude request.
   The overview gives entity *states* (e.g. `heat`), not temperatures. Add
   `sensor` if you need readings and accept the larger prompt.

The bot calls only `ha_get_overview` with fixed arguments, without
`system_info` (it contains your home's coordinates) and without
notifications. It re-renders the result as `entity_id (name): state` lines
and fails closed on any other result shape. `ha_token` is then needed only
if writes are enabled.

## Writes (disabled by default)

Writes need **all** of:

1. `allow_writes: true` in the app options.
2. An entry in `policy.json` in the app's config folder (Samba/File editor:
   `addon_configs/<id>_signal_claude/policy.json`; created on first start
   with no entities; **restart the app** after editing, no rebuild needed):

   ```json
   {"actions": {
     "light_on":  {"entities": ["light.test_lamp"], "senders": ["user1"], "contexts": ["dm", "group"]},
     "light_off": {"entities": ["light.test_lamp"], "senders": ["user1"], "contexts": ["dm", "group"]},
     "temperature_set": {"entities": ["climate.office"], "senders": ["user1", "user2"],
                         "contexts": ["dm"], "temperature_min": 18, "temperature_max": 23}
   }}
   ```

   Only `light_on`, `light_off` and `temperature_set` exist, mapped in code to
   `light.turn_on`, `light.turn_off` and `climate.set_temperature`. Locks,
   alarms, garages, cameras, scripts and administration are not supported.
   Unknown keys, services or roles make the bot refuse to start.
3. A **private confirmation for every write**. Claude only proposes. The
   requester gets a DM: `Confirm light.turn_on on light.test_lamp? Reply
   "confirm <code>"`. The code is single-use, valid 2 minutes, bound to that
   sender and that exact action, and accepted only in that sender's DM. The
   other person, the group, or Claude cannot confirm it. `cancel <code>`
   withdraws it.

Writes go through HA REST with `ha_token` (use a non-admin user). Outcomes:
`Done`, `did not perform` (HA rejected it or was unreachable) or **UNKNOWN**
(timeout, HTTP 5xx, dropped connection, or a restart mid-call). Unknown
outcomes are reported and **never retried**.

## Message handling

- **Who:** only the two configured ACIs, in a DM or the one configured group.
  Everything else is ignored and not stored. Identity comes from Signal
  metadata, never from message text.
- **When Claude runs:** every DM; group messages starting with `/ha` or `!ha`,
  containing `@HomeBot`, or with a real Signal @mention of the bot. Other
  group messages are stored as context only.
- **Old messages:** a request older than `command_max_age_seconds` (default
  300 s, judged by the *older* of the sender and server timestamps) is kept as
  context but never executed. The sender gets "reached me too late … please
  send it again". This covers backlog after downtime, phone-queued messages and
  replays. Messages more than 2 minutes in the future, already-expired
  disappearing messages, and messages older than `history_days` are dropped.
- **Duplicates:** each Signal message is processed at most once (SQLite).
- **Restarts:** a request interrupted by a restart is reported as not
  completed rather than re-run. Replies wait in a durable outbox and are
  retried for up to an hour. A retried send can at worst duplicate a text,
  never an action.
- **History:** the last 60 messages of the *same* conversation go to Claude,
  labelled `user1`/`user2` rather than ACIs. Group history never reaches a DM.
  "Delete for everyone" removes a message from the archive.
  Disappearing-message timers are honoured. Default retention is 7 days.

## Process isolation

Inside the container root only prepares `/data` and supervises. Each service
runs as its own user with no capabilities (`setpriv`, `no_new_privs`, empty
bounding set) and only its own environment:

| User | Process | Holds | Data |
|---|---|---|---|
| `signal` | signal-cli daemon (HTTP on 127.0.0.1) | Signal identity keys | `/data/signal` (0700) |
| `homebot` | the bot | HA token, notify token | `/data/homebot` SQLite (0700) |
| `node` | Claude broker + Claude Code | Claude OAuth token | `/data/claude` (0700) |

The bot reaches Claude only through the broker's Unix socket (directory
readable by `homebot` only, peer UID checked); the broker runs Claude with a
fixed command line (no tools, no MCP, safe mode). The signal-cli HTTP port
has no authentication of its own and is reachable by any process inside this
container; nothing else listens there.

## Proactive notifications (optional, n8n)

Set `notify_token` (≥ 32 random characters) to enable `POST /notify` on port
8999 of the app's internal network; leave it empty to disable the listener.
No LAN port is published.

```
POST /notify
Authorization: Bearer <notify_token>
{"target": "group" | "user1" | "user2", "message": "…", "id": "optional-dedupe-key"}
```

`202` means queued; `200 {"duplicate": true}` means that `id` was already
accepted. Use a stable `id` (e.g. the HA event id) so webhook retries don't
duplicate notifications.

## On-device smoke test checklist

Do this before regular use, with a harmless test light. Record the results.

1. [ ] App builds on the HAOS box (Logs show `Ran … tests … OK`).
2. [ ] `inspect`: `claude --version` shows `2.1.306`; `listAccounts` lists the bot.
2a. [ ] `trust`: both users show `TRUSTED_VERIFIED` after entering their
    compared safety numbers; a wrong number is rejected.
3. [ ] `claude_check`: the auth status shows subscription/OAuth login.
4. [ ] `run` with `allow_writes: false`. Logs show `Ready (writes disabled)`.
5. [ ] User 1 DM "is the test light on?" gives a correct answer (live state).
6. [ ] User 2 DM works; a third phone number gets no reply.
6a. [ ] Add a third person to the group: `/ha` there gets no group reply
    and a DM saying why. Remove them again; group replies resume.
7. [ ] Group: a plain message gets no reply and no Claude call in the logs.
   `/ha what did I just say?` answers using it.
8. [ ] Group: an @mention of the bot (picked from the mention list) triggers a reply.
9. [ ] Stop the app, send a DM, wait > 5 minutes, start. You get "too late".
10. [ ] Restart the app and send a DM. It works without re-registration
    (persistent `/data`).
11. [ ] Set `allow_writes: true` and add only the test light to
    `policy.json` in the config folder. Restart. "/ha turn on the test light" in the group gives a
    DM confirmation prompt. User 2 sending the code is rejected. User 1
    confirming turns it on. A second `confirm` with the same code is rejected.
12. [ ] Let a confirmation code expire (> 2 min). It is rejected.
13. [ ] n8n (if used): a POST to `/notify` without a token gets 401. With a
    token it delivers. Repeating the same `id` doesn't deliver twice.
14. [ ] Take an HA backup and confirm the app's data is in it; protect that backup.

## Backups, privacy and limitations

`/data/signal`, `/data/claude` and `/data/homebot` survive restarts and
updates (a 0.4.x `/data/bot.sqlite3` is moved into `/data/homebot` once). HA backups include secrets and conversation data; protect them. The
subscription token, HA token and an ha-mcp URL in the options are secrets.

Group messages are stored on the HA box and sent to Anthropic when a request
includes them. **Both group members should know and agree.** Signal's
end-to-end encryption ends at the bot.

Do not run more than one daemon for the same registered Signal account (the
bot also refuses a second instance on the same database). If you installed
the older `signal_codex` app, this slug creates a **separate** `/data`:
nothing is migrated, and re-registering the same number may invalidate the
old device.

## Test evidence

Verified on 2026-10-21 (see CHANGELOG.md):

- `docker build --platform linux/amd64` succeeds, with signal-cli 0.14.9
  (SHA-256 matches GitHub's published digest), Claude Code 2.1.306 and locked
  Python deps. 73 tests pass inside the image (including the Linux-only
  broker peer-UID test).
- Container smoke test of the real `run.sh` (signal-cli replaced by the test
  fake; everything else real): the three services run as `signal`/`node`/
  `homebot` with zero capabilities and `NoNewPrivs`; each sees only its own
  secrets; cross-user reads of keys, database, options and `/proc/*/environ`
  are denied; root is rejected by the broker; a DM flows through a read-only
  MCP read, the broker and Claude Code 2.1.306 (invalid token → "not logged
  in" reply); SIGTERM stops all services.
- Live: the bot refuses the admin URL of a real ha-mcp 8.7.0 and accepts its
  `/readonly` alias.
- signal-cli 0.14.9 source: changed identity keys are rejected on receive
  (`IdentityKeyStore.isTrustedIdentity`); `trust -v` verifies the number.
- Inside the image, the native `signal-cli` daemon answers `/api/v1/check`,
  JSON-RPC `version` and `/api/v1/events` (`text/event-stream`).
- Claude Code 2.1.306: the exact bot flags return `structured_output`. An
  invalid OAuth token yields `api_error_status: 401`, classified as `auth`,
  with Claude running as `node` via the broker.
- A live read-only `ha_get_overview` call against a real ha-mcp 8.7.0 / HA
  2026.10.2, using the bot's own code.

**Not yet verified:** Signal registration, real message receive/send and
mentions, `listIdentities`/`listGroups`/`trust` against a real account, the
HA built-in MCP server live, the bot reaching ha-mcp at `homeassistant:9584`
from inside the app (standard Supervisor routing, not yet exercised), HA REST
writes, Supervisor install/backup and the `addon_config` mapping, whether Supervisor's container security options
permit `setpriv`, n8n, and Claude with a real subscription token inside the
container. Do the checklist above.

## Development

```bash
python3 -m unittest discover -s tests -v          # core tests; integration tests skip without deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v # all tests
docker build --platform linux/amd64 -t signal-homebot-claude:dev .
```

Regenerate `requirements.lock` inside the base image after changing `requirements.txt`:

```bash
docker run --rm --platform linux/amd64 -v "$PWD/requirements.txt:/r.txt:ro" \
  node:22-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf \
  bash -c 'apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null &&
           python3 -m venv /v && /v/bin/pip install -q -r /r.txt >&2 && /v/bin/pip freeze --all --exclude pip'
```
