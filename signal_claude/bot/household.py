"""Household settings: the bot's Signal account, people, groups, actions and switches.

Managed through the setup page (ingress) and stored in /data/homebot/settings.json.
Secrets and connection details (Claude token, HA access, notify token) stay in
the app options instead.
"""
from __future__ import annotations
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from .core import ENTITY_RE, NAME_RE, Settings, load_permissions

ACI_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
PHONE_RE = r'\+[1-9][0-9]{6,14}'
MODELS = ('opus', 'fable', 'sonnet', 'haiku')  # Claude Code aliases for the latest models
SKILL_NAME_RE = r'[a-z0-9][a-z0-9_-]{0,39}'
MAX_SKILLS = 30
PERSON_RE = r'person\.[a-z0-9_]+'


class HouseholdError(ValueError):
    """Invalid settings; the message is safe to show in the setup page."""


@dataclass(frozen=True)
class Household:
    signal_account: str = ''   # the bot's number once registered and verified
    pending_account: str = ''  # number with a registration code requested
    bot_aci: str = ''
    bot_name: str = 'HomeBot'
    users: dict[str, str] = field(default_factory=dict)   # name -> ACI
    groups: dict[str, str] = field(default_factory=dict)  # name -> Signal group id
    allow_writes: bool = False          # master switch for changes
    auto_approve: bool = False          # the reviewer's "allow" runs without asking
    # Ordered "allow|review|deny <pattern>" lines, last match wins (core.load_permissions).
    permissions: list = field(default_factory=list)
    rules: str = ''                     # admin-written rules for Claude and the reviewer (trusted)
    require_verified_identity: bool = True
    command_max_age: int = 300
    history_days: int = 7
    notes: str = ''  # admin-written hints for Claude (trusted), e.g. where presence data lives
    model: str = 'opus'  # Claude Code model alias
    # Admin-written instructions per topic (trusted): {name, description, body, always}.
    # Claude sees the descriptions and reads a body when the topic comes up.
    skills: list = field(default_factory=list)
    persons: dict[str, str] = field(default_factory=dict)  # user name -> HA person entity (presence)

    @property
    def permission_rules(self) -> list[tuple[str, str]]:
        return load_permissions(self.permissions)

    @property
    def settings(self) -> Settings:
        return Settings(command_max_age=self.command_max_age, history_days=self.history_days)

    @property
    def roles(self) -> dict[str, str]:
        """ACI -> user name."""
        return {aci: name for name, aci in self.users.items()}

    def change(self, **changes) -> 'Household':
        new = replace(self, **changes)
        new.validate()
        return new

    def validate(self) -> None:
        for number in (self.signal_account, self.pending_account):
            if number and not re.fullmatch(PHONE_RE, number):
                raise HouseholdError('Phone numbers must be in international format, e.g. +4915112345678')
        if self.bot_aci and not re.fullmatch(ACI_RE, self.bot_aci):
            raise HouseholdError('Invalid bot account ID')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', self.bot_name):
            raise HouseholdError('Bot name: letters, digits, _ or -, up to 32 characters')
        for kind, entries, pattern in (('user', self.users, ACI_RE), ('group', self.groups, r'[A-Za-z0-9+/=_-]{8,100}')):
            for name, ident in entries.items():
                if not re.fullmatch(NAME_RE, name):
                    raise HouseholdError(f'{kind} name "{name}": lowercase letters, digits and _, starting with a letter')
                if not isinstance(ident, str) or not re.fullmatch(pattern, ident):
                    raise HouseholdError(f'{kind} "{name}": invalid Signal ID')
        acis = list(self.users.values())
        if len(set(acis)) != len(acis):
            raise HouseholdError('Two users have the same Signal ID')
        if self.bot_aci and self.bot_aci in acis:
            raise HouseholdError("A user can't be the bot itself")
        if len(set(self.groups.values())) != len(self.groups):
            raise HouseholdError('Two groups have the same Signal ID')
        if set(self.users) & set(self.groups):
            raise HouseholdError('Users and groups need different names')
        try:
            load_permissions(self.permissions)
        except ValueError as err:
            raise HouseholdError(f'Permissions: {err}') from None
        if len(self.rules) > 4000:
            raise HouseholdError('Rules: at most 4000 characters')
        if self.model not in MODELS:
            raise HouseholdError(f'Model must be one of {", ".join(MODELS)}')
        if len(self.notes) > 4000:
            raise HouseholdError('Notes: at most 4000 characters')
        if len(self.skills) > MAX_SKILLS:
            raise HouseholdError(f'At most {MAX_SKILLS} skills')
        names = set()
        for skill in self.skills:
            if not isinstance(skill, dict) or set(skill) != {'name', 'description', 'body', 'always'}:
                raise HouseholdError('A skill has a name, description, body and "always"')
            name = skill['name']
            if not isinstance(name, str) or not re.fullmatch(SKILL_NAME_RE, name) or name in names:
                raise HouseholdError(f'Skill name "{name}": unique, lowercase letters, digits, _ or -')
            names.add(name)
            if not isinstance(skill['description'], str) or not 0 < len(skill['description'].strip()) <= 300:
                raise HouseholdError(f'Skill "{name}": a description of 1-300 characters (when it applies)')
            if not isinstance(skill['body'], str) or not 0 < len(skill['body'].strip()) <= 10000:
                raise HouseholdError(f'Skill "{name}": instructions of 1-10000 characters')
            if not isinstance(skill['always'], bool):
                raise HouseholdError(f'Skill "{name}": "always" must be true or false')
        for name, person in self.persons.items():
            if name not in self.users or not isinstance(person, str) or not re.fullmatch(PERSON_RE, person):
                raise HouseholdError(f'Presence of "{name}": a configured user and a person.* entity')
        if not 30 <= self.command_max_age <= 3600 or not 1 <= self.history_days <= 30:
            raise HouseholdError('Request age must be 30-3600 s and history 1-30 days')

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: object) -> 'Household':
        if isinstance(data, dict):
            data = migrate_lists(migrate_actions(data))
        if not isinstance(data, dict) or set(data) - set(cls.__dataclass_fields__):
            raise HouseholdError('Unknown settings')
        try:
            house = cls(**data)
        except TypeError as err:
            raise HouseholdError(str(err)) from None
        types = {'signal_account': str, 'pending_account': str, 'bot_aci': str, 'bot_name': str,
                 'users': dict, 'groups': dict, 'permissions': list, 'rules': str, 'allow_writes': bool,
                 'auto_approve': bool,
                 'require_verified_identity': bool, 'command_max_age': int, 'history_days': int,
                 'notes': str, 'model': str, 'skills': list, 'persons': dict}
        for key, typ in types.items():
            value = getattr(house, key)
            if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
                raise HouseholdError(f'{key} has the wrong type')
        house.validate()
        return house


