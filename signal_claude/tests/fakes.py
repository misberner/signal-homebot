"""Realistic fakes for integration tests. No real Signal, Claude or HA is used.

- FakeSignalDaemon mirrors signal-cli 0.14.9 HttpServerHandler/SseEventBuffer:
  GET /api/v1/check, POST /api/v1/rpc (JSON-RPC 2.0), GET /api/v1/events
  (``id:<start>-<seq>``, ``event:receive``, ``data:{"account","envelope"}``,
  Last-Event-ID replay of buffered events, ``:`` keep-alives).
- FakeHomeAssistant records REST service calls and serves a real MCP SDK
  streamable-HTTP server (uvicorn) exposing GetLiveContext plus a write tool
  that must never be called.
- make_fake_claude writes an executable standing in for ``claude -p``.
"""
from __future__ import annotations
import asyncio
import json
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

from aiohttp import web

from tests.ids import ACCOUNT, BOT, GROUP, STRANGER, USER1, USER2  # noqa: F401
HA_MCP_SECRET_PATH = '/private_testsecret'


def envelope(sender=USER1, text='hello', ts=None, group=None, server_ts=None,
             expires=0, mentions=None, **data_extra) -> dict:
    """A receive event as signal-cli 0.14.9 serializes it (message-envelope.schema.json)."""
    ts = ts if ts is not None else int(time.time() * 1000)
    dm = {'timestamp': ts, 'message': text, 'expiresInSeconds': expires,
          'viewOnce': False, **data_extra}
    if text is None:
        del dm['message']
    if group is not None:
        dm['groupInfo'] = {'groupId': group, 'groupName': 'Home', 'revision': 3, 'type': 'DELIVER'}
    if mentions is not None:
        dm['mentions'] = mentions
    return {'account': ACCOUNT, 'envelope': {
        'source': sender, 'sourceNumber': None, 'sourceUuid': sender, 'sourceName': 'Someone',
        'sourceDevice': 1, 'timestamp': ts,
        'serverReceivedTimestamp': server_ts if server_ts is not None else ts + 50,
        'serverDeliveredTimestamp': (server_ts if server_ts is not None else ts) + 80,
        'dataMessage': dm}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


IMAGE_TOKEN = 'c9ba749c71c171e1c4bbe9c7751c579d'
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15'
       b'\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00'
       b'\x00IEND\xaeB`\x82')

