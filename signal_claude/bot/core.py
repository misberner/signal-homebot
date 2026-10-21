"""Pure logic and SQLite state. All network/process effects live in server.py.

Security model (see HANDOFF.md section 4):
- Sender/group identity comes only from signal-cli envelope metadata.
- Old, future-dated or disappeared messages never trigger Claude or writes.
- Claude may only *propose* actions. Every proposal is re-validated here
  against a fixed action table and the policy, and every write requires a
  single-use confirmation from the requesting user's private DM.
"""
from __future__ import annotations
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT = 4000
APPROVAL_TTL = 120
OUTBOX_MAX_AGE = 3600
OUTBOX_MAX_ATTEMPTS = 8
OBJECT_REPLACEMENT = '￼'  # Signal puts this where an @mention was

# The only HA services the gateway can ever call. Policy files can narrow
# this (entities, senders, contexts) but cannot add services. Locks, alarms,
# garage doors, cameras and administration are deliberately absent.
ACTIONS = {
    'light_on': 'light.turn_on',
    'light_off': 'light.turn_off',
    'temperature_set': 'climate.set_temperature',
}
NAME_RE = r'[a-z][a-z0-9_]{0,31}'  # user and group names (options, policy, notify)
CONTEXTS = ('dm', 'group')


class Denied(ValueError):
    """A proposed action is not permitted. The message is safe to show users."""


@dataclass(frozen=True)
class Incoming:
    sender: str
    conv: str
    group: bool
    timestamp: int  # sender-set ms timestamp; also the Signal message id
    text: str
    expires_in_seconds: int = 0
    server_timestamp: int = 0  # ms, set by the Signal server
    mentions_bot: bool = False


@dataclass(frozen=True)
class RemoteDelete:
    sender: str
    conv: str
    target_timestamp: int


@dataclass(frozen=True)
class Settings:
    command_max_age: int = 300   # seconds; older commands are context only
    clock_skew: int = 120        # seconds a timestamp may lie in the future
    history_days: int = 7


def parse_signal_event(event: dict, bot_aci: str, users: set[str], group_ids: set[str],
                       bot_name: str = 'HomeBot') -> Incoming | RemoteDelete | None:
    """Never trust the text for identity. Use Signal metadata from signal-cli.

    Accepts the signal-cli HTTP SSE payload ``{"account", "envelope"}`` and the
    JSON-RPC ``receive`` notification ``{"method", "params": {...}}``. Ignores
    receipts, typing, sync/echo, edits, stories, attachment-only messages and
    unknown senders/groups.
    """
    if not isinstance(event, dict):
        return None
    payload = event.get('params') if event.get('method') == 'receive' else event
    if not isinstance(payload, dict) or payload.get('exception'):
        return None
    envelope = payload.get('envelope')
    if not isinstance(envelope, dict):
        return None
    dm = envelope.get('dataMessage')
    sender = envelope.get('sourceUuid')
    if not isinstance(dm, dict) or not isinstance(sender, str):
        return None
    sender = sender.lower()
    if not sender or sender == bot_aci.lower() or sender not in users:
        return None
    grp = dm.get('groupInfo')
    if grp is not None:
        group_id = grp.get('groupId') if isinstance(grp, dict) else None
        if not isinstance(group_id, str) or group_id not in group_ids:
            return None
        if grp.get('type') not in (None, 'DELIVER'):
            return None  # group updates, not messages
        conv, is_group = f'group:{group_id}', True
    else:
        conv, is_group = f'dm:{sender}', False

    deletion = dm.get('remoteDelete')
    if deletion is not None:
        target = deletion.get('targetTimestamp') if isinstance(deletion, dict) else None
        if not _valid_ts(target):
            return None
        return RemoteDelete(sender, conv, target)

    text = dm.get('message')
    if not isinstance(text, str) or not text.strip() or dm.get('viewOnce'):
        return None
    ts = dm.get('timestamp')
    server_ts = envelope.get('serverReceivedTimestamp') or 0
    if not _valid_ts(ts) or not isinstance(server_ts, int) or server_ts < 0:
        return None
    expires = dm.get('expiresInSeconds') or 0
    if not isinstance(expires, int) or expires < 0:
        return None
    text, mentions_bot = _resolve_mentions(text, dm.get('mentions'), bot_aci, bot_name)
    return Incoming(sender, conv, is_group, ts, text.strip()[:MAX_TEXT], expires,
                    server_ts, mentions_bot)