def migrate_actions(data: dict) -> dict:
    """Settings from before 0.10 had an action table. Keep its scope: if it
    had entries, their entities become an active allowlist."""
    if 'actions' not in data:
        return data
    actions = data.get('actions')
    data = {k: v for k, v in data.items() if k != 'actions'}
    entities = []
    for rule in actions if isinstance(actions, list) else []:
        for entity in (rule.get('entities') or []) if isinstance(rule, dict) else []:
            if isinstance(entity, str) and re.fullmatch(ENTITY_RE + '|[a-z_]+\\.\\*', entity) \
                    and entity not in entities:
                entities.append(entity)
    if actions and 'allowlist_on' not in data:
        data['allowlist_on'], data['allow'] = True, entities
    return data


def migrate_lists(data: dict) -> dict:
    """0.10 had a separate allowlist and denylist; express them as ordered
    permission lines with the same effect (the denylist last, so it wins)."""
    if not {'deny', 'allow', 'allowlist_on'} & set(data):
        return data
    data = dict(data)
    deny, allow, only = data.pop('deny', []), data.pop('allow', []), data.pop('allowlist_on', False)
    lines = list(data.get('permissions') or [])
    if only:
        lines += ['deny *'] + [f'review {p}' for p in allow if isinstance(p, str)]
    lines += [f'deny {p}' for p in deny if isinstance(p, str)]
    data['permissions'] = lines
    return data


class HouseholdStore:
    """Atomic JSON file (0600) holding the Household."""

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Household:
        if not self.path.exists():
            return Household()
        return Household.from_json(json.loads(self.path.read_text()))

    def save(self, house: Household) -> None:
        house.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix='.settings-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(house.to_json(), f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
