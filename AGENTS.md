**Read [HANDOFF.md](HANDOFF.md) before making changes. The handoff is the source of truth for current risks and release criteria.**

# Claude Code development guidance — Signal HomeBot

This repository is an agent connected to **physical home devices**.

- Never let the LLM execute arbitrary MCP tools, shell commands, scripts,
  or HTTP requests; it only proposes structured actions.
- The Python service validates every write against an explicit allowlist.
  Reject all unknown fields, entities and services.
- Keep Signal identifiers authenticated from signal-cli metadata, not text.
- Store all authorized shared-group messages as context; invoke the model
  only for explicit requests. Treat old group messages as untrusted data.
- Keep service tokens, Signal key material and OAuth credentials inaccessible
  to model-controlled process or files. Claude runs as a non-root `node` user.
- Use `claude -p --output-format json --json-schema` with no built-in/MCP tools.
- Don't use `--dangerously-skip-permissions`, `--bare` (which ignores subscription
  login) or `ANTHROPIC_API_KEY` (which could incur separate usage charges).
- Every write needs a single-use DM confirmation; never add an auto-execute
  path. Never retry an HA action whose outcome is `unknown`.
- The bot (not Claude) makes exactly one fixed read-only MCP call per request
  (`GetLiveContext` or ha-mcp `ha_get_overview`). Never call other MCP tools.
- Never log message text, tokens, or MCP URLs (ha-mcp URLs contain a secret).
- Process users: signal-cli = `signal`, bot = `homebot`, Claude broker +
  Claude = `node`; root only in `run.sh`. Never give the bot the Claude token
  or let it run Claude directly; flags live in `bot/claude_broker.py`.
- Senders must have verified identity keys (`listIdentities`), and the group
  roster must be exactly bot + two users before anything is posted there.
- Layout: HA app repository. The app lives in `signal_claude/` (keep the slug:
  changing it orphans the users' `/data`). Bump `version` in
  `signal_claude/config.yaml` for every release users should receive.
- Tests (from `signal_claude/`): `python3 -m unittest discover -s tests -v`
  (core only); install `requirements.lock` in a venv to run the integration
  tests too. `docker build --platform linux/amd64 .` runs the full suite.
- Upstream contracts are pinned and were verified against signal-cli 0.14.9,
  Claude Code 2.1.306, ha-mcp 8.7.0. Re-verify before bumping versions.

Next: on-device smoke checklist (DOCS.md), then live Signal/HA evidence.
