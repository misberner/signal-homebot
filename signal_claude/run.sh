#!/usr/bin/env bash
set -euo pipefail
# Root only prepares /data and supervises. Every long-running process gets its
# own unprivileged user and only the environment it needs:
#   signal  - signal-cli daemon, owns /data/signal (identity keys)
#   node    - Claude broker + Claude Code, owns /data/claude and the OAuth token
#   homebot - the bot, owns /data/homebot (SQLite), holds the HA/notify tokens
umask 077
cd /opt/homebot
# Values emitted by this script are shell quoted. Do not echo them into the logs.
eval "$(python3 scripts/options_to_env.py)"

BASE_PATH="/opt/homebot/.venv/bin:/usr/local/bin:/usr/bin:/bin"
CLAUDE_SOCKET=/run/homebot-claude/claude.sock
BOT_DB=/data/homebot/bot.sqlite3
POLICY_FILE=/run/homebot/policy.json

# Replace the current (sub)shell with "$@" running as a user with no
# capabilities and no way to regain privileges. Start long-running services as
# `run_as ... &` so that $! is the service itself, not a wrapper shell.
run_as() {
  local user=$1; shift
  exec setpriv --reuid="$user" --regid="$user" --init-groups \
    --inh-caps=-all --bounding-set=-all --no-new-privs -- "$@"
}
as_user() { ( run_as "$@" ); }
signal_env=(env -i PATH="$BASE_PATH" HOME=/data/signal LANG=C.UTF-8 signal-cli --config /data/signal)
signal_cli() { as_user signal "${signal_env[@]}" "$@"; }

mkdir -p /data/signal /data/claude/config /data/homebot /run/homebot /run/homebot-claude
# 0.3.x kept the database directly in /data (root-owned); move it once.
for f in /data/bot.sqlite3 /data/bot.sqlite3-wal /data/bot.sqlite3-shm; do
  if [[ -e "$f" && ! -e "/data/homebot/${f#/data/}" ]]; then mv "$f" /data/homebot/; fi
done
rm -f /data/bot.sqlite3.lock
chown -R signal:signal /data/signal
chown -R node:node /data/claude
chown -R homebot:homebot /data/homebot
chmod 600 /data/homebot/bot.sqlite3* 2>/dev/null || true
chmod 700 /data/signal /data/claude /data/claude/config /data/homebot
chmod 600 /data/options.json
# Only node and homebot may reach the broker socket.
chown node:homebot /run/homebot-claude && chmod 750 /run/homebot-claude
chown root:homebot /run/homebot && chmod 750 /run/homebot

# Policy: admin-editable copy in the add-on config folder (Samba/File editor:
# addon_configs/<slug>/policy.json). Created from the safe default on first start.
if [[ -d /config ]]; then
  if [[ ! -f /config/policy.json ]]; then
    install -m 644 /opt/homebot/policy.json /config/policy.json
    echo "Created /config/policy.json (grants no writes); edit it and restart to allow devices."
  fi
  install -m 640 -g homebot /config/policy.json "$POLICY_FILE"
else
  install -m 640 -g homebot /opt/homebot/policy.json "$POLICY_FILE"
fi

