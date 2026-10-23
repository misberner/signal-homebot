"""Home Assistant cards the bot may render (with HA's own frontend, see
render/render.mjs) and send as pictures.

Claude proposes a card configuration; only a fixed list of display cards is
allowed: no custom cards, no markdown (templates), no iframes, maps or
pictures, nothing that fetches other URLs. A card is only drawn, never
clicked. Checked by the bot and again by the render broker.
"""
from __future__ import annotations
import json
import re

CARD_TYPES = frozenset({
    # Energy dashboard cards (a period applies)
    'energy-sankey', 'energy-distribution', 'energy-usage-graph', 'energy-solar-graph',
    'energy-devices-graph', 'energy-devices-detail-graph', 'energy-sources-table',
    'energy-gas-graph', 'energy-water-graph', 'energy-self-sufficiency-gauge',
    'energy-solar-consumed-gauge', 'energy-grid-neutrality-gauge', 'energy-carbon-consumed-gauge',
    'power-sankey', 'power-sources-graph',
    # History, statistics and states
    'history-graph', 'statistics-graph', 'statistic', 'sensor', 'gauge', 'entity', 'entities',
    'glance', 'tile', 'thermostat', 'humidifier', 'weather-forecast', 'logbook',
    # Layout
    'vertical-stack', 'horizontal-stack', 'grid',
})
STACKS = frozenset({'vertical-stack', 'horizontal-stack', 'grid'})
# Energy card periods, as the energy dashboard names them (DateRange).
PERIODS = ('today', 'yesterday', 'this_week', 'this_month', 'this_year', 'now-7d', 'now-30d', 'now-12m')
# Keys that could load something from elsewhere or style the page.
FORBIDDEN_KEYS = frozenset({'card_mod', 'style', 'url', 'url_path', 'image', 'camera_image', 'camera_view',
                            'icon_template', 'content', 'service', 'action', 'perform_action'})
MAX_CARD_JSON = 6000
DASHBOARD_RE = r'(lovelace|energy|[a-z0-9]+-[a-z0-9_-]+)(/[A-Za-z0-9_-]{1,64})?'
NOT_DASHBOARDS = ('developer-tools', 'media-browser')
LANGUAGE_RE = r'[a-z]{2,3}(-[A-Za-z]{2,4})?'


class CardError(ValueError):
    """Not renderable; the message is safe to show users."""


def check_card(card: object, depth: int = 0) -> dict:
    """A card configuration made only of allowed cards and plain values."""
    if not isinstance(card, dict) or not isinstance(card.get('type'), str):
        raise CardError('a card needs a type')
    if card['type'] not in CARD_TYPES:
        raise CardError(f'card type "{card["type"][:40]}" is not available')
    if depth > 2:
        raise CardError('cards nest at most two levels')
    if len(json.dumps(card)) > MAX_CARD_JSON:
        raise CardError('card configuration too large')
    for key, value in card.items():
        _check_value(key, value)
    if card['type'] in STACKS:
        cards = card.get('cards')
        if not isinstance(cards, list) or not 0 < len(cards) <= 6:
            raise CardError('a stack holds 1-6 cards')
        for child in cards:
            check_card(child, depth + 1)
    return card


def _check_value(key: str, value: object) -> None:
    if key in FORBIDDEN_KEYS or (isinstance(key, str) and key.endswith('_action')):
        raise CardError(f'"{key}" is not allowed in a card')
    if isinstance(value, str):
        if re.match(r'\s*(https?:|javascript:|data:|//|/local/|/api/)', value, re.I) or '{{' in value or '{%' in value:
            raise CardError(f'"{key}" must not be a link or template')
    elif isinstance(value, dict):
        if key != 'cards':
            for k, v in value.items():
                _check_value(k, v)
    elif isinstance(value, list):
        if key != 'cards':
            for item in value:
                _check_value(key, item)
    elif not (value is None or isinstance(value, (bool, int, float))):
        raise CardError(f'"{key}" has an unsupported value')


def check_request(request: object) -> dict:
    """A render request: {"card", "period"?, "language"?} or {"dashboard", "language"?}."""
    if not isinstance(request, dict) or set(request) - {'card', 'period', 'language', 'dashboard'}:
        raise CardError('malformed render request')
    language = request.get('language')
    if language is not None and not (isinstance(language, str) and re.fullmatch(LANGUAGE_RE, language)):
        raise CardError('invalid language')
    if 'dashboard' in request:
        path = request['dashboard']
        if ('card' in request or not isinstance(path, str) or not re.fullmatch(DASHBOARD_RE, path)
                or path.split('/')[0] in NOT_DASHBOARDS):
            raise CardError('not a dashboard')
        return {'dashboard': path, 'language': language}
    period = request.get('period')
    if period is not None and period not in PERIODS:
        raise CardError(f'period must be one of {", ".join(PERIODS)}')
    return {'card': check_card(request.get('card')), 'period': period, 'language': language}
