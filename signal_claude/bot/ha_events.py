"""Home Assistant events over its WebSocket API (read-only subscriptions).

The bot listens for state changes (to start tasks with a state trigger) and
for the ``homebot_notify`` event, which automations fire to message a user or
group. It only subscribes; it never sends commands over this connection.
"""
from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import Awaitable, Callable

import aiohttp

LOG = logging.getLogger('homebot')
NOTIFY_EVENT = 'homebot_notify'
EVENT_TYPES = ('state_changed', NOTIFY_EVENT)
# Home Assistant lets non-admin users subscribe only to a fixed list of
# events (state_changed is on it, custom events are not). Losing one of these
# is not worth the connection: the rest keeps working.
OPTIONAL = frozenset({NOTIFY_EVENT})
HEALTHY_SECONDS = 60  # a connection that lasted this long resets the backoff


class AuthFailed(RuntimeError):
    pass


async def listen(http: aiohttp.ClientSession, ha_url: str, token: str,
                 handler: Callable[[dict], Awaitable[None]], on_connect: Callable[[], None] = lambda: None,
                 event_types: tuple[str, ...] = EVENT_TYPES) -> None:
    """Deliver HA events to ``handler`` forever, reconnecting with backoff."""
    url = ha_url.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/api/websocket'
    backoff = 1
    refused: set[str] = set()  # optional events HA won't give this user: not asked for again
    while True:
        connected = None
        try:
            async with http.ws_connect(url, heartbeat=30, max_msg_size=16 << 20,
                                       timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                await _authenticate(ws, token)
                wanted = {n: t for n, t in enumerate(event_types, 1) if t not in refused}
                for n, event_type in wanted.items():
                    await ws.send_json({'id': n, 'type': 'subscribe_events', 'event_type': event_type})
                LOG.info('Listening to Home Assistant events')
                on_connect()
                connected = time.monotonic()
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        break
                    for item in _items(message.data):
                        if item.get('type') == 'result' and not item.get('success'):
                            event_type = wanted.get(item.get('id'))
                            if event_type not in OPTIONAL:
                                raise RuntimeError(f'subscription to {event_type} refused')
                            refused.add(event_type)
                            LOG.warning('Home Assistant does not let the ha_token user receive %s events '
                                        '(only admins may); use the /notify webhook instead (see the docs)',
                                        event_type)
                            continue
                        if item.get('type') == 'event' and isinstance(item.get('event'), dict):
                            try:
                                await handler(item['event'])
                            except Exception:
                                LOG.exception('Failed to handle a Home Assistant event')
            LOG.warning('Home Assistant event stream closed; reconnecting')
        except asyncio.CancelledError:
            raise
        except AuthFailed:
            LOG.warning('Home Assistant rejected the ha_token for events; retrying in 5 minutes')
            await asyncio.sleep(300)
            continue
        except Exception as err:
            LOG.warning('Home Assistant event stream interrupted (%s); reconnecting', type(err).__name__)
        if connected is not None and time.monotonic() - connected >= HEALTHY_SECONDS:
            backoff = 1
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def _authenticate(ws: aiohttp.ClientWebSocketResponse, token: str) -> None:
    first = await ws.receive_json(timeout=15)
    if first.get('type') != 'auth_required':
        raise RuntimeError('unexpected greeting')
    await ws.send_json({'type': 'auth', 'access_token': token})
    reply = await ws.receive_json(timeout=15)
    if reply.get('type') != 'auth_ok':
        raise AuthFailed()


def _items(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    items = data if isinstance(data, list) else [data]
    return [item for item in items if isinstance(item, dict)]
