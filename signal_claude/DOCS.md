# Signal HomeBot (Claude)

A private Signal bot for your household. Allowlisted people DM it, or address
it in configured groups, with questions such as "how warm is the living
room?" or "turn the kitchen light to 40%". Claude Code (with your
subscription) answers by **reading** Home Assistant through a restricted set
of read tools. It can *propose* changes, but each one runs only after the
requesting person says "yes" or reacts 👍.

It packages signal-cli 0.14.9 (Java build on Java 25), Claude Code 2.1.306,
Python 3.13 and the bot in one amd64 container. See
[Test evidence](#test-evidence) for what has and hasn't been verified.

## Install

1. **Settings → Apps → App store → ⋮ → Repositories**, add
   `https://github.com/misberner/signal-homebot`.
2. Install **Signal HomeBot (Claude)**. Supervisor builds the image on the HA
   box. The build runs the test suite and loads Signal's crypto library on
   your CPU; it fails if either fails. Updates arrive through the app store.

## Configuration tab (secrets and connections only)

| Option | What |
|---|---|
| `claude_oauth_token` | Run `claude setup-token` on your computer (Claude Pro/Max login, not an API key), paste the result. Never post it in chats or git. |
| `ha_mcp_kind`, `ha_mcp_url` | How the bot reads Home Assistant, see [Reading Home Assistant](#reading-home-assistant). |
| `ha_mcp_domains` | Domains for the overview tool (default `light,climate,cover,weather`). |
| `ha_token` | Long-lived token of a **non-admin** HA user. Needed for writes, and for the built-in MCP server. |
| `ha_base_url` | Usually `http://homeassistant:8123`. |
| `notify_token` | Optional, for n8n/automation notifications (≥ 32 random characters). |

Start the app, then click **Open Web UI** (or "HomeBot" in the sidebar).
Everything else happens there.

## Setup page

The page is behind Home Assistant's admin login and only reachable through
it.

1. **Bot's Signal number.** Enter a spare number that can receive SMS (or a
   call). Signal usually asks for a captcha: solve it at
   signalcaptchas.org, copy the "Open Signal" link, paste it, and click
   **Request code** within a minute. Then enter the code. The app becomes the
   number's main Signal device; no phone app is needed. A number registered
   by an earlier version is picked up automatically.
2. **People.** Each person sends the bot any message. They then appear under
   **Recently seen** (only their Signal ID and profile name are kept, never
   the text). Give them a short lowercase name and click **Add**. Then verify
   their safety number. Signal shows it only once the bot has written to
   them: the bot does that by itself when an unverified person messages it,
   or click **Send hello**. On their phone, open the chat with the bot → tap
   its name → **View safety number**, compare it with the number shown, and
   enter it. Until a person is verified, the bot answers them only with that
   note (you can switch verification off in Settings, but don't).
3. **Groups (optional).** Add the bot's number to a Signal group and post
   something there. The group appears under **Groups the bot is in**, with its
   members. Add it with a name. The bot answers there when addressed with
   `/ha`, `!ha` or an @-mention, and only while every member is a configured
   person.
4. **What the bot may change.** Off by default. See [Changes](#changes).
5. **Settings.**
   - Bot name, request age limit, history retention.
   - **Claude model:** Opus by default; Fable is the most capable and uses
     more of your plan.
   - **Notes for the assistant:** trusted context Claude always gets, e.g.
     "Room presence: `pyscript.presence_<name>`, attribute `area`" or what
     your rooms and devices are called.
   - **Test Home Assistant** and **Test Claude** check the connections.

Each person should also enable **Registration Lock** (Signal → Settings →
Account), so their number can't be re-registered by someone else.

## Reading Home Assistant

Claude never talks to Home Assistant directly and never sees an HA
credential. For each request, the bot opens a local read-only tool server
(on 127.0.0.1, with a one-time token) that offers Claude only these tools. It
forwards each call to Home Assistant with constrained arguments:

| `ha_mcp_kind` | Tools Claude gets | Upstream |
|---|---|---|
| `ha_mcp` (recommended) | `search_entities`, `get_states` (≤ 25 entities, with attributes such as temperatures), `get_overview` (states per domain, never your coordinates), `get_history` (≤ 10 entities), `get_dashboard` (list, read or search dashboards; screenshots if ha-mcp's screenshot beta is on), `get_automation_config` / `get_script_config` (sources), `get_image` (pictures of `image.*` entities via HA's image proxy, and `camera.*` snapshots via ha-mcp; ≤ 4 MB) | [ha-mcp](https://github.com/homeassistant-ai/ha-mcp) |
| `hass` | `get_live_context` | HA's built-in **Model Context Protocol Server** (Assist API, read-only option, exposed entities only) |

**ha-mcp must be read-only.** The bot refuses any ha-mcp connection whose
tool list contains write tools. If ha-mcp runs inside Home Assistant (the
"HA-MCP Server" integration, port 9584), use
`http://homeassistant:9584/private_…/readonly` as `ha_mcp_url`. The
`/readonly` suffix makes that connection read-only. Better still is a
separate ha-mcp instance with Read Only Mode on, because whoever knows the
`private_…` URL can drop the suffix. No HA token is sent to ha-mcp.

For `hass`, set `ha_mcp_url` to `http://homeassistant:8123/api/mcp/assist`
and `ha_token` to a non-admin user's token.

## Changes

With **Allow changes** on (setup page) and an `ha_token` set, Claude may
propose a Home Assistant service call on one entity. Each proposal passes
three checks:

1. **Fixed limits (in code, not configurable).** Only these domains can ever
   be controlled: light, switch, fan, cover, climate, humidifier,
   water_heater, media_player, scene, script, input_*, button, vacuum,
   select, number. Locks, alarms, cameras, notifications, automations,
   shell/REST commands and HA administration can't be. The service must
   belong to the entity's domain, the entity must exist in HA, and the data
   must be flat values (no area/device targets).
2. **Your denylist** ("Never touch"): patterns like `cover.garage*`,
   `script.*` or `*.turn_off`, matched against the entity and the service.
3. **The reviewer**: a separate Claude call without tools and without the
   chat history. It sees your **Rules** (plain text, e.g. "Hot water: only
   Manual or Off. Never open the garage door."), who asked, where and when,
   the request, the exact call and the entity's current state, and decides:
   - **deny**: the bot says why and does nothing;
   - **ask**: the bot asks the person who asked, in their private chat:
     "Shall I light.turn_on on light.kitchen (brightness_pct=40)?" plus the
     reviewer's note. Answer "yes" (or "ja", "ok", 👍), react 👍, or "no" /
     👎. With several open questions, reply to the one you mean. Questions
     expire after 2 minutes and work once; nobody else can answer them;
   - **allow**: with **Auto mode** on, the bot does it right away and says
     "Done: … (auto-approved: …)"; with Auto mode off it asks like above.

Claude also sees your rules, so it proposes what fits them. The switch, the
user and the denylist are checked again when a confirmed or scheduled change
runs. A reviewer call takes about 10-20 s with Opus.

**Later and on a schedule.** Ask for a time ("turn on the hot water at
2:45") and the question says "… at Thu 23.10. 02:45". Your yes (or an
auto-approval) schedules it, for up to 7 days ahead. When it's due, the bot checks the switch and the
denylist again, runs it once and tells you the result. If the bot was offline and the
time passed by more than 10 minutes, it doesn't run late; it reports
"missed". Reminders ("remind us at 8 to …") need no confirmation and arrive
in the chat they were asked in. **Tasks** ("tell me every weekday at 7 whether
it will rain", "at 22:00 check which windows are open") run Claude when
they are due: once, daily, on weekdays or weekly. Claude is told that a
scheduled task is firing (not a new message), with who set it up, when, and
the original request. Its answer is posted as "⏰ #12: …" in the chat, or
nothing if the task's condition doesn't hold. Actions it proposes go through
the same checks (the reviewer knows it's a scheduled run); a running task can't create further tasks. Up to 20 per person;
cancelling a repeating task stops the series. `/scheduled` lists your pending items and
`/cancel 12` cancels one (or just ask). Scheduling is done by the bot,
durably, not by Claude: Claude Code's own scheduling only lives as long as
one Claude process, and the bot starts a fresh one per message.

Outcomes: **Done**, **did not perform** (HA rejected the call or was
unreachable), or **UNKNOWN** (timeout, server error, restart mid-call).
Unknown outcomes are reported and never retried.

## Trusted accounts

The bot acts only for configured people, in layers:

1. **Authenticated sender.** Identity comes from the decrypted Signal envelope,
   never from message text, names or numbers. Everyone else is ignored
   (only their ID shows up under "Recently seen").
2. **Pinned, verified identity keys.** signal-cli rejects messages after a
   person's key changes (e.g. their number was re-registered elsewhere). The
   bot additionally requires a verified safety number by default. After
   someone legitimately gets a new phone, verify them again in the setup page.
3. **Groups with only configured members.** Before answering and before every
   group message (including notifications), every member and pending invitee
   must be a configured person. Otherwise the bot stays silent there and tells
   the requester privately.
4. **Fresh, explicit requests.** Requests older than the age limit (default 5
   minutes), judged by the older of the sender's and the server's timestamps,
   are never executed. This covers backlog after downtime and replays. The
   sender is told to resend.

This can't protect against someone who controls a configured person's Signal
account or devices (an unlocked phone, a linked Signal Desktop).

## Message handling

- Claude runs for every private message, and for group messages that start
  with `/ha` or `!ha`, mention `@HomeBot`, or @-mention the bot. Other group
  messages are only kept as context.
- While working, the bot marks your message read, shows "typing…" and posts
  one "⏳ Looking into it…" message. It updates that message as Claude looks
  things up ("🔎 Searching", "📊 Reading", …), then edits it into the answer.
- Commands, which never go to Claude:
  - `/reset` (or `/clear`) forgets this chat's history;
  - `/scheduled` lists your pending items;
  - `/cancel N` cancels one.

  In groups, write them with a leading `/` or `/ha`.
- Each Signal message is processed at most once.
- A request interrupted by a restart is reported as not completed, not
  re-run.
- Replies wait in a durable outbox and are retried for up to an hour. A
  retried send can at worst duplicate a text, never an action.
- Claude gets the last 60 messages of the *same* conversation, both sides
  (its own earlier answers too), labelled with people's names rather than
  IDs, plus the current local date and time (your HA timezone). Group history
  never reaches a private chat.
- "Delete for everyone" removes a message from the archive. Disappearing
  timers are honoured. History is kept for 7 days by default.
- Each answer starts a fresh Claude process (no long-lived Claude
  session). Its instructions and tools are identical every time, so that part
  is prompt-cached. Expect roughly 5–30 seconds, depending on how much it
  needs to look up.

## Process isolation

Inside the container, root only prepares `/data` and supervises. Each service
runs as its own user with no capabilities (`setpriv`, `no_new_privs`) and
only its own environment:

| User | Process | Holds | Data |
|---|---|---|---|
| `signal` | signal-cli daemon (HTTP on 127.0.0.1) | Signal identity keys | `/data/signal` |
| `homebot` | bot, setup page, read-tool proxy | HA token, notify token | `/data/homebot` (settings, SQLite) |
| `node` | Claude broker + Claude Code | Claude OAuth token | `/data/claude` |

The bot reaches Claude only through the broker's Unix socket (peer UID
checked). The broker runs Claude with a fixed command line: no built-in tools
(no shell, files or web), no settings, hooks or plugins, and only the bot's
read-tool server. The signal-cli HTTP port has no authentication of its own
and is reachable only inside this container.

## Proactive notifications (optional, n8n)

With `notify_token` set, `POST /notify` on port 8999 of the app's internal
network sends a message:

```
Authorization: Bearer <notify_token>
{"target": "<person or group name>", "message": "…", "id": "optional-dedupe-key"}
```

`202` means queued; `200 {"duplicate": true}` means that `id` was already
accepted. Use a stable `id` (e.g. the HA event id) so retries don't duplicate.

## On-device checklist

Run through this once with a harmless test light.

1. [ ] The app builds (Supervisor log ends without error). The app log shows
   `Signal crypto library: libsignal OK` and `Ready`.
2. [ ] Setup page: the number registers (or an earlier registration is
   picked up), and the bot ID appears.
3. [ ] **Test Home Assistant** and **Test Claude** both succeed.
4. [ ] Your DM shows up under Recently seen. After adding and verifying
   yourself, "how warm is it in <room>?" gets a correct answer.
5. [ ] A message from a number that isn't configured gets no reply.
6. [ ] (Groups) `/ha` in the group answers. With an outsider added, it
   doesn't, and you get a private note.
7. [ ] Stop the app, send a request, wait > 5 minutes, start. You get
   "too late".
8. [ ] Allow changes (Auto mode off), ask to turn on a test light:
   - "yes" turns it on;
   - a 👍 on the question works too;
   - "no" cancels;
   - waiting more than 2 minutes expires the question.
9. [ ] Restart the app. Everything still works without re-registering.
10. [ ] (n8n) `/notify` without the token gets 401; with it, the message
    arrives once.

## Backups, privacy and limitations

`/data` (Signal keys, Claude login, settings, history) survives restarts and
updates and is part of HA backups. Protect those backups. Messages are sent to
Anthropic when the bot answers. Everyone in a group with the bot should know
and agree. Signal's end-to-end encryption ends at the bot.

Don't run another signal-cli for the same number. If you used the older
`signal_codex` app, this one has a separate `/data` and migrates nothing.

## Test evidence

Verified on 2026-10-22 (see CHANGELOG.md):

- **0.8.0:** 103 tests pass. A real Claude Code run through the streaming
  broker delivered a progress event after 2.5 s, used Opus 5.5, and finished
  in 5.7 s.
- **Prompt caching (measured):** with a fixed working directory, ~2.8k
  tokens came from the cache instead of ~1.1k.

- **Image:** the amd64 build succeeds, with signal-cli 0.14.9 (checksum
  matches GitHub's digest), Claude Code 2.1.306, Debian's Java 25 and locked
  Python dependencies. 85 tests pass inside the image.
- **Real Claude Code 2.1.306 with read tools:** through the broker and the
  bot's read-tool proxy (in front of a fake read-only ha-mcp), real Claude
  Code called `get_states`, answered with the temperature, and proposed
  `light.turn_on` with `brightness_pct: 40` in the new action format, in
  13.7 s. `--safe-mode` was shown to silently drop MCP servers, hence the
  current flag set.
- **Container smoke test** of the real `run.sh` (signal-cli replaced by the
  test fake):
  - an existing registration was picked up automatically;
  - the setup page refused a non-ingress client;
  - all three services ran as separate users without capabilities, and each
    saw only its own secrets;
  - a DM went through the read-tool preflight and the real Claude CLI and
    got a reply;
  - SIGTERM stopped everything.
- **On your HA box (0.5.x):** the Supervisor build, the Celeron J4025
  (x86-64-v2) running libsignal, `setpriv` under HA's AppArmor, and Signal
  registration and verification.
- **Live ha-mcp 8.7.0:** the admin URL is refused, and its `/readonly` alias
  works.

**Not yet verified:**
- on the box: the setup page via ingress, and registration through the
  daemon's API;
- real Signal messages, reactions and quoted replies;
- the read tools against live ha-mcp;
- HA REST writes;
- backups;
- n8n.

Use the checklist above.

## Development

```bash
cd signal_claude
python3 -m unittest discover -s tests -v          # core tests (stdlib only)
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v # all tests
docker build --platform linux/amd64 -t signal-homebot-claude:dev .
```

Regenerate `requirements.lock` inside the base image after changing
`requirements.txt`:

```bash
docker run --rm --platform linux/amd64 -v "$PWD/requirements.txt:/r.txt:ro" \
  node:22-trixie-slim@sha256:8eb619e402eaede1748a3c8173bffab391592812e85136452f781741c7242ddf \
  bash -c 'apt-get update -qq && apt-get install -y -qq python3-venv >/dev/null &&
           python3 -m venv /v && /v/bin/pip install -q -r /r.txt >&2 && /v/bin/pip freeze --all --exclude pip'
```
