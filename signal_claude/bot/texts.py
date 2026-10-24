"""The bot's own fixed texts around actions (questions and outcomes), in
Home Assistant's language: German or English.

Outcomes the bot reports itself are never written by Claude: a failure or an
unknown outcome must read exactly like one. Claude only supplies the sentence
for a confirmed success ("done"), and the state read back afterwards follows it.
"""
from __future__ import annotations

TEXTS = {
    'en': {
        'ask': 'Shall I do this?\n{what}\n(asked {where})\nCheck: {reason}\n'
               'Reply "yes" or react 👍 within {minutes} minutes; "no" or 👎 cancels.',
        'confirm_footer': 'Reply "yes" or react 👍 within {minutes} minutes; "no" or 👎 cancels.',
        'where_here': 'here', 'where_group': 'in the group', 'where_task': 'by a scheduled task',
        'confirm_privately': 'Confirm it in your private chat with me.',
        'done': '✅ {text}',
        'now': ' (now: {state})',
        'unreadable': ' (I could not read its state afterwards)',
        'mismatch': '⚠️ Home Assistant accepted {what}, but it now reports: {state}.',
        'failed': '❌ Home Assistant did not perform {what} ({detail}).',
        'unknown': '⚠️ The outcome of {what} is UNKNOWN ({detail}). It was not retried; please check the device.',
        'auto': 'auto-approved: {reason}', 'granted': 'approved with task #{task}',
        'not_allowed': 'Not allowed: {reason}.', 'not_doing': 'Not doing {what}: {reason}',
        'not_executed': 'Not executed: {reason}.',
        'cancelled': 'Cancelled.', 'stale': 'That question has already been answered or expired.',
        'scheduled_prefix': 'Scheduled #{id}: ',
    },
    'de': {
        'ask': 'Soll ich das ausführen?\n{what}\n(gefragt {where})\nPrüfung: {reason}\n'
               'Antworte „ja“ oder reagiere mit 👍 (innerhalb von {minutes} Minuten); „nein“ oder 👎 bricht ab.',
        'confirm_footer': 'Antworte „ja“ oder reagiere mit 👍 (innerhalb von {minutes} Minuten); '
                          '„nein“ oder 👎 bricht ab.',
        'where_here': 'hier', 'where_group': 'in der Gruppe', 'where_task': 'von einer geplanten Aufgabe',
        'confirm_privately': 'Bestätige das bitte in unserem privaten Chat.',
        'done': '✅ {text}',
        'now': ' (jetzt: {state})',
        'unreadable': ' (den Zustand danach konnte ich nicht lesen)',
        'mismatch': '⚠️ Home Assistant hat {what} angenommen, meldet jetzt aber: {state}.',
        'failed': '❌ Home Assistant hat {what} nicht ausgeführt ({detail}).',
        'unknown': '⚠️ Ob {what} ausgeführt wurde, ist UNBEKANNT ({detail}). Nicht wiederholt; '
                   'bitte am Gerät prüfen.',
        'auto': 'automatisch freigegeben: {reason}', 'granted': 'freigegeben mit Aufgabe #{task}',
        'not_allowed': 'Nicht erlaubt: {reason}.', 'not_doing': 'Mache ich nicht ({what}): {reason}',
        'not_executed': 'Nicht ausgeführt: {reason}.',
        'cancelled': 'Abgebrochen.', 'stale': 'Diese Frage ist schon beantwortet oder abgelaufen.',
        'scheduled_prefix': 'Geplant #{id}: ',
    },
}
STATE_WORDS = {
    'de': {'on': 'an', 'off': 'aus', 'open': 'offen', 'closed': 'geschlossen', 'opening': 'öffnet',
           'closing': 'schließt', 'unavailable': 'nicht erreichbar', 'unknown': 'unbekannt',
           'heat': 'heizen', 'cool': 'kühlen', 'auto': 'automatisch', 'playing': 'spielt',
           'paused': 'pausiert', 'idle': 'bereit', 'cleaning': 'saugt', 'docked': 'in der Station',
           'returning': 'fährt zurück'},
}
# What a service should leave the entity in, to notice a device that didn't react.
EXPECTED = {'turn_on': {'on'}, 'turn_off': {'off'}, 'open_cover': {'open', 'opening'},
            'close_cover': {'closed', 'closing'}}
EXPECTING_DOMAINS = frozenset({'light', 'switch', 'fan', 'cover', 'humidifier', 'input_boolean'})
# Entities whose state is just a timestamp of the last press/run.
STATELESS_DOMAINS = frozenset({'scene', 'script', 'button', 'input_button', 'todo'})
# Service fields whose result shows in a differently named attribute.
ATTRIBUTE_OF = {'position': 'current_position', 'tilt_position': 'current_tilt_position'}


def language(ha_language: str | None) -> str:
    return 'de' if (ha_language or '').lower().startswith('de') else 'en'


def t(lang: str, key: str, **values) -> str:
    return TEXTS.get(lang, TEXTS['en'])[key].format(**values)


def state_summary(lang: str, data: dict, state: dict) -> str:
    """The entity's state plus the attributes the call set, e.g. "an, brightness_pct 40"."""
    words = STATE_WORDS.get(lang, {})
    raw = str(state.get('state', ''))
    parts = [words.get(raw, raw)]
    attributes = state.get('attributes') if isinstance(state.get('attributes'), dict) else {}
    for key, wanted in data.items():
        if key == 'entity_id':
            continue
        if key == 'brightness_pct' and isinstance(attributes.get('brightness'), (int, float)):
            parts.append(f'brightness_pct {round(attributes["brightness"] * 100 / 255)}')
        elif isinstance(attributes.get(ATTRIBUTE_OF.get(key, key)), (str, int, float)):
            value = attributes[ATTRIBUTE_OF.get(key, key)]
            parts.append(f'{key} {words.get(value, value) if isinstance(value, str) else value}')
    return ', '.join(p for p in parts if p)[:200]


def unexpected(service: str, state: dict) -> bool:
    """The entity plainly did not do what the call asked (or went offline)."""
    domain, name = service.split('.')
    raw = state.get('state')
    if raw == 'unavailable':
        return True
    return domain in EXPECTING_DOMAINS and name in EXPECTED and raw not in EXPECTED[name]


def shows_state(service: str) -> bool:
    return service.split('.')[0] not in STATELESS_DOMAINS
