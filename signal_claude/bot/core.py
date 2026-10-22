"""Pure logic and SQLite state. All network/process effects live in server.py.

Security model (see HANDOFF.md section 4):
- Sender/group identity comes only from signal-cli envelope metadata.
- Old, future-dated or disappeared messages never trigger Claude or writes.
- Claude may only *propose* actions. Every proposal is validated here against
  fixed limits and the denylist, then reviewed (server.py); a write needs a
  single-use confirmation from the requester's private DM unless the admin
  enabled auto mode and the reviewer allowed it.
"""
from __future__ import annotations
import fnmatch
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TEXT = 4000
APPROVAL_TTL = 120
SCHEDULE_MAX_AHEAD = 7 * 86400   # actions/reminders may be scheduled up to a week ahead
SCHEDULE_GRACE = 600             # a job more than 10 min overdue (bot was down) is skipped, not run late
REMIND = 'homebot.remind'        # internal "service" for reminders: a message, never an HA call
TASK = 'homebot.task'            # internal "service": run Claude with a stored instruction, then post its answer
REPEATS = ('once', 'daily', 'weekdays', 'weekly')
MAX_TASKS = 20                   # pending tasks per user
OUTBOX_MAX_AGE = 3600
OUTBOX_MAX_ATTEMPTS = 8
OBJECT_REPLACEMENT = '￼'  # Signal puts this where an @mention was

# Domains whose services a policy may allow. Everything else (locks, alarms,
# cameras, notify, HA/Supervisor administration, shell/REST commands, automation
# editing, ...) can never be called, whatever the policy says.
WRITABLE_DOMAINS = frozenset({
    'light', 'switch', 'fan', 'cover', 'climate', 'humidifier', 'water_heater',
    'media_player', 'scene', 'script', 'input_boolean', 'input_number',
    'input_select', 'input_button', 'button', 'vacuum', 'select', 'number',
})
ENTITY_RE = r'[a-z_]+\.[a-z0-9_]+'
FIELD_RE = r'[a-z_][a-z0-9_]{0,40}'
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
    quote_ts: int = 0          # timestamp of the message this one replies to
    quote_author: str = ''     # ACI of that message's author


@dataclass(frozen=True)
class Reaction:
    sender: str
    conv: str
    group: bool
    timestamp: int
    emoji: str
    target_ts: int
    target_author: str
    remove: bool
    server_timestamp: int = 0
    expires_in_seconds: int = 0


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
                       bot_name: str = 'HomeBot') -> Incoming | Reaction | RemoteDelete | None:
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

    ts = dm.get('timestamp')
    server_ts = envelope.get('serverReceivedTimestamp') or 0
    if not _valid_ts(ts) or not isinstance(server_ts, int) or server_ts < 0:
        return None
    expires = dm.get('expiresInSeconds') or 0
    if not isinstance(expires, int) or expires < 0:
        return None

    reaction = dm.get('reaction')
    if reaction is not None:  # reaction-schema: emoji, targetAuthorUuid, targetSentTimestamp, isRemove
        if (not isinstance(reaction, dict) or not isinstance(reaction.get('emoji'), str)
                or not _valid_ts(reaction.get('targetSentTimestamp'))):
            return None
        return Reaction(sender, conv, is_group, ts, reaction['emoji'], reaction['targetSentTimestamp'],
                        str(reaction.get('targetAuthorUuid') or '').lower(),
                        reaction.get('isRemove') is True, server_ts, expires)

    text = dm.get('message')
    if not isinstance(text, str) or not text.strip() or dm.get('viewOnce'):
        return None
    text, mentions_bot = _resolve_mentions(text, dm.get('mentions'), bot_aci, bot_name)
    quote = dm.get('quote') if isinstance(dm.get('quote'), dict) else {}
    quote_ts = quote.get('id') if _valid_ts(quote.get('id')) else 0
    return Incoming(sender, conv, is_group, ts, text.strip()[:MAX_TEXT], expires,
                    server_ts, mentions_bot, quote_ts, str(quote.get('authorUuid') or '').lower())


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


def freshness(msg: Incoming | Reaction, now_ms: int, settings: Settings) -> str:
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

TARGET_KEYS = frozenset({'entity_id', 'device_id', 'area_id', 'floor_id', 'label_id', 'target'})
DENY_RE = r'[a-z_*?]+\.[a-z0-9_*?]+'
MAX_DENY = 100
VERDICTS = ('allow', 'ask', 'deny')