class FakeSignalDaemon:
    def __init__(self):
        self.prefix = f'{int(time.time() * 1000)}-'
        self.events: list[tuple[int, str]] = []
        self.seq = 0
        self.clients: list[asyncio.Queue] = []
        self.sent: list[dict] = []
        self.rpc_failures = 0          # next N sends fail with a JSON-RPC error
        self.send_results = 'SUCCESS'  # or e.g. 'NETWORK_FAILURE'
        self.connects: list[str | None] = []
        self.identities = {USER1: 'TRUSTED_VERIFIED', USER2: 'TRUSTED_VERIFIED'}
        self.safety_numbers = {USER1: '1' * 60, USER2: '2' * 60}
        self.accounts: list[dict] = [{'number': ACCOUNT, 'aci': BOT}]
        self.pending_registration: str | None = None
        self.require_captcha = True
        self.next_ts = int(time.time() * 1000)
        self.group_members = [BOT, USER1, USER2]
        self.pending_members: list[str] = []
        self.rpc_methods: list[str] = []
        self.indicators: list[tuple[str, dict]] = []
        self.reject_edits = False
        self.profile_names: dict[str, str] = {}
        self.edits: list[tuple[int, str]] = []
        self.runner: web.AppRunner | None = None
        self.url = ''

    async def start(self):
        app = web.Application()
        app.router.add_get('/api/v1/check', lambda _: web.Response(status=200))
        app.router.add_post('/api/v1/rpc', self.rpc)
        app.router.add_get('/api/v1/events', self.events_endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = free_port()
        await web.TCPSite(self.runner, '127.0.0.1', port).start()
        self.url = f'http://127.0.0.1:{port}'

    async def stop(self):
        for q in self.clients:
            q.put_nowait(None)
        await self.runner.cleanup()

    def push(self, payload: dict | str) -> str:
        self.seq += 1
        data = payload if isinstance(payload, str) else json.dumps(payload)
        self.events.append((self.seq, data))
        for q in self.clients:
            q.put_nowait((self.seq, data))
        return self.prefix + str(self.seq)

    def drop_connections(self):
        for q in self.clients:
            q.put_nowait(None)

    async def rpc(self, req: web.Request) -> web.Response:
        if not req.content_type.startswith('application/json'):
            return web.Response(status=415)
        body = await req.json()
        assert body['jsonrpc'] == '2.0' and 'id' in body
        self.rpc_methods.append(body['method'])
        params = body.get('params') or {}
        def ok(result):
            return web.json_response({'jsonrpc': '2.0', 'id': body['id'], 'result': result})

        def error(message, code=-1):
            return web.json_response({'jsonrpc': '2.0', 'id': body['id'],
                                      'error': {'code': code, 'message': message}})
        method = body['method']
        if method == 'listIdentities':  # ListIdentitiesCommand JsonIdentity
            aci = params.get('number')
            sn = self.safety_numbers.get(aci, '0' * 60)
            return ok([{'number': None, 'uuid': aci, 'fingerprint': 'ab' * 33,
                        'safetyNumber': ' '.join(sn[i:i + 5] for i in range(0, 60, 5)),
                        'scannableSafetyNumber': None, 'trustLevel': self.identities[aci],
                        'addedTimestamp': 1}] if aci in self.identities else [])
        if method in ('sendTyping', 'sendReceipt'):
            self.indicators.append((method, params))
            return ok({})
        if method == 'listContacts':  # ListContactsCommand JsonContact(number, uuid, name, profile, ...)
            return ok([{'number': None, 'uuid': u, 'name': '', 'profile': {'givenName': n, 'familyName': None}}
                       for u, n in self.profile_names.items() if u in (params.get('recipient') or [])])
        if method == 'listAccounts':  # ListAccountsCommand JsonAccount(number, aci)
            return ok(self.accounts)
        if method == 'register':  # RegistrationParams(voice, captcha, ...)
            if self.require_captcha and not params.get('captcha'):
                return error('Captcha required for verification, use --captcha CAPTCHA')
            self.pending_registration = params['account']
            return ok({})
        if method == 'verify':  # VerifyParams(verificationCode, pin)
            if params.get('account') != self.pending_registration or params.get('verificationCode') != '123456':
                return error('Verify error: Invalid verification code')
            self.accounts = [{'number': params['account'], 'aci': BOT}]
            return ok({})
        if method == 'trust':  # TrustCommand: recipient, verifiedSafetyNumber
            aci, sn = params.get('recipient'), params.get('verifiedSafetyNumber')
            if sn != self.safety_numbers.get(aci):
                return error('Failed to set the trust for this number, make sure the number and the '
                             'fingerprint/safety number are correct.')
            self.identities[aci] = 'TRUSTED_VERIFIED'
            return ok({})
        if body['method'] == 'listGroups':  # ListGroupsCommand JsonGroup
            group = {'id': GROUP, 'name': 'Home', 'description': None, 'isMember': True, 'isBlocked': False,
                     'messageExpirationTime': 0,
                     'members': [{'number': None, 'uuid': u, 'isAdmin': u == USER1} for u in self.group_members],
                     'pendingMembers': [{'number': None, 'uuid': u} for u in self.pending_members],
                     'requestingMembers': [], 'admins': [], 'banned': [], 'permissionAddMember': 'ONLY_ADMINS',
                     'permissionEditDetails': 'ONLY_ADMINS', 'permissionSendMessage': 'EVERY_MEMBER',
                     'groupInviteLink': None, 'isTerminated': False}
            wanted = params.get('groupId') or [GROUP]
            return ok([group] if GROUP in wanted else [])
        if body['method'] != 'send':
            return web.json_response({'jsonrpc': '2.0', 'id': body['id'],
                                      'error': {'code': -32601, 'message': 'Method not implemented'}})
        if self.rpc_failures:
            self.rpc_failures -= 1
            return web.json_response({'jsonrpc': '2.0', 'id': body['id'],
                                      'error': {'code': -5, 'message': 'Failed to send message due to rate limiting'}})
        self.next_ts = max(self.next_ts + 1, int(time.time() * 1000))
        edit_of = body['params'].get('editTimestamp')
        if edit_of is not None:
            # Like Signal-iOS: the latest revision takes the edit's timestamp, and
            # an edit must target the latest revision (an earlier timestamp now
            # names a past revision, which can't be edited; the edit is dropped).
            original = next((m for m in self.sent if m['_ts'] == edit_of), None)
            if original is None or self.reject_edits:
                return error('Failed to send message: edit target not found')
            self.edits.append((edit_of, body['params']['message']))
            original['message'] = body['params']['message']
            original['_edits'] = original.get('_edits', 0) + 1
            original['_ts'] = self.next_ts
            return ok({'timestamp': self.next_ts, 'results': [{'type': 'SUCCESS'}]})
        self.sent.append({**body['params'], '_ts': self.next_ts})
        recipients = body['params'].get('recipient') or [USER1, USER2]
        return ok({'timestamp': self.next_ts,
                   'results': [{'recipientAddress': {'uuid': r}, 'type': self.send_results} for r in recipients]})

    async def events_endpoint(self, req: web.Request) -> web.StreamResponse:
        last = req.headers.get('Last-Event-ID')
        self.connects.append(last)
        resp = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
        await resp.prepare(req)
        q: asyncio.Queue = asyncio.Queue()
        if last is not None:
            last_seq = int(last[len(self.prefix):]) if last.startswith(self.prefix) else 0
            for seq, data in self.events:
                if seq > last_seq:
                    q.put_nowait((seq, data))
        self.clients.append(q)
        try:
            await resp.write(b':\n')
            while True:
                item = await q.get()
                if item is None:
                    break
                seq, data = item
                await resp.write(f'id:{self.prefix}{seq}\nevent:receive\ndata:{data}\n\n'.encode())
        finally:
            self.clients.remove(q)
        return resp

    def messages_to(self, dest: str) -> list[dict]:
        return [p for p in self.sent
                if (dest.startswith('group:') and p.get('groupId') == dest[6:]) or
                   (dest.startswith('dm:') and p.get('recipient') == [dest[3:]])]

    def texts_to(self, dest: str) -> list[str]:
        return [p['message'] for p in self.messages_to(dest)]


class FakeHomeAssistant:
    """HA REST service endpoint + a real MCP SDK server on /api/mcp/assist.

    The MCP server (uvicorn thread) is shared by all tests; it serves whichever
    FakeHomeAssistant is ``active``.
    """
    active: 'FakeHomeAssistant | None' = None
    _urls: dict[str, str] = {}

    def __init__(self, token='ha-test-token', live_text='light.test_lamp: off'):
        self.token = token
        self.live_text = live_text
        self.calls: list[tuple[str, dict]] = []
        self.mode = 'ok'  # ok | 400 | 500 | slow | drop
        self.mcp_calls: list[str] = []
        self.mcp_args: list[dict] = []
        self.mcp_auth_headers: list[str | None] = []
        self.mcp_fail = False
        self.overview: dict | None = None  # ha-mcp ha_get_overview result override
        self.states = {'light.test_lamp': 'off', 'sensor.living_room_temperature': '21.5',
                       'image.vacuum_map': '2026-10-21T10:34:14+00:00'}
        self.attributes = {'image.vacuum_map': {
            'entity_picture': '/api/image_proxy/image.vacuum_map?token=' + IMAGE_TOKEN}}
        self.runner = None
        self.url = ''

    @property
    def mcp_url(self) -> str:
        return FakeHomeAssistant._urls['admin'] + '/api/mcp/assist'

    @property
    def ha_mcp_url(self) -> str:
        """ha-mcp admin endpoint: authenticated only by its secret path."""
        return FakeHomeAssistant._urls['admin'] + HA_MCP_SECRET_PATH

    @property
    def ha_mcp_readonly_url(self) -> str:
        """ha-mcp's per-connection /readonly alias: write tools hidden."""
        return FakeHomeAssistant._urls['readonly'] + HA_MCP_SECRET_PATH + '/readonly'

    async def start(self):
        app = web.Application()
        app.router.add_post('/api/services/{domain}/{service}', self.service)
        app.router.add_get('/api/image_proxy/{entity_id}', self.image_proxy)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        port = free_port()
        await web.TCPSite(self.runner, '127.0.0.1', port).start()
        self.url = f'http://127.0.0.1:{port}'
        FakeHomeAssistant.active = self
        if not FakeHomeAssistant._urls:
            FakeHomeAssistant._urls = {'admin': _start_mcp(False), 'readonly': _start_mcp(True)}

    async def image_proxy(self, req: web.Request) -> web.Response:
        # Like HA: the entity's access token authorizes this one image.
        if req.match_info['entity_id'] != 'image.vacuum_map' or req.query.get('token') != IMAGE_TOKEN:
            return web.Response(status=401)
        return web.Response(body=PNG, content_type='image/png')

    async def stop(self):
        FakeHomeAssistant.active = None
        await self.runner.cleanup()

    async def service(self, req: web.Request) -> web.Response:
        if req.headers.get('Authorization') != f'Bearer {self.token}':
            return web.Response(status=401)
        name = f'{req.match_info["domain"]}.{req.match_info["service"]}'
        self.calls.append((name, await req.json()))
        if self.mode == 'slow':
            await asyncio.sleep(5)
        if self.mode == 'drop' and req.transport:
            req.transport.close()
            await asyncio.sleep(1)
        if self.mode in ('400', '500'):
            return web.Response(status=int(self.mode))
        return web.json_response([])


def _start_mcp(read_only: bool) -> str:
    """One MCP server per mode, like ha-mcp's normal and /readonly endpoints."""
    import uvicorn
    from mcp.server import MCPServer
    server = MCPServer('fake-home-assistant')

    @server.tool(name='homeassistant__GetLiveContext')
    def get_live_context() -> str:
        """Provides real-time information about the CURRENT state of devices."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('homeassistant__GetLiveContext')
        if fake.mcp_fail:
            raise RuntimeError('boom')
        return fake.live_text

    @server.tool(name='HassTurnOn')
    def hass_turn_on(name: str) -> str:
        """Turns on a device. The bot must never call this."""
        FakeHomeAssistant.active.mcp_calls.append('HassTurnOn')
        return 'ok'

    # ha-mcp (homeassistant-ai/ha-mcp 8.7.0) tool names and result shape.
    @server.tool(name='ha_get_overview')
    def ha_get_overview(detail_level: str = 'minimal', domains: list[str] | None = None,
                        limit: int | None = None, include_entity_id: bool | None = None,
                        include_state: bool | None = None, include_notifications: bool = True,
                        fields: list[str] | None = None) -> str:
        """Get AI-friendly system overview."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_get_overview')
        fake.mcp_args.append({'detail_level': detail_level, 'domains': domains, 'limit': limit,
                              'include_entity_id': include_entity_id, 'include_state': include_state,
                              'include_notifications': include_notifications, 'fields': fields})
        result = fake.overview or {'success': True, 'domain_stats': {'light': {
            'count': 1, 'states_summary': {'off': 1},
            'entities': [{'friendly_name': 'Test Lamp', 'entity_id': 'light.test_lamp', 'state': 'off'}]}},
            'pagination': {'has_more': False}}
        if fields and 'system_info' in fields:
            result = {**result, 'system_info': {'latitude': 51.0, 'longitude': 7.0}}
        if read_only and fake.overview is None:
            result = {**result, 'read_only_mode': True, 'read_only_mode_hint': 'Read Only Mode is ON'}
        return json.dumps(result)

    @server.tool(name='ha_get_state')
    def ha_get_state(entity_id: str | list[str], fields: list[str] | None = None) -> str:
        """Get current state of entities."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_get_state')
        fake.mcp_args.append({'entity_id': entity_id, 'fields': fields})
        ids = entity_id if isinstance(entity_id, list) else [entity_id]
        return json.dumps({'data': {'success': True, 'states': {
            e: {'entity_id': e, 'state': fake.states.get(e, 'unknown'), 'attributes': fake.attributes.get(e, {})}
            for e in ids}}})

    @server.tool(name='ha_config_get_automation')
    def ha_config_get_automation(identifier: str) -> str:
        """Get automation configuration."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_config_get_automation')
        return json.dumps({'success': True, 'automation_id': identifier,
                           'config': {'triggers': [{'trigger': 'time', 'at': '06:00'}],
                                      'actions': [{'action': 'light.turn_on'}]}})

    @server.tool(name='ha_config_get_script')
    def ha_config_get_script(script_id: str) -> str:
        """Get script configuration."""
        FakeHomeAssistant.active.mcp_calls.append('ha_config_get_script')
        return json.dumps({'success': True, 'script_id': script_id.removeprefix('script.'),
                           'config': {'sequence': [{'action': 'water_heater.set_operation_mode'}]}})

    @server.tool(name='ha_config_get_dashboard')
    def ha_config_get_dashboard(url_path: str | None = None, list_only: bool = False, view_path: str | None = None,
                                entity_id: str | None = None, card_type: str | None = None,
                                heading: str | None = None, query: str | None = None,
                                include_config: bool = False, include_screenshot: bool = False) -> list:
        """Get Lovelace dashboards."""
        from mcp.server.mcpserver import Image
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_config_get_dashboard')
        fake.mcp_args.append({'url_path': url_path, 'list_only': list_only, 'include_screenshot': include_screenshot})
        text = json.dumps({'success': True, 'url_path': url_path,
                           'config': {'views': [{'title': 'Heizung', 'cards': [{'type': 'tile'}]}]}})
        return [text, Image(data=PNG, format='png')] if include_screenshot else [text]

    @server.tool(name='ha_search')
    def ha_search(query: str | None = None, domain_filter: str | None = None, area_filter: str | None = None,
                  limit: int = 10, exact_match: bool = True, result_fields: list[str] | None = None) -> str:
        """Search entities."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_search')
        fake.mcp_args.append({'query': query, 'domain_filter': domain_filter, 'limit': limit})
        return json.dumps({'success': True, 'entities': [{'entity_id': e, 'state': st}
                                                         for e, st in fake.states.items()]})

    @server.tool(name='ha_get_history')
    def ha_get_history(entity_ids: str | list[str], start_time: str | None = None, limit: int | None = None,
                       minimal_response: bool = True) -> str:
        """State history."""
        fake = FakeHomeAssistant.active
        fake.mcp_calls.append('ha_get_history')
        fake.mcp_args.append({'entity_ids': entity_ids, 'start_time': start_time, 'limit': limit})
        return json.dumps({'success': True, 'entities': {}})

    if not read_only:  # ha-mcp hides write-capable tools in Read Only Mode
        @server.tool(name='ha_call_service')
        def ha_call_service(domain: str, service: str, entity_id: str | None = None) -> str:
            """Calls any Home Assistant service. The bot must never call this."""
            FakeHomeAssistant.active.mcp_calls.append('ha_call_service')
            return 'ok'

        @server.tool(name='ha_write_file')
        def ha_write_file(path: str, content: str) -> str:
            """Writes a config file. The bot must never call this."""
            FakeHomeAssistant.active.mcp_calls.append('ha_write_file')
            return 'ok'

    mcp_app = server.streamable_http_app(streamable_http_path='/api/mcp/assist', stateless_http=True)

    async def auth(scope, receive, send):
        if scope['type'] == 'http':
            fake = FakeHomeAssistant.active
            header = dict(scope['headers']).get(b'authorization')
            if fake:
                fake.mcp_auth_headers.append(header.decode() if header else None)
            if scope['path'] in (HA_MCP_SECRET_PATH, HA_MCP_SECRET_PATH + '/readonly'):
                # ha-mcp: the secret path is the credential.
                scope = {**scope, 'path': '/api/mcp/assist', 'raw_path': b'/api/mcp/assist'}
                await mcp_app(scope, receive, send)
                return
            token = fake.token if fake else None
            if header != f'Bearer {token}'.encode():
                await send({'type': 'http.response.start', 'status': 401, 'headers': []})
                await send({'type': 'http.response.body', 'body': b''})
                return
        await mcp_app(scope, receive, send)

    port = free_port()
    server_ = uvicorn.Server(uvicorn.Config(auth, host='127.0.0.1', port=port,
                                           log_level='warning', lifespan='on'))
    threading.Thread(target=server_.run, daemon=True).start()
    deadline = time.time() + 15
    while not server_.started and time.time() < deadline:
        time.sleep(0.02)
    if not server_.started:
        raise RuntimeError('fake MCP server did not start')
    return f'http://127.0.0.1:{port}'


FAKE_CLAUDE = r'''#!{python}
import asyncio, json, os, sys, time
spec = json.load(open({spec!r}))
prompt = sys.stdin.read()
argv = sys.argv[1:]
mcp_config = json.load(open(argv[argv.index('--mcp-config') + 1])) if '--mcp-config' in argv else None
with open({record!r}, 'a') as f:
    f.write(json.dumps({{'argv': argv, 'env': dict(os.environ), 'prompt': prompt,
                        'cwd': os.getcwd(), 'mcp_config': mcp_config}}) + '\n')
tool_text = None
if spec.get('call_tool') and mcp_config:
    async def use_tool():
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        server = mcp_config['mcpServers']['ha']
        async with httpx2.AsyncClient(headers=server.get('headers', {{}})) as http:
            async with Client(streamable_http_client(server['url'], http_client=http)) as client:
                names = [t.name for t in (await client.list_tools()).tools]
                result = await client.call_tool(spec['call_tool']['name'], spec['call_tool']['args'])
                text = ' '.join(getattr(c, 'text', '') for c in result.content)
                images = [[c.mime_type, len(c.data)] for c in result.content if getattr(c, 'type', '') == 'image']
                return json.dumps({{'tools': names, 'error': result.is_error, 'text': text, 'images': images}})
    try:
        tool_text = asyncio.run(use_tool())
    except BaseException as err:
        tool_text = json.dumps({{'exception': type(err).__name__}})
mode = spec.get('mode', 'ok')
if mode == 'hang':
    time.sleep(30)
if mode == 'auth':
    print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': True,
                      'api_error_status': 401,
                      'result': 'Failed to authenticate. API Error: 401 OAuth access token is invalid.'}}))
    sys.exit(1)
if mode == 'limit':
    print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': True,
                      'api_error_status': 429, 'result': 'Claude usage limit reached.'}}))
    sys.exit(1)
if mode == 'garbage':
    print('not json'); sys.exit(2)
out = dict(spec.get('output', {{'reply': 'ok', 'actions': []}}))
if tool_text is not None:
    out['reply'] = tool_text
stream = '--output-format' in argv and argv[argv.index('--output-format') + 1] == 'stream-json'
if stream:
    print(json.dumps({{'type': 'system', 'subtype': 'init'}}))
    if spec.get('call_tool'):
        print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{
            'type': 'tool_use', 'name': 'mcp__ha__' + spec['call_tool']['name'],
            'input': spec['call_tool']['args']}}]}}}}))
    for name in spec.get('progress', []):
        print(json.dumps({{'type': 'assistant', 'message': {{'content': [{{
            'type': 'tool_use', 'name': name, 'input': {{'query': 'presence'}}}}]}}}}), flush=True)
        time.sleep(spec.get('progress_delay', 0))
    if spec.get('tool_result_size'):  # like a large HA read: one long stream-json line
        print(json.dumps({{'type': 'user', 'message': {{'content': [{{
            'type': 'tool_result', 'content': 'x' * spec['tool_result_size']}}]}}}}))
print(json.dumps({{'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,
                  'result': json.dumps(out), 'structured_output': out, 'api_error_status': None,
                  'modelUsage': {{'claude-opus-5-5': {{}}}}}}))
'''


class FakeClaude:
    def __init__(self, directory: Path):
        self.spec = directory / 'claude_spec.json'
        self.record = directory / 'claude_calls.jsonl'
        self.path = directory / 'fake-claude'
        self.path.write_text(FAKE_CLAUDE.format(python=sys.executable, spec=str(self.spec),
                                                record=str(self.record)))
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.set()

    def set(self, reply='ok', actions=(), mode='ok', call_tool=None, progress=(), progress_delay=0,
            tool_result_size=0):
        self.spec.write_text(json.dumps({'mode': mode, 'call_tool': call_tool, 'progress': list(progress),
                                         'progress_delay': progress_delay, 'tool_result_size': tool_result_size,
                                         'output': {'reply': reply, 'actions': list(actions)}}))

    @property
    def calls(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


def scrubbed_env():
    """Secrets that must never reach the Claude subprocess."""
    return {'ANTHROPIC_API_KEY': 'sk-ant-api-should-not-leak', 'HA_TOKEN': 'ha-test-token',
            'NOTIFY_TOKEN': 'n' * 40}


os.environ.setdefault('PYTHONDONTWRITEBYTECODE', '1')
