"""Setup page, served through Home Assistant ingress (admin login required).

Only accepts connections from Supervisor's ingress proxy, and every change
needs the ``X-HomeBot: 1`` header (cross-site pages can't send it without a
CORS preflight, which is never granted). Shows IDs and safety numbers, never
message text.
"""
from __future__ import annotations
import re
from pathlib import Path

from aiohttp import web

from .core import ClaudeError, build_schema
from .household import HouseholdError

INDEX = Path(__file__).with_name('static') / 'index.html'


def make_app(bot) -> web.Application:
    @web.middleware
    async def guard(request: web.Request, handler):
        if bot.cfg.ui_peer is not None and request.remote != bot.cfg.ui_peer:
            raise web.HTTPForbidden(text='Open this page from Home Assistant (Settings → Apps → Open Web UI).')
        if request.method != 'GET' and request.headers.get('X-HomeBot') != '1':
            raise web.HTTPForbidden(text='Missing X-HomeBot header')
        try:
            return await handler(request)
        except HouseholdError as err:
            return web.json_response({'error': str(err)}, status=400)
        except SignalErrorOut as err:
            return web.json_response({'error': str(err)}, status=400)

    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    ui = SetupApi(bot)
    app.router.add_get('/', lambda _: web.FileResponse(INDEX, headers={'Cache-Control': 'no-store'}))
    app.router.add_get('/api/state', ui.state)
    for name in ('register', 'verify', 'users/add', 'users/remove', 'groups/add', 'groups/remove',
                 'trust', 'hello', 'settings', 'test'):
        app.router.add_post(f'/api/{name}', getattr(ui, name.replace('/', '_')))
    return app


class SignalErrorOut(RuntimeError):
    pass


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        raise HouseholdError('Invalid request') from None
    if not isinstance(data, dict):
        raise HouseholdError('Invalid request')
    return data


def text(data: dict, key: str, required: bool = True) -> str:
    value = data.get(key, '')
    if not isinstance(value, str) or (required and not value.strip()):
        raise HouseholdError(f'{key} is required')
    return value.strip()


