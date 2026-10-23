"""Pure logic and SQLite state. All network/process effects live in server.py.

Security model (see HANDOFF.md section 4):
- Sender/group identity comes only from signal-cli envelope metadata.
- Old, future-dated or disappeared messages never trigger Claude or writes.
- Claude may only *propose* actions. Every proposal is validated here against
  fixed limits and the permission list, then reviewed (server.py); a write needs a
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

from .cards import PERIODS

MAX_TEXT = 4000
APPROVAL_TTL = 120
SCHEDULE_MAX_AHEAD = 7 * 86400   # actions/reminders may be scheduled up to a week ahead
SCHEDULE_GRACE = 600             # a job more than 10 min overdue (bot was down) is skipped, not run late
REMIND = 'homebot.remind'        # internal "service" for reminders: a message, never an HA call
TASK = 'homebot.task'            # internal "service": run Claude with a stored instruction, then post its answer
REPEATS = ('once', 'daily', 'weekdays', 'weekly')
MAX_TASKS = 20                   # pending tasks per user
MAX_GRANTS = 5                   # actions a task may run without asking (confirmed when it is set up)
TRIGGER_REPEATS = ('once', 'always')  # a task started by a state change: the first time, or every time
TRIGGER_TTL = 7 * 86400          # a one-off trigger waits at most a week
TRIGGER_COOLDOWN = 600           # an "always" trigger fires at most every 10 minutes
TRIGGER_KEYS = frozenset({'entity_id', 'attribute', 'to', 'from', 'above', 'below', 'for_minutes'})
OUTBOX_MAX_AGE = 3600
OUTBOX_MAX_ATTEMPTS = 8
OBJECT_REPLACEMENT = '￼'  # Signal puts this where an @mention was
IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp')  # what Claude can look at
MAX_IMAGES = 4                   # photos per message passed to Claude
MAX_IMAGE_BYTES = 5 << 20        # Claude's limit per image
ATTACHMENT_ID_RE = r'[A-Za-z0-9._-]{1,128}'

# Domains whose services a policy may allow. Everything else (locks, alarms,
# cameras, notify, HA/Supervisor administration, shell/REST commands, automation
# editing, ...) can never be called, whatever the policy says.
WRITABLE_DOMAINS = frozenset({
    'light', 'switch', 'fan', 'cover', 'climate', 'humidifier', 'water_heater',
    'media_player', 'scene', 'script', 'input_boolean', 'input_number',
    'input_select', 'input_button', 'button', 'vacuum', 'select', 'number', 'todo',
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
    images: tuple[str, ...] = ()  # signal-cli attachment ids of photos sent with it
    quote_text: str = ''       # the quoted message's text as the sender's app shows it


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
    images = _images(dm.get('attachments'))
    if dm.get('viewOnce') or not ((isinstance(text, str) and text.strip()) or images):
        return None
    text, mentions_bot = _resolve_mentions(text if isinstance(text, str) else '', dm.get('mentions'),
                                           bot_aci, bot_name)
    if images:  # the history keeps only this marker, never the picture
        text = (text.strip() + ' ' if text.strip() else '') + \
            ('[photo]' if len(images) == 1 else f'[{len(images)} photos]')
    quote = dm.get('quote') if isinstance(dm.get('quote'), dict) else {}
    quote_ts = quote.get('id') if _valid_ts(quote.get('id')) else 0
    quote_text = quote.get('text') if isinstance(quote.get('text'), str) else ''
    return Incoming(sender, conv, is_group, ts, text.strip()[:MAX_TEXT], expires,
                    server_ts, mentions_bot, quote_ts, str(quote.get('authorUuid') or '').lower(), images,
                    quote_text[:2000] if quote_ts else '')


def _images(attachments: object) -> tuple[str, ...]:
    """Ids of attached photos Claude can look at (attachment.schema.json)."""
    if not isinstance(attachments, list):
        return ()
    return tuple(a['id'] for a in attachments
                 if isinstance(a, dict) and a.get('contentType') in IMAGE_TYPES
                 and isinstance(a.get('id'), str) and re.fullmatch(ATTACHMENT_ID_RE, a['id'])
                 and not (isinstance(a.get('size'), int) and a['size'] > MAX_IMAGE_BYTES))[:MAX_IMAGES]


def image_type(data: bytes) -> str | None:
    """The image's real type from its first bytes (not the sender's claim)."""
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


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
MAX_RULES = 200
VERDICTS = ('allow', 'ask', 'deny')


PERMISSION_RE = r'(allow|review|deny)\s+(\*|[a-z_*?]+\.[a-z0-9_*?]+)'
EFFECTS = ('allow', 'review', 'deny')


def load_permissions(lines: object) -> list[tuple[str, str]]:
    """Parse the admin's ordered permission list into (effect, pattern) rules.

    One rule per line: ``allow|review|deny <pattern>``, where the pattern is a
    glob like ``light.*``, ``cover.garage*``, ``*.turn_off`` or ``*``, matched
    against the entity and the service. Blank lines and ``#`` comments are
    ignored. Like .gitignore, the last matching line wins.
    """
    if not isinstance(lines, list) or len(lines) > MAX_RULES or not all(isinstance(x, str) for x in lines):
        raise ValueError(f'at most {MAX_RULES} lines of text')
    rules = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(PERMISSION_RE, line)
        if not match:
            raise ValueError(f'line {n} ("{line[:40]}"): use allow, review or deny, then a pattern '
                             'like light.*, cover.garage*, *.turn_off or *')
        rules.append((match.group(1), match.group(2)))
    return rules


def permission(rules: list[tuple[str, str]], service: str, entity_id: str) -> tuple[str, str | None]:
    """(effect, matching line) for a call; the last matching rule wins, default 'review'."""
    for effect, pattern in reversed(rules):
        if fnmatch.fnmatchcase(entity_id, pattern) or fnmatch.fnmatchcase(service, pattern):
            return effect, f'{effect} {pattern}'
    return 'review', None


def _scalar(value: object, max_len: int) -> bool:
    if isinstance(value, str):
        return len(value) <= max_len
    return isinstance(value, (bool, int, float)) and value == value  # no NaN


def validate_action(action: object, rules: list[tuple[str, str]]) -> tuple[str, dict]:
    """Fixed, deterministic limits on a proposed action (before any review).

    One existing-looking entity, a service of that entity's domain, a domain
    from WRITABLE_DOMAINS, not denied by the admin's permission rules, and
    flat service data (no extra targets). Returns (service, service_data incl.
    entity_id); ``permission()`` tells whether it still needs review.
    """
    if not isinstance(action, dict) or set(action) - {'service', 'entity_id', 'data', 'at', 'repeat'}:
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
    effect, line = permission(rules, service, entity_id)
    if effect == 'deny':
        raise Denied(f'not permitted ({line})')
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
        grants = '; '.join(f'{g["service"]} on {g["entity_id"]}' for g in data.get('grants') or [])
        when = (f' when {describe_trigger(data["trigger"])}' + (', every time' if repeat == 'always' else '')
                if data.get('trigger') else (f' ({repeat})' if repeat != 'once' else ''))
        return (f'task "{data.get("prompt", "")}"' + when
                + (f', may do without asking: {grants}' if grants else ''))
    extra = ', '.join(f'{k}={v}' for k, v in data.items() if k != 'entity_id')
    return f'{service} on {data["entity_id"]}' + (f' ({extra})' if extra else '')


def parse_trigger(value: object) -> dict:
    """A task's state trigger, like HA's state/numeric_state triggers: the
    entity (or one attribute) changes to/from a state, rises above or falls
    below a number, optionally for some minutes."""
    if not isinstance(value, dict) or set(value) - TRIGGER_KEYS:
        raise Denied('a trigger has entity_id, attribute, to, from, above, below and for_minutes')
    trigger = {k: v for k, v in value.items() if v is not None}
    if not isinstance(trigger.get('entity_id'), str) or not re.fullmatch(ENTITY_RE, trigger['entity_id']):
        raise Denied('a trigger needs an entity_id')
    if 'attribute' in trigger and not (isinstance(trigger['attribute'], str)
                                       and re.fullmatch(FIELD_RE, trigger['attribute'])):
        raise Denied('invalid trigger attribute')
    for key in ('to', 'from'):
        if key in trigger:
            if isinstance(trigger[key], bool) or not isinstance(trigger[key], (str, int, float)):
                raise Denied(f'trigger "{key}" must be a state')
            trigger[key] = str(trigger[key])[:64]
    for key in ('above', 'below'):
        if key in trigger and (isinstance(trigger[key], bool) or not isinstance(trigger[key], (int, float))):
            raise Denied(f'trigger "{key}" must be a number')
    minutes = trigger.get('for_minutes', 0)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
        raise Denied('for_minutes must be 0-1440')
    if not trigger.get('for_minutes'):
        trigger.pop('for_minutes', None)
    if not {'to', 'from', 'above', 'below'} & set(trigger):
        raise Denied('a trigger needs to, from, above or below')
    return trigger


def describe_trigger(trigger: dict) -> str:
    parts = []
    if 'from' in trigger and 'to' in trigger:
        parts.append(f'changes from "{trigger["from"]}" to "{trigger["to"]}"')
    elif 'from' in trigger:
        parts.append(f'leaves "{trigger["from"]}"')
    elif 'to' in trigger:
        parts.append(f'becomes "{trigger["to"]}"')
    if 'above' in trigger:
        parts.append(f'is above {trigger["above"]}')
    if 'below' in trigger:
        parts.append(f'is below {trigger["below"]}')
    what = trigger['entity_id'] + (f' {trigger["attribute"]}' if 'attribute' in trigger else '')
    return f'{what} {" and ".join(parts)}' + (f' for {trigger["for_minutes"]} min' if 'for_minutes' in trigger else '')


def trigger_value(trigger: dict, state: object) -> object:
    """The watched value in an HA state object, or None (missing, unavailable)."""
    if not isinstance(state, dict):
        return None
    if 'attribute' in trigger:
        attributes = state.get('attributes')
        return attributes.get(trigger['attribute']) if isinstance(attributes, dict) else None
    value = state.get('state')
    return None if value in (None, 'unavailable', 'unknown') else value


def trigger_holds(trigger: dict, state: object) -> bool:
    """Whether the state satisfies the trigger's to/above/below (not "from")."""
    value = trigger_value(trigger, state)
    if value is None:
        return False
    if 'to' in trigger and str(value).casefold() != trigger['to'].casefold():
        return False
    if 'above' in trigger or 'below' in trigger:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if 'above' in trigger and not number > trigger['above']:
            return False
        if 'below' in trigger and not number < trigger['below']:
            return False
    return True