def _valid_ts(ts: object) -> bool:
    return isinstance(ts, int) and not isinstance(ts, bool) and ts > 0


def _resolve_mentions(text: str, mentions: object, bot_aci: str,
                      bot_name: str) -> tuple[str, bool]:
    """Replace Signal mention placeholders; report whether the bot was mentioned."""
    if not isinstance(mentions, list):
        return text, False
    found = False
    for m in sorted((m for m in mentions if isinstance(m, dict)),
                    key=lambda m: m.get('start', -1), reverse=True):
        start, length = m.get('start'), m.get('length')
        if not isinstance(start, int) or not isinstance(length, int) or start < 0:
            continue
        is_bot = str(m.get('uuid') or '').lower() == bot_aci.lower()
        found = found or is_bot
        label = '@' + (bot_name if is_bot else str(m.get('name') or 'someone'))
        if text[start:start + length] == OBJECT_REPLACEMENT * length:
            text = text[:start] + label + text[start + length:]
    return text, found


def addressed(text: str, group: bool, bot_name: str, mentions_bot: bool = False) -> bool:
    if not group or mentions_bot:
        return True
    return bool(re.search(r'^(?:/ha|!ha)(?:\s|$)', text, re.I) or
                re.search(r'(?<!\w)@' + re.escape(bot_name) + r'\b', text, re.I))


def freshness(msg: Incoming, now_ms: int, settings: Settings) -> str:
    """Classify an incoming message against the event-age policy.

    Returns one of:
      'future'  - timestamp beyond allowed clock skew: drop, never store.
      'expired' - disappearing message already expired: drop, never store.
      'ancient' - older than history retention: drop, never store.
      'stale'   - store as context, but never invoke Claude or confirm.
      'fresh'   - may be acted upon.

    Age uses the *older* of the sender and server timestamps, so neither a
    message queued on a phone nor a replayed/backlogged one counts as fresh.
    """
    stamps = [msg.timestamp] + ([msg.server_timestamp] if msg.server_timestamp else [])
    if max(stamps) > now_ms + settings.clock_skew * 1000:
        return 'future'
    oldest = min(stamps)
    if msg.expires_in_seconds and oldest + msg.expires_in_seconds * 1000 <= now_ms:
        return 'expired'
    age = now_ms - oldest
    if age > settings.history_days * 86400 * 1000:
        return 'ancient'
    if age > settings.command_max_age * 1000:
        return 'stale'
    return 'fresh'


# ---------------------------------------------------------------- trust

def identity_trusted(identities: object, aci: str, require_verified: bool) -> bool:
    """Check a signal-cli ``listIdentities`` result for one sender.

    signal-cli itself rejects messages whose identity key changed since it was
    first seen (``UNTRUSTED``). With ``require_verified`` the key must also have
    been verified by safety number (``trust -v``), not just trusted on first use.
    """
    if not isinstance(identities, list):
        return False
    accepted = {'TRUSTED_VERIFIED'} if require_verified else {'TRUSTED_VERIFIED', 'TRUSTED_UNVERIFIED'}
    levels = [i.get('trustLevel') for i in identities
              if isinstance(i, dict) and str(i.get('uuid') or '').lower() == aci.lower()]
    # Every known key for this ACI must be acceptable; one bad key fails closed.
    return bool(levels) and all(level in accepted for level in levels)


def group_roster_problem(groups: object, group_id: str, bot_aci: str, users: set[str]) -> str | None:
    """Check a signal-cli ``listGroups`` result before replying in a group.

    Every member and pending invitee must be the bot or a configured user:
    anyone else would read the bot's replies (home state and the conversation
    history). Returns a reason, or None if the roster is safe.
    """
    if not isinstance(groups, list):
        return 'group lookup failed'
    group = next((g for g in groups if isinstance(g, dict) and g.get('id') == group_id), None)
    if group is None:
        return 'group not found'
    if group.get('isMember') is not True or group.get('isBlocked') or group.get('isTerminated'):
        return 'bot is not an active member'
    members = group.get('members')
    if not isinstance(members, list):
        return 'member list missing'
    allowed = {bot_aci.lower(), *users}
    uuids = [str(m.get('uuid') or '').lower() if isinstance(m, dict) else '' for m in members]
    if bot_aci.lower() not in uuids:
        return 'bot is not listed as a member'
    if any(u not in allowed for u in uuids):
        return 'someone other than the configured users is a member'
    pending = group.get('pendingMembers') or []
    if not isinstance(pending, list) or any(
            not isinstance(m, dict) or str(m.get('uuid') or '').lower() not in users for m in pending):
        return 'someone other than the configured users is invited'
    return None


