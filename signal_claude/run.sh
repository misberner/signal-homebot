#!/usr/bin/env bash
set -euo pipefail
# Root only prepares /data and supervises. Every long-running process gets its
# own unprivileged user and only the environment it needs:
#   signal  - signal-cli daemon, owns /data/signal (identity keys)
#   node    - Claude broker + Claude Code, owns /data/claude and the OAuth token
#   homebot - the bot and its setup page, owns /data/homebot (settings, SQLite),
#             holds the HA/notify tokens (incl. personal HA tokens)
#   render  - headless Chromium drawing Home Assistant cards; holds ha_token
#             (to log in to the frontend), no files
# Household settings (number, people, groups, actions) are managed in the
# setup page (Open Web UI); the app options hold only secrets and connections.
umask 077
cd /opt/homebot
# Values emitted by this script are shell quoted. Do not echo them into the logs.
eval "$(python3 scripts/options_to_env.py)"

BASE_PATH="/opt/homebot/.venv/bin:/usr/local/bin:/usr/bin:/bin"
# Supervisor sets TZ to Home Assistant's timezone; keep it across `env -i`.
TZ="${TZ:-UTC}"
CLAUDE_SOCKET=/run/homebot-claude/claude.sock

# Replace the current (sub)shell with "$@" running as a user with no
# capabilities and no way to regain privileges. Start long-running services as
# `run_as ... &` so that $! is the service itself, not a wrapper shell.
run_as() {
  local user=$1; shift
  exec setpriv --reuid="$user" --regid="$user" --init-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs -- "$@"
}
as_user() { ( run_as "$@" ); }

mkdir -p /data/signal /data/claude/config /data/homebot /run/homebot-claude /run/homebot-render
# 0.3.x kept the database directly in /data (root-owned); move it once.
for f in /data/bot.sqlite3 /data/bot.sqlite3-wal /data/bot.sqlite3-shm; do
  if [[ -e "$f" && ! -e "/data/homebot/${f#/data/}" ]]; then mv "$f" /data/homebot/; fi
done
rm -f /data/bot.sqlite3.lock
chown -R signal:signal /data/signal
chown -R node:node /data/claude
chown -R homebot:homebot /data/homebot
chmod 700 /data/signal /data/claude /data/claude/config /data/homebot
chmod 600 /data/options.json
chmod 600 /data/homebot/bot.sqlite3* /data/homebot/settings.json 2>/dev/null || true
# Only node and homebot may reach the broker socket.
chown node:homebot /run/homebot-claude && chmod 750 /run/homebot-claude
chown render:homebot /run/homebot-render && chmod 750 /run/homebot-render

echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
echo -n "Signal crypto library: "
as_user signal env -i PATH="$BASE_PATH" HOME=/data/signal java -cp "/opt/signal-cli/lib/*" \
  /opt/homebot/scripts/LibsignalCheck.java 2>&1 | tail -1
echo "Claude Code: $(claude --version 2>/dev/null || echo missing)"

pids=()
cleanup() {
  # Stop the bot first so it can finish its current SQLite transaction.
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT TERM INT

# Multi-account mode: the setup page registers the number through the daemon.
# --no-receive-stdout: signal-cli otherwise prints every received message,
# including its text, into the app log.
echo "Starting Signal daemon"
run_as signal env -i PATH="$BASE_PATH" HOME=/data/signal LANG=C.UTF-8 \
  signal-cli --config /data/signal daemon --http=127.0.0.1:7583 --no-receive-stdout &
sig_pid=$!
for n in $(seq 1 90); do
  if curl -fsS http://127.0.0.1:7583/api/v1/check >/dev/null 2>&1; then break; fi
  if ! kill -0 "$sig_pid" 2>/dev/null; then echo 'Signal daemon crashed' >&2; exit 1; fi
  sleep 2
done
curl -fsS http://127.0.0.1:7583/api/v1/check >/dev/null

broker_pid=''
if [[ -n "${CLAUDE_OAUTH_TOKEN}" ]]; then
  echo 'Starting Claude broker'
  run_as node env -i PATH="$BASE_PATH" HOME=/data/claude LANG=C.UTF-8 TZ="$TZ" PYTHONUNBUFFERED=1 \
    CLAUDE_HOME=/data/claude CLAUDE_SOCKET="$CLAUDE_SOCKET" BROKER_ALLOWED_UID="$(id -u homebot)" \
    CLAUDE_MCP_URL=http://127.0.0.1:8765/mcp \
    CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_OAUTH_TOKEN}" python3 -m bot.claude_broker &
  broker_pid=$!
  for n in $(seq 1 50); do [[ -S "$CLAUDE_SOCKET" ]] && break; sleep 0.2; done
  if [[ ! -S "$CLAUDE_SOCKET" ]]; then echo 'Claude broker did not start' >&2; exit 1; fi
else
  echo 'No claude_oauth_token set: the bot will answer that Claude is not configured.'
fi

render_pid=''
if [[ -n "${HA_TOKEN}" ]]; then
  echo 'Starting render broker'
  run_as render env -i PATH="$BASE_PATH" HOME=/tmp LANG=C.UTF-8 TZ="$TZ" PYTHONUNBUFFERED=1 \
    RENDER_SOCKET=/run/homebot-render/render.sock BROKER_ALLOWED_UID="$(id -u homebot)" \
    HA_URL="${HA_BASE_URL}" HA_TOKEN="${HA_TOKEN}" CHROMIUM=/usr/bin/chromium \
    python3 -m bot.render_broker &
  render_pid=$!
  for n in $(seq 1 50); do [[ -S /run/homebot-render/render.sock ]] && break; sleep 0.2; done
fi

echo 'Starting HomeBot (setup page: Open Web UI)'
run_as homebot env -i PATH="$BASE_PATH" HOME=/data/homebot LANG=C.UTF-8 TZ="$TZ" PYTHONUNBUFFERED=1 \
  SIGNAL_DAEMON_URL=http://127.0.0.1:7583 \
  HA_BASE_URL="${HA_BASE_URL}" HA_MCP_URL="${HA_MCP_URL}" HA_MCP_KIND="${HA_MCP_KIND}" \
  HA_MCP_DOMAINS="${HA_MCP_DOMAINS}" HA_TOKEN="${HA_TOKEN}" HA_USER_TOKENS="${HA_USER_TOKENS}" \
  NOTIFY_TOKEN="${NOTIFY_TOKEN}" \
  NOTIFY_BIND="${NOTIFY_BIND:-0.0.0.0}" NOTIFY_PORT="${NOTIFY_PORT:-8999}" \
  BOT_DB=/data/homebot/bot.sqlite3 SETTINGS_FILE=/data/homebot/settings.json \
  CLAUDE_SOCKET="$CLAUDE_SOCKET" CLAUDE_CONFIGURED="$([[ -n "${CLAUDE_OAUTH_TOKEN}" ]] && echo true || echo false)" \
  RENDER_SOCKET=/run/homebot-render/render.sock RENDER_CONFIGURED="$([[ -n "${render_pid}" ]] && echo true || echo false)" \
  python3 -m bot.server &
bot_pid=$!
pids=("$bot_pid" ${broker_pid:+"$broker_pid"} ${render_pid:+"$render_pid"} "$sig_pid")
# If any process stops, terminate and let Supervisor manage restarts.
wait -n "${pids[@]}"
echo 'One of the services stopped' >&2
exit 1