class SetupApi:
    def __init__(self, bot):
        self.bot = bot

    async def rpc(self, method: str, params: dict, account: str | None = None):
        from .server import SignalError
        try:
            return await self.bot.signal_rpc(method, params, account=account)
        except SignalError as err:
            raise SignalErrorOut(str(err) or f'Signal {method} failed') from None
        except Exception as err:
            raise SignalErrorOut(f'Signal daemon unreachable ({type(err).__name__})') from None

    # ---------------------------------------------------------------- read

    async def state(self, request: web.Request) -> web.Response:
        bot, house = self.bot, self.bot.house
        users = []
        for name, aci in house.users.items():
            entry = {'name': name, 'aci': aci, 'trust': None, 'safety_number': None}
            if house.signal_account:
                try:
                    ids = await self.rpc('listIdentities', {'number': aci})
                    if isinstance(ids, list) and ids and isinstance(ids[0], dict):
                        entry.update(trust=ids[0].get('trustLevel'), safety_number=ids[0].get('safetyNumber'))
                except SignalErrorOut:
                    pass
            users.append(entry)
        known_groups = []
        if house.signal_account:
            try:
                listed = await self.rpc('listGroups', {})
                known_groups = [g for g in listed if isinstance(g, dict)] if isinstance(listed, list) else []
            except SignalErrorOut:
                pass
        by_id = {g.get('id'): g for g in known_groups}
        groups = [{'name': name, 'id': gid, 'title': (by_id.get(gid) or {}).get('name'),
                   'problem': await bot.group_problem(gid) if house.signal_account else 'not registered'}
                  for name, gid in house.groups.items()]
        candidates = [{'id': g.get('id'), 'title': g.get('name'),
                       'members': [m.get('uuid') for m in g.get('members') or [] if isinstance(m, dict)]}
                      for g in known_groups if g.get('id') not in bot.group_ids and g.get('isMember')]
        for gid in bot.seen_groups:
            if gid not in bot.group_ids and gid not in {c['id'] for c in candidates}:
                candidates.append({'id': gid, 'title': None, 'members': []})
        return web.json_response({
            'account': {'number': house.signal_account, 'pending': house.pending_account,
                        'bot_aci': house.bot_aci, 'bot_name': house.bot_name},
            'users': users,
            'groups': groups,
            'seen_senders': [{'aci': aci, 'profile_name': name, 'seen': ts}
                             for aci, (name, ts) in reversed(bot.seen_senders.items()) if aci not in house.roles],
            'group_candidates': candidates,
            'actions': house.actions,
            'allow_writes': house.allow_writes,
            'require_verified_identity': house.require_verified_identity,
            'command_max_age': house.command_max_age,
            'history_days': house.history_days,
            'notes': house.notes,
            'model': house.model,
            'ha': {'kind': bot.cfg.mcp_kind, 'write_token': bool(bot.cfg.ha_token)},
            'claude_configured': bot.cfg.claude_configured,
        })

    # ---------------------------------------------------------------- Signal account

    async def register(self, request: web.Request) -> web.Response:
        data = await body(request)
        number = re.sub(r'[\s()/-]', '', text(data, 'number'))
        house = self.bot.house.change(pending_account=number)  # validates the format
        params = {'voice': data.get('voice') is True}
        if captcha := text(data, 'captcha', required=False):
            params['captcha'] = captcha
        await self.rpc('register', params, account=number)
        self.bot.update_house(house)
        return web.json_response({'ok': True, 'message': f'Signal sent a code to {number}.'})

    async def verify(self, request: web.Request) -> web.Response:
        data = await body(request)
        number = self.bot.house.pending_account
        if not number:
            raise HouseholdError('Request a code first')
        code = re.sub(r'\D', '', text(data, 'code'))
        params = {'verificationCode': code}
        if pin := text(data, 'pin', required=False):
            params['pin'] = pin
        await self.rpc('verify', params, account=number)
        self.bot.update_house(self.bot.house.change(signal_account=number, pending_account='', bot_aci=''))
        self.bot.store.set_cursor('')
        await self.bot.refresh_bot_aci()
        return web.json_response({'ok': True, 'message': f'{number} is registered.'})

    # ---------------------------------------------------------------- people and groups

    async def users_add(self, request: web.Request) -> web.Response:
        data = await body(request)
        name, aci = text(data, 'name').lower(), text(data, 'aci').lower()
        house = self.bot.house
        if name in house.users:
            raise HouseholdError(f'There is already a user called {name}')
        self.bot.update_house(house.change(users={**house.users, name: aci}))
        self.bot.seen_senders.pop(aci, None)
        return web.json_response({'ok': True})

    async def users_remove(self, request: web.Request) -> web.Response:
        name = text(await body(request), 'name')
        house = self.bot.house
        actions = [{**rule, 'senders': [u for u in rule.get('senders', []) if u != name]} for rule in house.actions]
        self.bot.update_house(house.change(users={n: a for n, a in house.users.items() if n != name},
                                           actions=actions))
        return web.json_response({'ok': True})

    async def groups_add(self, request: web.Request) -> web.Response:
        data = await body(request)
        name, gid = text(data, 'name').lower(), text(data, 'id')
        house = self.bot.house
        if name in house.groups:
            raise HouseholdError(f'There is already a group called {name}')
        self.bot.update_house(house.change(groups={**house.groups, name: gid}))
        self.bot.seen_groups.pop(gid, None)
        return web.json_response({'ok': True})

    async def groups_remove(self, request: web.Request) -> web.Response:
        name = text(await body(request), 'name')
        house = self.bot.house
        self.bot.update_house(house.change(groups={n: g for n, g in house.groups.items() if n != name}))
        return web.json_response({'ok': True})

    async def trust(self, request: web.Request) -> web.Response:
        data = await body(request)
        name = text(data, 'name')
        aci = self.bot.house.users.get(name)
        if not aci:
            raise HouseholdError('Unknown user')
        number = re.sub(r'\D', '', text(data, 'safety_number'))
        if len(number) != 60:
            raise HouseholdError('A safety number has 60 digits')
        await self.rpc('trust', {'recipient': aci, 'verifiedSafetyNumber': number})
        return web.json_response({'ok': True, 'message': f'{name} is verified.'})

    async def hello(self, request: web.Request) -> web.Response:
        """Message a person so their phone shows the bot's safety number."""
        name = text(await body(request), 'name')
        aci = self.bot.house.users.get(name)
        if not aci:
            raise HouseholdError('Unknown user')
        if not self.bot.house.signal_account:
            raise HouseholdError('Register the bot first')
        self.bot.enqueue((f'dm:{aci}', self.bot.verify_note()))
        return web.json_response({'ok': True, 'message': f'Sent a message to {name}. Their phone now shows '
                                                         'the safety number in the chat with the bot.'})

    # ---------------------------------------------------------------- settings and tests

    async def settings(self, request: web.Request) -> web.Response:
        data = await body(request)
        house = self.bot.house
        changes = {}
        for key, typ in (('bot_name', str), ('allow_writes', bool), ('require_verified_identity', bool),
                         ('command_max_age', int), ('history_days', int), ('actions', list), ('notes', str),
                         ('model', str)):
            if key in data:
                value = data[key]
                if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
                    raise HouseholdError(f'{key} has the wrong type')
                changes[key] = value
        self.bot.update_house(house.change(**changes))
        return web.json_response({'ok': True})

    async def test(self, request: web.Request) -> web.Response:
        what = text(await body(request), 'what')
        if what == 'ha':
            self.bot.tools._preflight_ok_until = 0
            try:
                await self.bot.tools.preflight()
            except Exception as err:
                return web.json_response({'ok': False, 'message': f'Home Assistant: {err}'})
            return web.json_response({'ok': True, 'message': 'Home Assistant is reachable and read-only.'})
        if what == 'claude':
            try:
                await self.bot.ask_broker('Reply with the single word OK and no actions.', build_schema([]))
            except ClaudeError as err:
                return web.json_response({'ok': False, 'message': f'Claude: {err.kind}'})
            return web.json_response({'ok': True, 'message': 'Claude answered.'})
        raise HouseholdError('Unknown test')