def trigger_fires(trigger: dict, old: object, new: object) -> bool:
    """A state change that starts the trigger: the new state holds and either
    the old one didn't or (with "from") the value left that state. Changes
    from or to unavailable/unknown never fire."""
    before, after = trigger_value(trigger, old), trigger_value(trigger, new)
    if before is None or after is None or not trigger_holds(trigger, new):
        return False
    if 'from' in trigger:
        return str(before).casefold() == trigger['from'].casefold() and str(before) != str(after)
    return not trigger_holds(trigger, old)


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
            'repeat': {'type': 'string', 'enum': list(REPEATS)},
        },
        'required': ['service', 'entity_id', 'data'],
        'additionalProperties': False,
    }
    actions = {'type': 'array', 'items': item, 'maxItems': 3 if writes else 0}
    reminder = {'type': 'object', 'properties': {'at': {'type': 'string'}, 'text': {'type': 'string'}},
                'required': ['at', 'text'], 'additionalProperties': False}
    grant = {'type': 'object', 'properties': {'service': {'type': 'string'}, 'entity_id': {'type': 'string'}},
             'required': ['service', 'entity_id'], 'additionalProperties': False}
    trigger = {'type': ['object', 'null'],
               'properties': {'entity_id': {'type': 'string'}, 'attribute': {'type': 'string'},
                              'to': {'type': 'string'}, 'from': {'type': 'string'},
                              'above': {'type': 'number'}, 'below': {'type': 'number'},
                              'for_minutes': {'type': 'integer'}},
               'required': ['entity_id'], 'additionalProperties': False}
    task = {'type': 'object', 'properties': {'at': {'type': ['string', 'null']}, 'when': trigger,
                                             'prompt': {'type': 'string'},
                                             'repeat': {'type': 'string',
                                                        'enum': list(REPEATS) + list(TRIGGER_REPEATS[1:])},
                                             'grants': {'type': 'array', 'items': grant,
                                                        'maxItems': MAX_GRANTS if writes else 0}},
            'required': ['at', 'when', 'prompt', 'repeat', 'grants'], 'additionalProperties': False}
    return {
        'type': 'object',
        'properties': {'reply': {'type': 'string'}, 'actions': actions,
                       'images': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3},
                       'cards': {'type': 'array', 'maxItems': 2, 'items': {
                           'type': 'object', 'additionalProperties': False, 'required': ['card'],
                           'properties': {'card': {'type': 'object'},
                                          'period': {'type': ['string', 'null'], 'enum': [*PERIODS, None]}}}},
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
            or set(obj) - {'reply', 'actions', 'reminders', 'tasks', 'cancel', 'images', 'cards'}):
        raise ClaudeError('invalid', 'missing structured output')
    obj = {'reminders': [], 'tasks': [], 'cancel': [], 'images': [], 'cards': [], **obj}
    if not isinstance(obj['cards'], list):
        raise ClaudeError('invalid', 'cards must be a list')
    if not isinstance(obj['images'], list) or not all(isinstance(i, str) for i in obj['images']):
        raise ClaudeError('invalid', 'images must list entity ids')
    if (not isinstance(obj['reply'], str) or not isinstance(obj['actions'], list)
            or not all(isinstance(obj[k], list) for k in ('reminders', 'tasks', 'cancel'))):
        raise ClaudeError('invalid', 'output does not match the schema')
    if not all(isinstance(a, dict) for a in obj['actions'] + obj['reminders'] + obj['tasks']):
        raise ClaudeError('invalid', 'actions, reminders and tasks must be JSON objects')
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in obj['cancel']):
        raise ClaudeError('invalid', 'cancel must list numbers')
    return obj


