**Read [HANDOFF.md](HANDOFF.md) before making changes. The handoff is the source of truth for current risks and release criteria.**

# Claude Code development guidance — Signal HomeBot

This repository is an agent connected to **physical home devices**.

- Never let the LLM execute arbitrary MCP tools, shell commands, scripts,
  or HTTP requests; it only proposes structured actions.
- The Python service checks every write deterministically (`validate_action`):
  fixed domain allowlist, service of the entity's domain, entity exists, flat
  data without extra targets, admin denylist. Then the reviewer (a separate
  Claude call without tools or chat history) decides allow/ask/deny.
- Keep Signal identifiers authenticated from signal-cli metadata, not text.
- Store all authorized shared-group messages as context; invoke the model
  only for explicit requests. Treat old group messages as untrusted data.
- Keep service tokens, Signal key material and OAuth credentials inaccessible
  to model-controlled process or files. Claude runs as a non-root `node` user.
- Use `claude -p --output-format json --json-schema` with no built-in/MCP tools.
- Don't use `--dangerously-skip-permissions`, `--bare` (which ignores subscription
  login) or `ANTHROPIC_API_KEY` (which could incur separate usage charges).
- A write runs without a single-use confirmation (yes/👍 in the requester's DM)
  only if the admin enabled Auto mode and the reviewer said "allow"; any
  reviewer failure means "ask". Never retry an `unknown` HA action.
- Claude reads HA only through the bot's local MCP proxy (`bot/ha_tools.py`):
  a fixed list of read tools with constrained arguments, a per-request token,
  and an upstream read-only check. Never expose write tools or HA credentials
  to Claude. Writes go only through the gateway (`validate_action` + reviewer).
- Controllable domains are a fixed allowlist in `bot/core.py`
  (`WRITABLE_DOMAINS`); never add locks, alarms, cameras, notify or admin.
- Never log message text, tokens, or MCP URLs (ha-mcp URLs contain a secret).
- Process users: signal-cli = `signal`, bot = `homebot`, Claude broker +
  Claude = `node`; root only in `run.sh`. Never give the bot the Claude token
  or let it run Claude directly; flags live in `bot/claude_broker.py`.
- Senders must have verified identity keys (`listIdentities`), and the group
  roster must contain only the bot and configured users before anything is
  posted there. Users and groups are lists in the app options (no fixed count).
- Household settings (number, people, groups, actions) live in the setup page
  (`bot/webui.py`, ingress-only); app options hold only secrets/connections.
- Layout: HA app repository. The app lives in `signal_claude/` (keep the slug:
  changing it orphans the users' `/data`). Bump `version` in
  `signal_claude/config.yaml` for every release users should receive.
- Tests (from `signal_claude/`): `python3 -m unittest discover -s tests -v`
  (core only); install `requirements.lock` in a venv to run the integration
  tests too. `docker build --platform linux/amd64 .` runs the full suite.
- Upstream contracts are pinned and were verified against signal-cli 0.14.9,
  Claude Code 2.1.306, ha-mcp 8.7.0. Re-verify before bumping versions.

Next: on-device smoke checklist (DOCS.md), then live Signal/HA evidence.