case "${SETUP_MODE}" in
  register)
    : "${SIGNAL_ACCOUNT:?Set signal_phone to E.164 number}"
    echo "Requesting Signal registration SMS for ${SIGNAL_ACCOUNT}..."
    args=()
    if [[ -n "${SIGNAL_CAPTCHA}" ]]; then args+=(--captcha "${SIGNAL_CAPTCHA}"); fi
    # signal-cli prints nothing on success, so say what happened.
    if signal_cli -a "${SIGNAL_ACCOUNT}" register "${args[@]}"; then
      echo "OK: Signal sent a verification code to ${SIGNAL_ACCOUNT}."
      echo "Next: put it in verification_code, set setup_mode to verify, and start again."
    else
      echo "Registration request FAILED (see the message above)." >&2
      exit 1
    fi
    ;;
  verify)
    : "${SIGNAL_ACCOUNT:?Set signal_phone}"
    : "${VERIFY_CODE:?Set verification_code from SMS}"
    echo "Verifying Signal registration..."
    if signal_cli -a "${SIGNAL_ACCOUNT}" verify "${VERIFY_CODE}"; then
      echo "OK: ${SIGNAL_ACCOUNT} is registered; this app is now its primary Signal device."
      echo "Next: clear verification_code and signal_captcha, then run setup_mode inspect."
    else
      echo "Verification FAILED (see the message above). Codes expire; request a new one with register." >&2
      exit 1
    fi
    ;;
  trust)
    : "${SIGNAL_ACCOUNT:?Set signal_phone}"
    echo "Compare each safety number with the one on that person's phone (Signal: conversation"
    echo "with the bot > View safety number). Only if they match, enter it as that user's"
    echo "safety_number and run this mode again. signal-cli refuses numbers that do not match."
    while IFS=$'\t' read -r name aci sn; do
      sn="${sn// /}"
      echo "${name} (${aci}):"
      if [[ -n "$sn" ]]; then
        if signal_cli -a "${SIGNAL_ACCOUNT}" trust -v "$sn" "$aci" < /dev/null; then
          echo "  verified"
        else
          echo "  NOT verified: the entered safety number does not match" >&2
        fi
      fi
      signal_cli -a "${SIGNAL_ACCOUNT}" --output=json listIdentities -n "$aci" < /dev/null \
        | python3 scripts/show_identities.py
    done < <(python3 -c 'import json,sys
for u in json.loads(sys.argv[1]):
    print(u.get("name", ""), u.get("aci", ""), u.get("safety_number") or "", sep="\t")' "${USERS_JSON}")
    ;;
  claude_check)
    echo "Checking Claude Code installation and configured subscription authentication..."
    : "${CLAUDE_OAUTH_TOKEN:?Set claude_oauth_token from claude setup-token on your workstation}"
    # Never log the token. Only the authentication method is displayed.
    as_user node env -i PATH="$BASE_PATH" HOME=/data/claude CLAUDE_CONFIG_DIR=/data/claude/config \
      CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_OAUTH_TOKEN}" DISABLE_AUTOUPDATER=1 claude auth status --text
    ;;
  listen)
    : "${SIGNAL_ACCOUNT:?Set signal_phone}"
    echo "Send two test messages to the bot within the next 60 seconds. Only IDs are logged."
    signal_cli --output=json -a "${SIGNAL_ACCOUNT}" receive --timeout 60 \
      | python3 -u scripts/show_sender_ids.py
    ;;
  inspect)
    echo "CPU: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2- | sed 's/^ *//')"
    echo "x86-64 levels: $(/lib64/ld-linux-x86-64.so.2 --help | grep -oE 'x86-64-v[0-9] \(supported' | cut -d' ' -f1 | tr '\n' ' ')"
    echo -n "Signal crypto library: "
    as_user signal env -i PATH="$BASE_PATH" HOME=/data/signal java -cp "/opt/signal-cli/lib/*" \
      /opt/homebot/scripts/LibsignalCheck.java 2>&1 | tail -1
    echo "Signal accounts (registration must be completed first):"
    signal_cli --output=json listAccounts || true
    if [[ -n "${SIGNAL_ACCOUNT}" ]]; then
      echo "Bot account ID (put into signal_bot_aci):"
      signal_cli --output=json listAccounts 2>/dev/null | python3 -c 'import json,sys