def load_deny(patterns: object) -> list[str]:
    """Validate an admin pattern list (denylist or allowlist): globs like
    ``light.*``, ``cover.garage*`` or ``*.turn_off``, matched against entity and service."""
    if (not isinstance(patterns, list) or len(patterns) > MAX_DENY
            or not all(isinstance(p, str) and re.fullmatch(DENY_RE, p) for p in patterns)):
        raise ValueError('entries look like light.*, cover.garage* or *.turn_off')
    return list(patterns)


def denied_by(patterns: list[str], service: str, entity_id: str) -> str | None:
    """The first pattern matching the entity or the service, if any."""
    return next((p for p in patterns if fnmatch.fnmatchcase(entity_id, p) or fnmatch.fnmatchcase(service, p)),
                None)


def _scalar(value: object, max_len: int) -> bool:
    if isinstance(value, str):
        return len(value) <= max_len
    return isinstance(value, (bool, int, float)) and value == value  # no NaN


def validate_action(action: object, deny: list[str], allow: list[str] | None = None) -> tuple[str, dict]:
    """Fixed, deterministic limits on a proposed action (before any review).

    One existing-looking entity, a service of that entity's domain, a domain
    from WRITABLE_DOMAINS, nothing on the admin's denylist, on the allowlist
    if one is active (``allow`` not None), and flat service data (no extra
    targets). Returns (service, service_data incl. entity_id).
    """
    if not isinstance(action, dict) or set(action) - {'service', 'entity_id', 'data', 'at'}:
        raise Denied('unexpected action fields')
    service, entity_id, data = action.get('service'), action.get('entity_id'), action.get('data') or {}
    if not isinstance(service, str) or not isinstance(entity_id, str) or not isinstance(data, dict):
        raise Denied('malformed action')
    if not re.fullmatch(r'[a-z_]+\.[a-z_]+', service) or not re.fullmatch(ENTITY_RE, entity_id):
        raise Denied('malformed action')
    domain = entity_id.split('.')[0]
    if service.split('.')[0] != domain:
        raise Denied(f'{service} does not belong to {entity_id}')
    if domain not in WRITABLE_DOMAINS:
        raise Denied(f'{domain} devices can never be controlled by the bot')
    if pattern := denied_by(deny, service, entity_id):
        raise Denied(f'blocked by the denylist ({pattern})')
    if allow is not None and not denied_by(allow, service, entity_id):
        raise Denied(f'{entity_id} / {service} is not on the allowlist')
    if len(data) > 12:
        raise Denied('too many fields')
    for key, value in data.items():
        if not re.fullmatch(FIELD_RE, key) or key in TARGET_KEYS:
            raise Denied(f'field "{key}" is not allowed')
        if not (_scalar(value, 200) or (isinstance(value, list) and len(value) <= 10
                                         and all(_scalar(v, 100) for v in value))):
            raise Denied(f'field "{key}" must be a simple value')
    return service, {'entity_id': entity_id, **data}


def describe(service: str, data: dict) -> str:
    if service == REMIND:
        return f'reminder "{data.get("text", "")}"'
    if service == TASK:
        repeat = data.get('repeat', 'once')
        return f'task "{data.get("prompt", "")}"' + (f' ({repeat})' if repeat != 'once' else '')
    extra = ', '.join(f'{k}={v}' for k, v in data.items() if k != 'entity_id')
    return f'{service} on {data["entity_id"]}' + (f' ({extra})' if extra else '')


def parse_when(value: object, now: float) -> int | None:
    """Parse an optional local 'YYYY-MM-DDTHH:MM[:SS]' into epoch seconds.

    None/'' means "now". Must lie between 30 s and SCHEDULE_MAX_AHEAD ahead.
    """
    if value in (None, ''):
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\d[T ]\d\d:\d\d(:\d\d)?', value):
        raise Denied('time must look like 2026-10-23T02:45')
    fmt = '%Y-%m-%dT%H:%M:%S' if value.count(':') == 2 else '%Y-%m-%dT%H:%M'
    try:
        when = int(time.mktime(time.strptime(value.replace(' ', 'T'), fmt)))
    except (ValueError, OverflowError):
        raise Denied('invalid time') from None
    if when < now + 30:
        raise Denied('that time is not in the future')
    if when > now + SCHEDULE_MAX_AHEAD:
        raise Denied('can schedule at most 7 days ahead')
    return when