# ---------------------------------------------------------------- storage

SCHEMA_VERSION = 7


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
                              ('outbox', 'edit_ts INTEGER'),
                              # v5: pictures sent with a message (JSON list of data URIs).
                              ('outbox', 'attachments TEXT'),
                              # v6: the status message a message with pictures replaces (deleted after).
                              ('outbox', 'delete_ts INTEGER'),
                              # v7: what the pictures showed, for the history (e.g. which card).
                              ('outbox', 'history_note TEXT'),
                              # v4: repeating scheduled actions ('once', 'daily', ...).
                              ('approvals', 'repeat TEXT'), ('actions', 'repeat TEXT')):
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
                     data: dict, now: int, prompt: str, run_at: int | None = None, repeat: str = 'once') -> None:
        """Record a pending approval and queue its private DM atomically."""
        with self._tx():
            self.db.execute('INSERT INTO approvals(code,sender,request_conv,service,data_json,created_at,'
                            'expires_at,status,run_at,repeat) VALUES(?,?,?,?,?,?,?,?,?,?)',
                            (code, sender, request_conv, service, json.dumps(data),
                             now, now + APPROVAL_TTL, 'pending', run_at, repeat))
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
            row = self.db.execute("SELECT request_conv,service,data_json,run_at,repeat FROM approvals "
                                  "WHERE code=? AND sender=? AND status='pending' AND expires_at>?",
                                  (code, sender, now)).fetchone()
            if not row:
                return None
            self.db.execute("UPDATE approvals SET status='consumed' WHERE code=?", (code,))
            later = row[3] or row[1] == TASK  # a time, or a task waiting for its trigger
            cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,"
                                  "data_json,status,created_at,run_at,repeat) VALUES(?,?,?,?,?,?,?,?,?)",
                                  (code, sender, row[0], row[1], row[2],
                                   'scheduled' if later else 'executing', now, row[3], row[4] or 'once'))
            return cur.lastrowid, row[0], row[1], json.loads(row[2]), row[3]

    def add_action(self, sender: str, request_conv: str, service: str, data: dict, now: int) -> int:
        """Journal an auto-approved action as executing (no confirmation step)."""
        with self._tx():
            return self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                                   "status,created_at) VALUES(?,?,?,?,?,'executing',?)",
                                   (f'auto-{now}-{os.urandom(6).hex()}', sender, request_conv, service,
                                    json.dumps(data), now)).lastrowid

    def add_scheduled(self, sender: str, request_conv: str, service: str, data: dict,
                      run_at: int | None, now: int, repeat: str = 'once') -> int:
        """A job that needs no (further) confirmation: reminders, tasks, auto-approved
        actions. A task without run_at waits for its state trigger."""
        with self._tx():
            cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                                  "status,created_at,run_at,repeat) VALUES(?,?,?,?,?,'scheduled',?,?,?)",
                                  (f'job-{now}-{os.urandom(6).hex()}', sender, request_conv, service,
                                   json.dumps(data), now, run_at, repeat))
            return cur.lastrowid

    def scheduled(self, sender: str | None = None) -> list[tuple[int, str, str, str, dict, int | None]]:
        """Pending jobs (all, or one sender's): (id, sender, request_conv, service, data, run_at);
        run_at is None for a task waiting for its trigger."""
        query = "SELECT id,sender,request_conv,service,data_json,run_at FROM actions WHERE status='scheduled'"
        rows = self.db.execute(query + (' AND sender=?' if sender else '') + ' ORDER BY run_at IS NULL, run_at, id',
                               (sender,) if sender else ()).fetchall()
        return [(i, s, c, svc, json.loads(d), r) for i, s, c, svc, d, r in rows]

    def repeat_of(self, action_id: int) -> str:
        """How a job repeats: a task's own setting, else the action's column."""
        row = self.db.execute('SELECT service,data_json,repeat FROM actions WHERE id=?', (action_id,)).fetchone()
        if not row:
            return 'once'
        if row[0] == TASK:
            return json.loads(row[1]).get('repeat', 'once')
        return row[2] or 'once'

    def due_scheduled(self, now: int) -> list[tuple[int, str, str, str, dict, int]]:
        return [job for job in self.scheduled() if job[5] is not None and job[5] <= now]

    def watching(self) -> list[tuple[int, str, str, str, dict, None]]:
        """Tasks waiting for a state trigger."""
        return [job for job in self.scheduled() if job[5] is None and job[3] == TASK and job[4].get('trigger')]

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
        """Schedule the next run of a repeating task or action (a confirmed
        action keeps its confirmation for the whole series)."""
        row = self.db.execute('SELECT sender,request_conv,service,data_json,run_at,repeat FROM actions '
                              'WHERE id=?', (action_id,)).fetchone()
        if not row:
            return None
        sender, conv, service, data_json, run_at, repeat = row
        data = json.loads(data_json)
        if run_at is None:  # a triggered task: wait for the next time, after a pause
            if service != TASK or not data.get('trigger') or data.get('repeat') != 'always':
                return None
            data['not_before'] = now + TRIGGER_COOLDOWN
            return self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                                   "status,created_at,repeat) VALUES(?,?,?,?,?,'scheduled',?,?)",
                                   (f'job-{now}-{os.urandom(6).hex()}', sender, conv, service,
                                    json.dumps(data), now, repeat)).lastrowid
        when = next_run(run_at, self.repeat_of(action_id), now)
        if when is None:
            return None
        cur = self.db.execute("INSERT INTO actions(approval_code,sender,request_conv,service,data_json,"
                              "status,created_at,run_at,repeat) VALUES(?,?,?,?,?,'scheduled',?,?,?)",
                              (f'job-{now}-{os.urandom(6).hex()}', sender, conv, service, data_json, now, when,
                               repeat))
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
                self._repeat_task(action_id, now)  # a repeating series continues
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

    def _enqueue(self, outgoing: list[tuple], now: int, approval_code: str | None = None,
                 edit_ts: int | None = None) -> None:
        """Queue (dest, text) or (dest, text, [data URIs of pictures], note) messages; the
        note says what the pictures show, for the history. An edit can't add
        pictures: such a message is sent anew and replaces (deletes) the status
        message ``edit_ts`` instead."""
        for dest, text, *rest in outgoing:
            pictures = rest[0] if rest else []
            note = rest[1] if len(rest) > 1 else None
            if text.strip() or pictures:
                self.db.execute('INSERT INTO outbox(dest,text,created_at,next_attempt,approval_code,edit_ts,'
                                'attachments,delete_ts,history_note) VALUES(?,?,?,?,?,?,?,?,?)',
                                (dest, text[:MAX_TEXT], now, now, approval_code, None if pictures else edit_ts,
                                 json.dumps(pictures) if pictures else None, edit_ts if pictures else None, note))

    def due_outbox(self, now: int, limit: int = 20) -> list[tuple]:
        """(id, dest, text, attempts, created_at, edit_ts, pictures, delete_ts, note) of messages due."""
        return [(*row[:6], json.loads(row[6]) if row[6] else [], row[7], row[8])
                for row in self.db.execute("SELECT id,dest,text,attempts,created_at,edit_ts,attachments,delete_ts,"
                                           "history_note FROM outbox WHERE status='pending' AND next_attempt<=? "
                                           "ORDER BY id LIMIT ?", (now, limit)).fetchall()]

    def message_at(self, conv: str, timestamp: int) -> tuple[str, str] | None:
        """(sender, text) of a stored message, e.g. the one a reply quotes."""
        row = self.db.execute("SELECT sender, text FROM messages WHERE conv=? AND timestamp=? AND text != ''",
                              (conv, timestamp)).fetchone()
        return (row[0], row[1]) if row else None

    def outbox_unedit(self, item_id: int) -> None:
        """Editing the status message failed: send the text as a new message."""
        with self._tx():
            self.db.execute('UPDATE outbox SET edit_ts=NULL WHERE id=?', (item_id,))

    def next_outbox_time(self) -> int | None:
        row = self.db.execute("SELECT MIN(next_attempt) FROM outbox WHERE status='pending'").fetchone()
        return row[0]

    def outbox_sent(self, item_id: int, sent_ts: int | None = None) -> None:
        with self._tx():  # drop the text: the outbox is not a second transcript
            self.db.execute("UPDATE outbox SET status='sent', text='', attachments=NULL WHERE id=?", (item_id,))
            if isinstance(sent_ts, int):
                self.db.execute('UPDATE approvals SET prompt_ts=? WHERE code=(SELECT approval_code '
                                'FROM outbox WHERE id=?)', (sent_ts, item_id))

    def outbox_failed(self, item_id: int, attempts: int, created_at: int, error: str, now: int) -> str:
        attempts += 1
        if attempts >= OUTBOX_MAX_ATTEMPTS or now - created_at > OUTBOX_MAX_AGE:
            status, text_sql = 'dead', ", text='', attachments=NULL"
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
            self.db.execute("UPDATE outbox SET status='dead', text='', attachments=NULL, last_error=? WHERE id=?",
                            (error[:200], item_id))

    def outbox_status(self) -> dict[str, int]:
        return dict(self.db.execute('SELECT status, COUNT(*) FROM outbox GROUP BY status').fetchall())

    # -- webhook idempotency -------------------------------------------

    def notify(self, notify_id: str | None, dests: list[str], text: str, now: int) -> bool:
        """Atomically dedupe and enqueue a proactive notification."""
        with self._tx():
            if not self._claim_notify_id(notify_id, now):
                return False
            self._enqueue([(dest, text) for dest in dests], now)
            return True

    def claim_notify_id(self, notify_id: str | None, now: int) -> bool:
        """False if a notification with this id was already handled (7 days)."""
        with self._tx():
            return self._claim_notify_id(notify_id, now)

    def _claim_notify_id(self, notify_id: str | None, now: int) -> bool:
        return notify_id is None or self.db.execute('INSERT OR IGNORE INTO notify_ids VALUES(?,?)',
                                                    (notify_id, now)).rowcount == 1

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