# ---------------------------------------------------------------- policy

def load_policy(raw: object) -> dict:
    """Strictly parse policy.json. Unknown keys or services fail closed."""
    if not isinstance(raw, dict) or set(raw) - {'actions'}:
        raise ValueError('policy.json must be {"actions": {...}}')
    actions = raw.get('actions', {})
    if not isinstance(actions, dict):
        raise ValueError('policy actions must be an object')
    policy = {}
    for kind, rule in actions.items():
        if kind not in ACTIONS:
            raise ValueError(f'Unsupported action type in policy: {kind}')
        allowed = {'entities', 'senders', 'contexts'}
        if kind == 'temperature_set':
            allowed |= {'temperature_min', 'temperature_max'}
        if not isinstance(rule, dict) or set(rule) - allowed:
            raise ValueError(f'Unexpected keys in policy for {kind}')
        domain = ACTIONS[kind].split('.')[0]
        entities = rule.get('entities', [])
        senders = rule.get('senders', [])
        contexts = rule.get('contexts', ['dm'])
        if (not isinstance(entities, list) or
                not all(isinstance(e, str) and re.fullmatch(re.escape(domain) + r'\.[a-z0-9_]+', e)
                        for e in entities)):
            raise ValueError(f'{kind}: entities must be {domain}.* entity IDs')
        if not isinstance(senders, list) or not all(isinstance(n, str) and re.fullmatch(NAME_RE, n)
                                                    for n in senders):
            raise ValueError(f'{kind}: senders must be a list of user names')
        if not isinstance(contexts, list) or set(contexts) - set(CONTEXTS):
            raise ValueError(f'{kind}: contexts must be a subset of {CONTEXTS}')
        entry = {'entities': list(entities), 'senders': list(senders), 'contexts': list(contexts)}
        if kind == 'temperature_set':
            lo, hi = rule.get('temperature_min'), rule.get('temperature_max')
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lo, hi)) \
                    or not 5 <= lo <= hi <= 30:
                raise ValueError('temperature_set needs 5 <= temperature_min <= temperature_max <= 30')
            entry.update(temperature_min=lo, temperature_max=hi)
        policy[kind] = entry
    return policy


def permitted(policy: dict, writes_enabled: bool, role: str | None, context: str) -> dict:
    """The subset of the policy usable by this sender in this context."""
    if not writes_enabled or not role:
        return {}
    return {kind: rule for kind, rule in policy.items()
            if rule['entities'] and role in rule['senders'] and context in rule['contexts']}


def validate_action(action: object, allowed: dict) -> tuple[str, dict]:
    """Deterministic, fail-closed mapping from a proposed action to a HA call.

    ``allowed`` must come from ``permitted()`` for the authenticated sender.
    """
    if not isinstance(action, dict) or set(action) - {'type', 'entity_id', 'temperature'}:
        raise Denied('unexpected action fields')
    kind = action.get('type')
    if not isinstance(kind, str) or kind not in ACTIONS or kind not in allowed:
        raise Denied('action type not permitted')
    rule = allowed[kind]
    entity_id = action.get('entity_id')
    if not isinstance(entity_id, str) or entity_id not in rule['entities']:
        raise Denied('entity not permitted')
    data: dict = {'entity_id': entity_id}
    temp = action.get('temperature')
    if kind == 'temperature_set':
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            raise Denied('temperature required')
        if not rule['temperature_min'] <= temp <= rule['temperature_max']:
            raise Denied('temperature outside configured limits')
        data['temperature'] = temp
    elif temp is not None:
        raise Denied('unexpected temperature')
    return ACTIONS[kind], data


def describe(service: str, data: dict) -> str:
    extra = f' to {data["temperature"]}°' if 'temperature' in data else ''
    return f'{service} on {data["entity_id"]}{extra}'