def next_run(run_at: int, repeat: str, now: float) -> int | None:
    """The next occurrence of a repeating task after ``now``, at the same local
    wall-clock time (so it follows DST changes), or None for 'once'."""
    if repeat not in REPEATS[1:]:
        return None
    t = time.localtime(run_at)
    for day in range(1, 400):
        when = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday + day, t.tm_hour, t.tm_min, t.tm_sec, 0, 0, -1)))
        weekday = time.localtime(when).tm_wday
        if when > now and (repeat == 'daily' or (repeat == 'weekdays' and weekday < 5)
                           or (repeat == 'weekly' and day % 7 == 0)):
            return when
    return None


def local_time(epoch: float) -> str:
    return time.strftime('%a %d.%m. %H:%M', time.localtime(epoch))


def build_schema(writes: bool) -> dict:
    """JSON schema for Claude's structured output.

    No ``$schema`` key: Claude Code 2.1.x rejects draft 2020-12 references.
    When changes are off, ``actions`` must be empty.
    """
    item = {
        'type': 'object',
        'properties': {
            'service': {'type': 'string'},
            'entity_id': {'type': 'string'},
            'data': {'type': 'object'},
            'at': {'type': ['string', 'null']},
        },
        'required': ['service', 'entity_id', 'data'],
        'additionalProperties': False,
    }
    actions = {'type': 'array', 'items': item, 'maxItems': 3 if writes else 0}
    reminder = {'type': 'object', 'properties': {'at': {'type': 'string'}, 'text': {'type': 'string'}},
                'required': ['at', 'text'], 'additionalProperties': False}
    task = {'type': 'object', 'properties': {'at': {'type': 'string'}, 'prompt': {'type': 'string'},
                                             'repeat': {'type': 'string', 'enum': list(REPEATS)}},
            'required': ['at', 'prompt', 'repeat'], 'additionalProperties': False}
    return {
        'type': 'object',
        'properties': {'reply': {'type': 'string'}, 'actions': actions,
                       'reminders': {'type': 'array', 'items': reminder, 'maxItems': 3},
                       'tasks': {'type': 'array', 'items': task, 'maxItems': 3},
                       'cancel': {'type': 'array', 'items': {'type': 'integer'}, 'maxItems': 5}},
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


CLASSIFIER_SCHEMA = {
    'type': 'object',
    'properties': {'decision': {'type': 'string', 'enum': list(VERDICTS)}, 'reason': {'type': 'string'}},
    'required': ['decision', 'reason'],
    'additionalProperties': False,
}


def parse_verdict(stdout: bytes, returncode: int = 0) -> tuple[str, str]:
    """(decision, reason) from the reviewer's envelope; anything odd means 'ask'."""
    obj = _structured_output(stdout, returncode)
    decision, reason = obj.get('decision'), obj.get('reason')
    if decision not in VERDICTS or not isinstance(reason, str):
        return 'ask', 'the reviewer gave no clear answer'
    return decision, ' '.join(reason.split())[:300]


def parse_claude_output(stdout: bytes, returncode: int = 0) -> dict:
    """Validate Claude's answer (reply, actions, ...) before anything reaches the gateway."""
    return _check_output(_structured_output(stdout, returncode))


def _structured_output(stdout: bytes, returncode: int = 0) -> dict:
    """Validate the Claude CLI JSON envelope and return its structured output.

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
    if not isinstance(obj, dict):
        raise ClaudeError('invalid', 'missing structured output')
    return obj


def _check_output(obj: dict) -> dict:
    if (not isinstance(obj, dict) or not {'reply', 'actions'} <= set(obj)
            or set(obj) - {'reply', 'actions', 'reminders', 'tasks', 'cancel'}):
        raise ClaudeError('invalid', 'missing structured output')
    obj = {'reminders': [], 'tasks': [], 'cancel': [], **obj}
    if (not isinstance(obj['reply'], str) or not isinstance(obj['actions'], list)
            or not all(isinstance(obj[k], list) for k in ('reminders', 'tasks', 'cancel'))):
        raise ClaudeError('invalid', 'output does not match the schema')
    if not all(isinstance(a, dict) for a in obj['actions'] + obj['reminders'] + obj['tasks']):
        raise ClaudeError('invalid', 'actions, reminders and tasks must be JSON objects')
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in obj['cancel']):
        raise ClaudeError('invalid', 'cancel must list numbers')
    return obj


# ---------------------------------------------------------------- storage

SCHEMA_VERSION = 3


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
          CREATE TABLE IF NOT EXISTS events_seen(
            conv TEXT NOT NULL, sender TEXT NOT NULL, timestamp INTEGER NOT NULL, seen_at INTEGER NOT NULL,
            PRIMARY KEY(conv,sender,timestamp)
          );
        ''')
        # v3: the confirmation prompt's Signal timestamp, to match replies/reactions.
        for table, column in (('approvals', 'prompt_ts INTEGER'), ('outbox', 'approval_code TEXT'),
                              ('approvals', 'run_at INTEGER'), ('actions', 'run_at INTEGER'),
                              ('outbox', 'edit_ts INTEGER')):
            have = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
            if column.split()[0] not in have:
                self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column}')
        self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

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

    def finish_job(self, msg: Incoming, outgoing: list[tuple[str, str]], now: int,
                   edit_ts: int | None = None) -> None:
        """Mark the request done and queue its replies; the first reply to the
        request's chat replaces the status message ``edit_ts`` if given."""
        with self._tx():
            self.db.execute("UPDATE jobs SET status='done' WHERE conv=? AND sender=? AND timestamp=?",
                            (msg.conv, msg.sender, msg.timestamp))
            if edit_ts and outgoing and outgoing[0][0] == msg.conv:
                self._enqueue(outgoing[:1], now, edit_ts=edit_ts)
                outgoing = outgoing[1:]
            self._enqueue(outgoing, now)

    def add_outgoing(self, conv: str, sender: str, timestamp: int, text: str, settings: Settings = Settings()) -> None:
        """Remember a reply the bot sent, so Claude sees both sides of the chat.
        It inherits the chat's disappearing timer (from the latest incoming message)."""
        row = self.db.execute('SELECT timestamp, expires_at FROM messages WHERE conv=? AND sender!=? '
                              'ORDER BY timestamp DESC LIMIT 1', (conv, sender)).fetchone()
        expires = 0
        if row and row[1] - row[0] // 1000 < settings.history_days * 86400:
            expires = max(1, row[1] - row[0] // 1000)
        self.add(Incoming(sender, conv, conv.startswith('group:'), timestamp, text[:MAX_TEXT], expires), settings)

    def clear_conversation(self, conv: str) -> int:
        """Forget a conversation's stored history (the /reset command)."""
        with self._tx():
            return self.db.execute('DELETE FROM messages WHERE conv=?', (conv,)).rowcount

    def remote_delete(self, deletion: RemoteDelete) -> bool:
        with self._tx():
            cur = self.db.execute('DELETE FROM messages WHERE conv=? AND sender=? AND timestamp=?',
                                  (deletion.conv, deletion.sender, deletion.target_timestamp))
            return cur.rowcount > 0

    def has_message(self, conv: str, sender: str, timestamp: int) -> bool:
        return self.db.execute('SELECT 1 FROM messages WHERE conv=? AND sender=? AND timestamp=?',
                               (conv, sender, timestamp)).fetchone() is not None

    def context(self, conv: str, now: int | None = None, labels: dict[str, str] | None = None,
                limit: int = 60, max_chars: int = 18000, exclude: tuple[str, int] | None = None) -> str:
        now = int(time.time()) if now is None else now
        with self._tx():
            self.db.execute('DELETE FROM messages WHERE expires_at <= ?', (now,))
        rows = self.db.execute("SELECT sender,timestamp,text FROM messages "
                               "WHERE conv=? AND expires_at > ? AND text != '' ORDER BY timestamp DESC LIMIT ?",
                               (conv, now, limit)).fetchall()
        labels = labels or {}
        lines = [f'[{time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))}] '
                 f'{labels.get(sender, sender)}: {body}' for sender, ms, body in reversed(rows)
                 if (sender, ms) != exclude]
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
                     data: dict, now: int, prompt: str, run_at: int | None = None) -> None:
        """Record a pending approval and queue its private DM atomically."""
        with self._tx():
            self.db.execute('INSERT INTO approvals(code,sender,request_conv,service,data_json,created_at,'
                            'expires_at,status,run_at) VALUES(?,?,?,?,?,?,?,?,?)',
                            (code, sender, request_conv, service, json.dumps(data),
                             now, now + APPROVAL_TTL, 'pending', run_at))
            self._enqueue([(f'dm:{sender}', prompt)], now, approval_code=code)

    PROMPT_AMBIGUOUS = 'ambiguous'

    def find_approval(self, sender: str, now: int, prompt_ts: int | None = None) -> str | None:
        """The pending approval a reply/reaction refers to.

        With ``prompt_ts`` (a quoted reply or reaction) it must be that exact
        prompt. Without, there must be exactly one pending approval for the
        sender; several give PROMPT_AMBIGUOUS.
        """
        rows = self.db.execute("SELECT code, prompt_ts FROM approvals WHERE sender=? AND status='pending' "
                               'AND expires_at>?', (sender, now)).fetchall()
        if prompt_ts is not None:
            return next((code for code, ts in rows if ts == prompt_ts), None)
        if len(rows) > 1:
            return self.PROMPT_AMBIGUOUS
        return rows[0][0] if rows else None

    def seen(self, conv: str, sender: str, timestamp: int, now: int) -> bool:
        """Record a non-text event (e.g. a reaction); False if already seen."""
        with self._tx():
            cur = self.db.execute('INSERT OR IGNORE INTO events_seen VALUES(?,?,?,?)',
                                  (conv, sender, timestamp, now))
            return cur.rowcount == 1

    def consume_approval(self, code: str, sender: str, now: int) -> tuple[int, str, str, dict, int | None] | None:
        """Single use: atomically mark consumed and journal the action as
        executing (or scheduled, if it has a run time).

        Returns (action_id, request_conv, service, data, run_at) or None.
        """
        with self._tx():
            row = self.db.execute("SELECT request_conv,service,data_json,run_at FROM approvals "
                                  "WHERE code=? AND sender=? AND status='pending' AND expires_at>?",
                                  (code, sender, now)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE approvals SET status='consumed' WHERE code=?", (code,))
            cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,"
                                  "data_json,status,created_at,run_at) VALUES(?,?,?,?,?,?,?,?)",
                                  (code, sender, row[0], row[1], row[2],
                                   'scheduled' if row[3] else 'executing', now, row[3]))
            return cur.lastrowid, row[0], row[1], json.loads(row[2]), row[3]

    def add_action(self, sender: str, request_conv: str, service: str, data: dict, now: int) -> int:
        """Journal an auto-approved action as executing (no confirmation step)."""
        with self._tx():
            return self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                                   "status,created_at) VALUES(?,?,?,?,?,'executing',?)",
                                   (f'auto-{now}-{os.urandom(6).hex()}', sender, request_conv, service,
                                    json.dumps(data), now)).lastrowid

    def add_scheduled(self, sender: str, request_conv: str, service: str, data: dict,
                      run_at: int, now: int) -> int:
        """A job that needs no (further) confirmation: reminders, tasks, auto-approved actions."""
        with self._tx():
            cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                                  "status,created_at,run_at) VALUES(?,?,?,?,?,'scheduled',?,?)",
                                  (f'job-{now}-{os.urandom(6).hex()}', sender, request_conv, service,
                                   json.dumps(data), now, run_at))
            return cur.lastrowid

    def scheduled(self, sender: str | None = None) -> list[tuple[int, str, str, str, dict, int]]:
        """Pending jobs (all, or one sender's): (id, sender, request_conv, service, data, run_at)."""
        query = "SELECT id,sender,request_conv,service,data_json,run_at FROM actions WHERE status='scheduled'"
        rows = self.db.execute(query + (' AND sender=?' if sender else '') + ' ORDER BY run_at',
                               (sender,) if sender else ()).fetchall()
        return [(i, s, c, svc, json.loads(d), r) for i, s, c, svc, d, r in rows]

    def due_scheduled(self, now: int) -> list[tuple[int, str, str, str, dict, int]]:
        return [job for job in self.scheduled() if job[5] <= now]

    def start_scheduled(self, action_id: int) -> bool:
        """Claim a due job exactly once (scheduled -> executing)."""
        with self._tx():
            return self.db.execute("UPDATE actions SET status='executing' WHERE id=? AND status='scheduled'",
                                   (action_id,)).rowcount == 1

    def end_scheduled(self, action_id: int, status: str, detail: str,
                      outgoing: list[tuple[str, str]], now: int) -> bool:
        """Cancel or mark missed (only while still scheduled)."""
        assert status in ('cancelled', 'missed')
        with self._tx():
            ok = self.db.execute("UPDATE actions SET status=?, detail=?, finished_at=? "
                                 "WHERE id=? AND status='scheduled'", (status, detail, now, action_id)).rowcount == 1
            if ok:
                self._enqueue(outgoing, now)
            return ok

    def repeat_task(self, action_id: int, now: int) -> int | None:
        """Schedule the next occurrence of a repeating task; returns its id."""
        with self._tx():
            return self._repeat_task(action_id, now)

    def _repeat_task(self, action_id: int, now: int) -> int | None:
        row = self.db.execute('SELECT sender,request_conv,service,data_json,run_at FROM actions WHERE id=?',
                              (action_id,)).fetchone()
        if not row or row[2] != TASK:
            return None
        sender, conv, service, data_json, run_at = row
        when = next_run(run_at, json.loads(data_json).get('repeat', 'once'), now)
        if when is None:
            return None
        cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                              "status,created_at,run_at) VALUES(?,?,?,?,?,'scheduled',?,?)",
                              (f'job-{now}-{os.urandom(6).hex()}', sender, conv, service, data_json, now, when))
        return cur.lastrowid

    def cancel_scheduled(self, action_id: int, sender: str, now: int) -> bool:
        with self._tx():
            return self.db.execute("UPDATE actions SET status='cancelled', detail='cancelled by user', "
                                   "finished_at=? WHERE id=? AND sender=? AND status='scheduled'",
                                   (now, action_id, sender)).rowcount == 1

    def cancel_approval(self, code: str, sender: str) -> bool:
        with self._tx():
            cur = self.db.execute("UPDATE approvals SET status='cancelled' "
                                  "WHERE code=? AND sender=? AND status='pending'", (code, sender))
            return cur.rowcount == 1

    def finish_action(self, action_id: int, status: str, detail: str,
                      outgoing: list[tuple[str, str]], now: int) -> None:
        assert status in ('succeeded', 'failed', 'unknown', 'scheduled')
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
                if service in (TASK, REMIND):  # no device involved: report, and keep a series going
                    self.db.execute("UPDATE actions SET status='failed', detail='interrupted by restart', "
                                    'finished_at=? WHERE id=?', (now, action_id))
                    self._enqueue([(f'dm:{sender}', f'#{action_id} ({describe(service, json.loads(data_json))}) '
                                                    'was interrupted by a restart.')], now)
                    self._repeat_task(action_id, now)
                    continue
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

    def _enqueue(self, outgoing: list[tuple[str, str]], now: int, approval_code: str | None = None,
                 edit_ts: int | None = None) -> None:
        for dest, text in outgoing:
            if text.strip():
                self.db.execute('INSERT INTO outbox(dest,text,created_at,next_attempt,approval_code,edit_ts) '
                                'VALUES(?,?,?,?,?,?)', (dest, text[:MAX_TEXT], now, now, approval_code, edit_ts))

    def due_outbox(self, now: int, limit: int = 20) -> list[tuple[int, str, str, int, int, int | None]]:
        """(id, dest, text, attempts, created_at, edit_ts) of messages due to be sent."""
        return self.db.execute("SELECT id,dest,text,attempts,created_at,edit_ts FROM outbox "
                               "WHERE status='pending' AND next_attempt<=? ORDER BY id LIMIT ?",
                               (now, limit)).fetchall()

    def outbox_unedit(self, item_id: int) -> None:
        """Editing the status message failed: send the text as a new message."""
        with self._tx():
            self.db.execute('UPDATE outbox SET edit_ts=NULL WHERE id=?', (item_id,))

    def next_outbox_time(self) -> int | None:
        row = self.db.execute("SELECT MIN(next_attempt) FROM outbox WHERE status='pending'").fetchone()
        return row[0]

    def outbox_sent(self, item_id: int, sent_ts: int | None = None) -> None:
        with self._tx():  # drop the text: the outbox is not a second transcript
            self.db.execute("UPDATE outbox SET status='sent', text='' WHERE id=?", (item_id,))
            if isinstance(sent_ts, int):
                self.db.execute('UPDATE approvals SET prompt_ts=? WHERE code=(SELECT approval_code '
                                'FROM outbox WHERE id=?)', (sent_ts, item_id))

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
            self.db.execute('DELETE FROM events_seen WHERE seen_at <= ?', (now - 7 * 86400,))
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
