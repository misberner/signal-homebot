# Signal HomeBot — Home Assistant app repository

A household Signal bot for Home Assistant OS. Allowlisted people DM it or
address it in configured groups. Claude Code (subscription auth, no tools) answers from a
read-only Home Assistant MCP snapshot, and can *propose* a small set of
light/climate actions that each need a private, single-use confirmation.

## Install

Settings → Apps → App store → ⋮ → **Repositories**, add
`https://github.com/misberner/signal-homebot`, then install
**Signal HomeBot (Claude)**. Supervisor builds the image on the HA box (amd64
only), and the build runs the test suite. Setup, security model and the
on-device checklist are in [signal_claude/DOCS.md](signal_claude/DOCS.md).

**Status:** 0.5.0, hardened prototype. Not yet tested on a live HAOS
Supervisor, a real Signal account or HA writes. Read-only by default.

### Private repository

Supervisor clones app repositories anonymously over HTTPS, so it can't read a
private GitHub repo as-is. Either make the repository public (it contains no
secrets: tokens, accounts and the policy live in the app's options and config
folder), or add it with a read-only fine-grained token in the URL,
`https://<user>:<token>@github.com/misberner/signal-homebot`. Check on your
Supervisor that the token URL is accepted, and treat the token as a secret.

## Layout

| Path | What |
|---|---|
| `repository.yaml` | HA app repository metadata |
| `signal_claude/` | the app: `config.yaml`, `Dockerfile`, `run.sh`, `bot/`, `tests/`, `DOCS.md`, `CHANGELOG.md` |
| `HANDOFF.md`, `AGENTS.md` | engineering handoff and rules for coding agents |

## Development

```bash
cd signal_claude
python3 -m unittest discover -s tests -v          # core tests (stdlib only)
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock
.venv/bin/python -m unittest discover -s tests -v # all tests
docker build --platform linux/amd64 -t signal-homebot-claude:dev .
```