def build_schema(allowed: dict) -> dict:
    """JSON schema for Claude's structured output.

    No ``$schema`` key: Claude Code 2.1.x rejects draft 2020-12 references.
    When no writes are permitted, ``actions`` must be empty.
    """
    item = {
        'type': 'object',
        'properties': {
            'type': {'type': 'string', 'enum': sorted(allowed) or ['none']},
            'entity_id': {'type': 'string'},
            'temperature': {'type': ['number', 'null']},
        },
        'required': ['type', 'entity_id', 'temperature'],
        'additionalProperties': False,
    }
    actions = {'type': 'array', 'items': item, 'maxItems': 3 if allowed else 0}
    return {
        'type': 'object',
        'properties': {'reply': {'type': 'string'}, 'actions': actions},
        'required': ['reply', 'actions'],
        'additionalProperties': False,
    }


# ---------------------------------------------------------------- HA reads

def ha_mcp_overview_args(domains: list[str]) -> dict:
    """The one fixed, read-only ha-mcp call. ``system_info`` (location) and
    notifications are deliberately excluded. Verified against ha-mcp 8.7.0."""
    return {'detail_level': 'full', 'domains': list(domains), 'limit': 300,
            'include_entity_id': True, 'include_state': True, 'include_notifications': False,
            'fields': ['domain_stats', 'pagination', 'partial', 'warnings']}


def render_overview(text: str, require_read_only: bool = True) -> str:
    """Turn an ha-mcp ``ha_get_overview`` JSON result into compact state lines.

    Only entity ids, names and states pass through; anything unexpected fails closed.
    """
    data = json.loads(text)
    stats = data.get('domain_stats') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('success') is not True or not isinstance(stats, dict):
        raise ValueError('unexpected ha_get_overview result')
    # ha-mcp >= 8.x adds this after any fields= projection while Read Only
    # Mode (global setting or the /readonly endpoint) is active.
    if require_read_only and data.get('read_only_mode') is not True:
        raise ValueError('ha-mcp is not in Read Only Mode')
    lines = []
    for domain, info in sorted(stats.items()):
        if not isinstance(info, dict):
            raise ValueError('unexpected domain_stats entry')
        summary = info.get('states_summary') or {}
        counts = ', '.join(f'{k} {v}' for k, v in summary.items()) if isinstance(summary, dict) else ''
        lines.append(f'## {domain} ({info.get("count")}: {counts})')
        for entity in info.get('entities') or []:
            if isinstance(entity, dict) and isinstance(entity.get('entity_id'), str):
                name = str(entity.get('friendly_name') or '')[:80]
                lines.append(f'{entity["entity_id"]} ({name}): {str(entity.get("state"))[:40]}')
        if info.get('truncated'):
            lines.append('(more entities not shown)')
    pagination = data.get('pagination')
    if isinstance(pagination, dict) and pagination.get('has_more'):
        lines.append('(entity list truncated)')
    return '\n'.join(lines)


# ---------------------------------------------------------------- claude

class ClaudeError(RuntimeError):
    """kind: auth | limit | unavailable | timeout | invalid | config"""

    def __init__(self, kind: str, detail: str = ''):
        super().__init__(f'{kind}: {detail}' if detail else kind)
        self.kind = kind