for a in json.load(sys.stdin):
    print("  ", a.get("uuid") or a.get("aci") or a)' || true
    fi
    if [[ -n "${SIGNAL_ACCOUNT}" ]]; then
      echo "Signal groups:"
      signal_cli -a "${SIGNAL_ACCOUNT}" --output=json listGroups || true
    fi
    echo "Claude Code CLI:"
    claude --version || true
    echo "Claude subscription configured: ${CLAUDE_OAUTH_TOKEN:+yes}"
    echo "No daemon started; change setup_mode to run when ready."
    ;;
  run)
    # NOTIFY_TOKEN is optional: without it the /notify webhook is disabled.
    # HA_TOKEN is needed for writes and for the built-in HA MCP server, not for
    # read-only use of ha-mcp (whose secret is part of ha_mcp_url).
    # At least one entry in users is required (checked by the bot); groups are optional.
    required=(SIGNAL_ACCOUNT SIGNAL_BOT_ACI HA_MCP_URL CLAUDE_OAUTH_TOKEN)
    if [[ "${ALLOW_WRITES}" == true || "${HA_MCP_KIND}" == hass ]]; then required+=(HA_TOKEN); fi
    for key in "${required[@]}"; do
      if [[ -z "${!key:-}" ]]; then echo "Missing ${key}; check app configuration" >&2; exit 2; fi
    done
    pids=()
    cleanup() {
      # Stop the bot first so it can finish its current SQLite transaction.
      for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; done
    }
    trap cleanup EXIT TERM INT

    echo "Starting Signal daemon (primary account ${SIGNAL_ACCOUNT})"
    run_as signal "${signal_env[@]}" -a "${SIGNAL_ACCOUNT}" daemon --http=127.0.0.1:7583 &
    sig_pid=$!
    for n in $(seq 1 60); do
      if curl -fsS http://127.0.0.1:7583/api/v1/check >/dev/null 2>&1; then break; fi
      if ! kill -0 "$sig_pid" 2>/dev/null; then echo 'Signal daemon crashed' >&2; exit 1; fi
      sleep 2
    done
    curl -fsS http://127.0.0.1:7583/api/v1/check >/dev/null

    echo 'Starting Claude broker'
    run_as node env -i PATH="$BASE_PATH" HOME=/data/claude LANG=C.UTF-8 PYTHONUNBUFFERED=1 \
      CLAUDE_HOME=/data/claude CLAUDE_SOCKET="$CLAUDE_SOCKET" BROKER_ALLOWED_UID="$(id -u homebot)" \
      CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_OAUTH_TOKEN}" python3 -m bot.claude_broker &
    broker_pid=$!
    for n in $(seq 1 50); do [[ -S "$CLAUDE_SOCKET" ]] && break; sleep 0.2; done
    if [[ ! -S "$CLAUDE_SOCKET" ]]; then echo 'Claude broker did not start' >&2; exit 1; fi

    echo 'Starting HomeBot'
    run_as homebot env -i PATH="$BASE_PATH" HOME=/data/homebot LANG=C.UTF-8 PYTHONUNBUFFERED=1 \
      SIGNAL_DAEMON_URL=http://127.0.0.1:7583 SIGNAL_ACCOUNT="${SIGNAL_ACCOUNT}" \
      SIGNAL_BOT_ACI="${SIGNAL_BOT_ACI}" USERS_JSON="${USERS_JSON}" GROUPS_JSON="${GROUPS_JSON}" \
      BOT_NAME="${BOT_NAME}" \
      HA_BASE_URL="${HA_BASE_URL}" HA_MCP_URL="${HA_MCP_URL}" HA_MCP_KIND="${HA_MCP_KIND}" \
      HA_MCP_DOMAINS="${HA_MCP_DOMAINS}" HA_TOKEN="${HA_TOKEN}" NOTIFY_TOKEN="${NOTIFY_TOKEN}" \
      NOTIFY_BIND="${NOTIFY_BIND:-0.0.0.0}" NOTIFY_PORT="${NOTIFY_PORT:-8999}" BOT_DB="$BOT_DB" \
      ALLOW_WRITES="${ALLOW_WRITES}" REQUIRE_VERIFIED_IDENTITY="${REQUIRE_VERIFIED_IDENTITY}" COMMAND_MAX_AGE="${COMMAND_MAX_AGE}" HISTORY_DAYS="${HISTORY_DAYS}" \
      POLICY_FILE="$POLICY_FILE" CLAUDE_SOCKET="$CLAUDE_SOCKET" python3 -m bot.server &
    bot_pid=$!
    pids=("$bot_pid" "$broker_pid" "$sig_pid")
    # If any process stops, terminate and let Supervisor manage restarts.
    wait -n "$sig_pid" "$broker_pid" "$bot_pid"
    echo 'One of the services stopped' >&2
    exit 1
    ;;
  *) echo "Unknown setup_mode: ${SETUP_MODE}" >&2; exit 2;;
esac