def parse_claude_output(stdout: bytes, returncode: int = 0) -> dict:
    """Validate the Claude CLI JSON envelope before anything reaches the gateway.

    Claude Code writes a JSON envelope even when it exits non-zero (for example
    ``api_error_status: 401`` for an invalid/expired OAuth token), so classify
    from the envelope first. Never log or return the token or raw output.
    """
    try:
        envelope = json.loads(stdout.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        envelope = None
    if not isinstance(envelope, dict):
        raise ClaudeError('unavailable' if returncode else 'invalid', 'no JSON envelope')
    status = envelope.get('api_error_status')
    if envelope.get('is_error') or returncode:
        result = str(envelope.get('result') or '').lower()
        if status in (401, 403) or 'authenticat' in result or 'oauth' in result:
            raise ClaudeError('auth', f'status {status}')
        if status == 429 or 'usage limit' in result or 'rate limit' in result:
            raise ClaudeError('limit', f'status {status}')
        if isinstance(status, int) and status >= 500:
            raise ClaudeError('unavailable', f'status {status}')
        raise ClaudeError('invalid', f'subtype {envelope.get("subtype")}')
    obj = envelope.get('structured_output')
    if not isinstance(obj, dict) or set(obj) != {'reply', 'actions'}:
        raise ClaudeError('invalid', 'missing structured output')
    if not isinstance(obj['reply'], str) or not isinstance(obj['actions'], list):
        raise ClaudeError('invalid', 'output does not match the schema')
    for action in obj['actions']:
        if not isinstance(action, dict):
            raise ClaudeError('invalid', 'actions must be JSON objects')
    return obj


# ---------------------------------------------------------------- storage

SCHEMA_VERSION = 2


class Store:
    """SQLite state. Every multi-step state change is a single transaction."""

    def __init__(self, filename: str):
        Path(filename).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(filename, isolation_level=None)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if self.db.execute('PRAGMA user_version').fetchone()[0] < SCHEMA_VERSION:
            # Prototype (v1) approvals had no status/expiry binding: discard them.
            self.db.execute('DROP TABLE IF EXISTS approvals')
        self.db.executescript(f'''
          CREATE TABLE IF NOT EXISTS messages(
            conv TEXT NOT NULL, sender TEXT NOT NULL, timestamp INTEGER NOT NULL,
            text TEXT NOT NULL, expires_at INTEGER NOT NULL,
            PRIMARY KEY(conv,sender,timestamp)
          );
          CREATE INDEX IF NOT EXISTS messages_idx ON messages(conv,timestamp);
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS approvals(
            code TEXT PRIMARY KEY, sender TEXT NOT NULL, request_conv TEXT NOT NULL,
            service TEXT NOT NULL, data_json TEXT NOT NULL,
            created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
          );
          CREATE TABLE IF NOT EXISTS jobs(
            conv TEXT NOT NULL, sender TEXT NOT NULL, timestamp INTEGER NOT NULL,
            status TEXT NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(conv,sender,timestamp)
          );
          CREATE TABLE IF NOT EXISTS actions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, approval_code TEXT UNIQUE NOT NULL,
            sender TEXT NOT NULL, request_conv TEXT NOT NULL,
            service TEXT NOT NULL, data_json TEXT NOT NULL, status TEXT NOT NULL,
            created_at INTEGER NOT NULL, finished_at INTEGER, detail TEXT
          );
          CREATE TABLE IF NOT EXISTS outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT, dest TEXT NOT NULL, text TEXT NOT NULL,
            created_at INTEGER NOT NULL, next_attempt INTEGER NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT
          );
          CREATE TABLE IF NOT EXISTS notify_ids(id TEXT PRIMARY KEY, received_at INTEGER NOT NULL);
          PRAGMA user_version={SCHEMA_VERSION};
        ''')

    def _tx(self):
        return _Tx(self.db)

    # -- messages -------------------------------------------------------

    def add(self, msg: Incoming, settings: Settings = Settings()) -> bool:
        """Store a message for context. Returns False for duplicates."""
        with self._tx():
            return self._add(msg, settings)

    def _add(self, msg: Incoming, settings: Settings) -> bool:
        retention = msg.timestamp // 1000 + settings.history_days * 86400
        expires = retention
        if msg.expires_in_seconds > 0:
            expires = min(retention, msg.timestamp // 1000 + msg.expires_in_seconds)
        cur = self.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?)',
                              (msg.conv, msg.sender, msg.timestamp, msg.text, expires))
        return cur.rowcount == 1

    def add_with_job(self, msg: Incoming, now: int, settings: Settings = Settings()) -> bool:
        """Store a message and record that a command is being processed."""
        with self._tx():
            if not self._add(msg, settings):
                return False
            self.db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                            (msg.conv, msg.sender, msg.timestamp, 'pending', now))
            return True

    def finish_job(self, msg: Incoming, outgoing: list[tuple[str, str]], now: int) -> None:
        with self._tx():
            self.db.execute("UPDATE jobs SET status='done' WHERE conv=? AND sender=? AND timestamp=?",
                            (msg.conv, msg.sender, msg.timestamp))
            self._enqueue(outgoing, now)

    def remote_delete(self, deletion: RemoteDelete) -> bool:
        with self._tx():
            cur = self.db.execute('DELETE FROM messages WHERE conv=? AND sender=? AND timestamp=?',
                                  (deletion.conv, deletion.sender, deletion.target_timestamp))
            return cur.rowcount > 0

    def has_message(self, conv: str, sender: str, timestamp: int) -> bool:
        return self.db.execute('SELECT 1 FROM messages WHERE conv=? AND sender=? AND timestamp=?',
                               (conv, sender, timestamp)).fetchone() is not None

    def context(self, conv: str, now: int | None = None, labels: dict[str, str] | None = None,
                limit: int = 60, max_chars: int = 18000) -> str:
        now = int(time.time()) if now is None else now
        with self._tx():
            self.db.execute('DELETE FROM messages WHERE expires_at <= ?', (now,))
        rows = self.db.execute('SELECT sender,timestamp,text FROM messages '
                               'WHERE conv=? AND expires_at > ? ORDER BY timestamp DESC LIMIT ?',
                               (conv, now, limit)).fetchall()
        labels = labels or {}
        lines = [f'[{time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))}] '
                 f'{labels.get(sender, sender)}: {body}' for sender, ms, body in reversed(rows)]
        return '\n'.join(lines)[-max_chars:]

    # -- SSE cursor -------------------------------------------------------

    def get_cursor(self) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key='sse_id'").fetchone()
        return row[0] if row else None

    def set_cursor(self, cursor: str) -> None:
        with self._tx():
            self.db.execute("INSERT INTO metadata(key,value) VALUES('sse_id',?) "
                            'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (cursor,))

    # -- approvals and the action journal ---------------------------------

    def add_approval(self, code: str, sender: str, request_conv: str, service: str,
                     data: dict, now: int, prompt: str) -> None:
        """Record a pending approval and queue its private DM atomically."""
        with self._tx():
            self.db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?)',
                            (code, sender, request_conv, service, json.dumps(data),
                             now, now + APPROVAL_TTL, 'pending'))
            self._enqueue([(f'dm:{sender}', prompt)], now)

    def consume_approval(self, code: str, sender: str, now: int) -> tuple[int, str, str, dict] | None:
        """Single use: atomically mark consumed and journal the action as executing.

        Returns (action_id, request_conv, service, data) or None.
        """
        with self._tx():
            row = self.db.execute("SELECT request_conv,service,data_json FROM approvals "
                                  "WHERE code=? AND sender=? AND status='pending' AND expires_at>?",
                                  (code, sender, now)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE approvals SET status='consumed' WHERE code=?", (code,))
            cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,"
                                  "data_json,status,created_at) VALUES(?,?,?,?,?,'executing',?)",
                                  (code, sender, row[0], row[1], row[2], now))
            return cur.lastrowid, row[0], row[1], json.loads(row[2])

    def cancel_approval(self, code: str, sender: str) -> bool:
        with self._tx():
            cur = self.db.execute("UPDATE approvals SET status='cancelled' "
                                  "WHERE code=? AND sender=? AND status='pending'", (code, sender))
            return cur.rowcount == 1

    def finish_action(self, action_id: int, status: str, detail: str,
                      outgoing: list[tuple[str, str]], now: int) -> None:
        assert status in ('succeeded', 'failed', 'unknown')
        with self._tx():
            self.db.execute('UPDATE actions SET status=?, detail=?, finished_at=? WHERE id=?',
                            (status, detail[:200], now, action_id))
            self._enqueue(outgoing, now)

    def action_status(self, action_id: int) -> str | None:
        row = self.db.execute('SELECT status FROM actions WHERE id=?', (action_id,)).fetchone()
        return row[0] if row else None

    def recover(self, now: int) -> tuple[int, int]:
        """After a crash/restart: never retry. Report interrupted work instead.

        Actions left 'executing' have an unknown physical outcome. Jobs left
        'pending' were interrupted before a reply was queued.
        """
        with self._tx():
            actions = self.db.execute("SELECT id,sender,service,data_json FROM actions "
                                      "WHERE status='executing'").fetchall()
            for action_id, sender, service, data_json in actions:
                self.db.execute("UPDATE actions SET status='unknown', detail='interrupted by restart', "
                                'finished_at=? WHERE id=?', (now, action_id))
                self._enqueue([(f'dm:{sender}',
                                f'The bot restarted while running {describe(service, json.loads(data_json))}. '
                                'The outcome is UNKNOWN and it was NOT retried; please check the device.')],
                              now)
            jobs = self.db.execute("SELECT conv,sender,timestamp FROM jobs WHERE status='pending'").fetchall()
            for conv, _sender, ts in jobs:
                self.db.execute("UPDATE jobs SET status='interrupted' WHERE conv=? AND sender=? AND timestamp=?",
                                (conv, _sender, ts))
                when = time.strftime('%H:%M', time.localtime(ts / 1000))
                self._enqueue([(conv, f'I restarted while handling a request from {when}. '
                                      'It was not completed; please send it again if still needed.')], now)
        return len(actions), len(jobs)

    # -- outbox ---------------------------------------------------------

    def enqueue(self, outgoing: list[tuple[str, str]], now: int) -> None:
        with self._tx():
            self._enqueue(outgoing, now)

    def _enqueue(self, outgoing: list[tuple[str, str]], now: int) -> None:
        for dest, text in outgoing:
            if text.strip():
                self.db.execute('INSERT INTO outbox(dest,text,created_at,next_attempt) VALUES(?,?,?,?)',
                                (dest, text[:MAX_TEXT], now, now))

    def due_outbox(self, now: int, limit: int = 20) -> list[tuple[int, str, str, int, int]]:
        return self.db.execute("SELECT id,dest,text,attempts,created_at FROM outbox "
                               "WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT ?",
                               (now, limit)).fetchall()

    def next_outbox_time(self) -> int | None:
        row = self.db.execute("SELECT MIN(next_attempt) FROM outbox WHERE status='pending'").fetchone()
        return row[0]

    def outbox_sent(self, item_id: int) -> None:
        with self._tx():  # drop the text: the outbox is not a second transcript
            self.db.execute("UPDATE outbox SET status='sent', text='' WHERE id=?", (item_id,))

    def outbox_failed(self, item_id: int, attempts: int, created_at: int, error: str, now: int) -> str:
        attempts += 1
        if attempts >= OUTBOX_MAX_ATTEMPTS or now - created_at > OUTBOX_MAX_AGE:
            status, text_sql = 'dead', ", text=''"
        else:
            status, text_sql = 'pending', ''
        delay = min(300, 5 * 2 ** (attempts - 1))
        with self._tx():
            self.db.execute(f'UPDATE outbox SET status=?, attempts=?, next_attempt=?, last_error=?{text_sql} '
                            'WHERE id=?', (status, attempts, now + delay, error[:200], item_id))
        return status

    def outbox_dead(self, item_id: int, error: str) -> None:
        """Give up on a message without retrying (e.g. unsafe destination)."""
        with self._tx():
            self.db.execute("UPDATE outbox SET status='dead', text='', last_error=? WHERE id=?",
                            (error[:200], item_id))

    def outbox_status(self) -> dict[str, int]:
        return dict(self.db.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status').fetchall())

    # -- webhook idempotency -------------------------------------------

    def notify(self, notify_id: str | None, dest: str, text: str, now: int) -> bool:
        """Atomically dedupe and enqueue a proactive notification."""
        with self._tx():
            if notify_id is not None:
                cur = self.db.execute('INSERT OR IGNORE INTO notify_ids VALUES(?,?)', (notify_id, now))
                if cur.rowcount == 0:
                    return False
            self._enqueue([(dest, text)], now)
            return True

    # -- retention --------------------------------------------------------

    def purge(self, now: int) -> None:
        with self._tx():
            self.db.execute('DELETE FROM messages WHERE expires_at <= ?', (now,))
            self.db.execute("DELETE FROM approvals WHERE expires_at <= ?", (now - 86400,))
            self.db.execute("DELETE FROM outbox WHERE status!='pending' AND created_at <= ?", (now - 86400,))
            self.db.execute('DELETE FROM notify_ids WHERE received_at <= ?', (now - 7 * 86400,))
            self.db.execute("DELETE FROM jobs WHERE created_at <= ?", (now - 7 * 86400,))
            self.db.execute("DELETE FROM actions WHERE created_at <= ?", (now - 90 * 86400,))


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK for an autocommit connection."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self):
        self.db.execute('BEGIN IMMEDIATE')
        return self.db

    def __exit__(self, exc_type, *_):
        self.db.execute('ROLLBACK' if exc_type else 'COMMIT')
        return False
