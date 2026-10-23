"""Read-only Home Assistant tools for Claude, served as a local MCP proxy.

Claude never talks to Home Assistant directly and never holds HA credentials.
For each request the bot issues a short-lived bearer token; Claude Code
connects to this proxy on 127.0.0.1 with it and sees only a fixed set of read
tools. Every call is forwarded to the upstream MCP server (HA's built-in
server or ha-mcp) with constrained arguments, after checking that the upstream
connection is read-only.
"""
from __future__ import annotations
import asyncio
import base64
import contextlib
import json
import re
import secrets
import time
from typing import Annotated

from pydantic import Field

from .core import ENTITY_RE, ha_mcp_overview_args, render_overview

# Built-in HA mcp_server: older versions expose GetLiveContext, newer ones
# namespace it.
LIVE_CONTEXT_TOOLS = ('homeassistant__GetLiveContext', 'GetLiveContext')
# Pure write tools that ha-mcp hides in Read Only Mode; their presence in the
# catalog means the connection is not read-only.
HA_MCP_WRITE_TOOLS = ('ha_call_service', 'ha_write_file', 'ha_restart', 'ha_bulk_control')
TOOLS = {
    'ha_mcp': ('search_entities', 'get_states', 'get_overview', 'get_history', 'get_dashboard',
               'get_automation_config', 'get_script_config', 'get_image', 'get_calendar_events',
               'get_todo_items', 'get_logbook', 'get_traces', 'read_skill'),
    'hass': ('get_live_context', 'read_skill'),
}
MAX_RESULT = 30000
MAX_CONFIG_RESULT = 80000  # dashboards and automation sources are bigger than states
MAX_IMAGE = 4 << 20
IMAGE_TYPES = {'image/jpeg': 'jpeg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp'}
# HA's image entities publish a short-lived, image-only access link.
IMAGE_PROXY_RE = r'/api/image_proxy/(image\.[a-z0-9_]+)\?token=[A-Za-z0-9]{16,128}'
CONFIG_ID_RE = r'[A-Za-z0-9_.-]{1,100}'
# Module level: tool signatures are resolved from module globals.
EntityIds = Annotated[list[str], Field(min_length=1, max_length=25)]
HistoryIds = Annotated[list[str], Field(min_length=1, max_length=10)]
SearchLimit = Annotated[int, Field(ge=1, le=25)]
HistoryLimit = Annotated[int, Field(ge=1, le=200)]
EventLimit = Annotated[int, Field(ge=1, le=50)]
LogbookHours = Annotated[int, Field(ge=1, le=168)]
LogbookLimit = Annotated[int, Field(ge=1, le=100)]
TraceLimit = Annotated[int, Field(ge=1, le=20)]
ISO_TIME_RE = r'\d{4}-\d\d-\d\d([T ][\d:.+Z-]{0,20})?'
PREFLIGHT_TTL = 60


class UpstreamError(RuntimeError):
    pass


class HaTools:
    def __init__(self, cfg, skills=lambda: []):
        self.cfg = cfg
        self.skills = skills  # the household's skills (admin-written, trusted)
        self.tokens: dict[str, float] = {}
        self._preflight_ok_until = 0.0

    @property
    def tool_names(self) -> tuple[str, ...]:
        return TOOLS[self.cfg.mcp_kind]

    # ---------------------------------------------------------------- tokens

    def issue_token(self, ttl: float) -> str:
        now = time.monotonic()
        self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
        token = secrets.token_urlsafe(24)
        self.tokens[token] = now + ttl
        return token

    def revoke(self, token: str) -> None:
        self.tokens.pop(token, None)

    def token_valid(self, header: bytes | None) -> bool:
        if not header or not header.startswith(b'Bearer '):
            return False
        token = header[7:].decode(errors='replace')
        expiry = next((exp for t, exp in self.tokens.items() if secrets.compare_digest(t, token)), None)
        return expiry is not None and expiry > time.monotonic()

    # ---------------------------------------------------------------- upstream

    @contextlib.asynccontextmanager
    async def upstream(self):
        """A checked upstream MCP session (read-only for ha-mcp)."""
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        headers = {'Authorization': f'Bearer {self.cfg.ha_token}'} if self.cfg.mcp_kind == 'hass' else {}
        async with httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(35, read=35),
                                      follow_redirects=False) as http:
            async with Client(streamable_http_client(self.cfg.mcp_url, http_client=http)) as client:
                names = {t.name for t in (await client.list_tools()).tools}
                if self.cfg.mcp_kind == 'ha_mcp':
                    if names & set(HA_MCP_WRITE_TOOLS):
                        raise UpstreamError('ha-mcp connection is not in Read Only Mode')
                    if 'ha_get_overview' not in names:
                        raise UpstreamError('ha-mcp does not offer ha_get_overview')
                elif len(names & set(LIVE_CONTEXT_TOOLS)) != 1:
                    raise UpstreamError('HA MCP does not offer exactly one GetLiveContext tool')
                yield client, names

    async def preflight(self) -> None:
        """Fail closed before involving Claude if HA is unreachable or writable."""
        if time.monotonic() < self._preflight_ok_until:
            return
        async with self.upstream():
            pass
        self._preflight_ok_until = time.monotonic() + PREFLIGHT_TTL

    async def call(self, tool: str, args: dict) -> str:
        return (await self.call_content(tool, args))[0]

    async def call_content(self, tool: str, args: dict) -> tuple[str, list]:
        """(text, images) of an upstream tool result; images as MCP Image helpers.
        These are reads, so a dropped connection is retried once."""
        try:
            return await self._call_once(tool, args)
        except UpstreamError:
            raise
        except Exception:
            return await self._call_once(tool, args)

    async def _call_once(self, tool: str, args: dict) -> tuple[str, list]:
        from mcp.server.mcpserver import Image
        async with self.upstream() as (client, names):
            if tool == 'GetLiveContext':
                tool = next(n for n in LIVE_CONTEXT_TOOLS if n in names)
            result = await client.call_tool(tool, args)
            text = '\n'.join(getattr(c, 'text', '') for c in result.content)
            if result.is_error:
                raise UpstreamError(f'{tool} failed: {text[:300]}')
            images = []
            for c in result.content:
                fmt = IMAGE_TYPES.get(getattr(c, 'mime_type', None) or '')
                if getattr(c, 'type', '') == 'image' and fmt:
                    data = base64.b64decode(c.data)
                    if len(data) <= MAX_IMAGE:
                        images.append(Image(data=data, format=fmt))
            return text, images

    async def image(self, entity_id: str):
        """The current picture of an image entity, fetched through HA's image
        proxy with the entity's own short-lived access token (no HA credentials)."""
        import httpx2
        from mcp.server.mcpserver import Image
        state = json.loads(await self.call('ha_get_state', {'entity_id': [entity_id],
                                                            'fields': ['entity_id', 'attributes']}))
        states = (state.get('data') or state).get('states') or {}
        picture = ((states.get(entity_id) or {}).get('attributes') or {}).get('entity_picture')
        match = re.fullmatch(IMAGE_PROXY_RE, picture) if isinstance(picture, str) else None
        if not match or match.group(1) != entity_id:
            raise UpstreamError(f'{entity_id} has no picture')
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(15), follow_redirects=False) as http:
            async with http.stream('GET', self.cfg.ha_url + picture) as r:
                if r.status_code != 200:
                    raise UpstreamError(f'Home Assistant returned HTTP {r.status_code}')
                fmt = IMAGE_TYPES.get(r.headers.get('content-type', '').split(';')[0].strip().lower())
                if not fmt:
                    raise UpstreamError('not a JPEG, PNG, GIF or WebP image')
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_IMAGE:
                        raise UpstreamError('image larger than 4 MB')
        return Image(data=bytes(data), format=fmt)

    async def picture(self, entity_id: str) -> bytes:
        """The current picture of an image.* or camera.* entity, or a screenshot of
        "dashboard:<url_path>[/<view_path>]" (to send to the chat)."""
        if self.cfg.mcp_kind != 'ha_mcp':
            raise UpstreamError('pictures need ha-mcp')
        if m := re.fullmatch(r'dashboard:([a-z0-9_-]{1,64})(?:/([A-Za-z0-9_-]{1,64}))?', entity_id):
            args = {'url_path': m.group(1), 'include_screenshot': True}
            if m.group(2):
                args['view_path'] = m.group(2)
            text, images = await self.call_content('ha_config_get_dashboard', args)
            if not images:
                raise UpstreamError('screenshots are switched off in ha-mcp' if 'screenshot mode is disabled'
                                    in text else 'no screenshot')
            return images[0].data
        if re.fullmatch(r'camera\.[a-z0-9_]+', entity_id):
            _, images = await self.call_content('ha_get_camera_image', {'entity_id': entity_id})
            if not images:
                raise UpstreamError('no snapshot')
            return images[0].data
        if re.fullmatch(r'image\.[a-z0-9_]+', entity_id):
            return (await self.image(entity_id)).data
        raise UpstreamError('not an image.* or camera.* entity')

    async def chart_series(self, entity_ids: list[str], hours: int) -> list:
        """Data for a chart: long-term statistics where HA keeps them, else the
        raw state history (e.g. on/off sensors). Read-only ha-mcp calls."""
        from .charts import history_series, period_for, statistics_series
        span = f'{hours // 24}d' if hours >= 48 and hours % 24 == 0 else f'{hours}h'
        state = json.loads(await self.call('ha_get_state', {
            'entity_id': entity_ids, 'fields': ['entity_id', 'attributes'],
            'attribute_keys': ['friendly_name', 'unit_of_measurement']}))
        states = (state.get('data') or state).get('states') or {}
        meta = {eid: {'label': str(attrs.get('friendly_name') or eid)[:60],
                      'unit': str(attrs.get('unit_of_measurement') or '')[:20]}
                for eid, attrs in ((e, (states.get(e) or {}).get('attributes') or {}) for e in entity_ids)}
        found = statistics_series(await self.call('ha_get_history', {
            'entity_ids': entity_ids, 'source': 'statistics', 'start_time': span,
            'period': period_for(hours), 'limit': 1000}), meta)
        missing = [e for e in entity_ids if e not in found]
        if missing:
            found.update(history_series(await self.call('ha_get_history', {
                'entity_ids': missing, 'source': 'history', 'start_time': span, 'limit': 1000,
                'minimal_response': True}), meta))
        return [found[e] for e in entity_ids if e in found]

    # ---------------------------------------------------------------- MCP server

    def asgi_app(self):
        from mcp.server import MCPServer
        server = MCPServer('home-assistant-read-only')
        tools = self

        def check_entities(ids: list[str]) -> list[str]:
            bad = [e for e in ids if not re.fullmatch(ENTITY_RE, e)]
            if bad:
                raise ValueError(f'invalid entity ids: {bad[:3]}')
            return ids

        if self.cfg.mcp_kind == 'ha_mcp':
            @server.tool(name='search_entities')
            async def search_entities(query: str | None = None, domain: str | None = None,
                                      area: str | None = None,
                                      limit: SearchLimit = 15) -> str:
                """Find Home Assistant entities by name, domain (e.g. light, sensor) and/or area.
                Returns entity_id, friendly name, domain, area and current state."""
                return result_summary(await tools.call('ha_search', {
                    'query': query, 'domain_filter': domain, 'area_filter': area, 'limit': limit,
                    'exact_match': False,
                    'result_fields': ['entity_id', 'friendly_name', 'domain', 'state', 'area']}))

            @server.tool(name='get_states')
            async def get_states(entity_ids: EntityIds) -> str:
                """Current state and attributes (e.g. temperature, brightness) of up to 25 entities."""
                return result_summary(await tools.call('ha_get_state', {
                    'entity_id': check_entities(entity_ids),
                    'fields': ['entity_id', 'state', 'attributes', 'last_changed']}))

            @server.tool(name='get_overview')
            async def get_overview(domains: list[str] | None = None) -> str:
                """States of all entities in the given domains (default: the configured ones)."""
                doms = domains or list(tools.cfg.mcp_domains)
                if not all(re.fullmatch(r'[a-z_]+', d) for d in doms) or len(doms) > 10:
                    raise ValueError('domains must be up to 10 HA domain names')
                return render_overview(await tools.call('ha_get_overview', ha_mcp_overview_args(doms)))

            @server.tool(name='get_history')
            async def get_history(entity_ids: HistoryIds, start_time: str = '24h',
                                  limit: HistoryLimit = 50) -> str:
                """State history of up to 10 entities. start_time is relative ('6h', '7d')
                or an ISO datetime; newest changes first."""
                if not re.fullmatch(r'\d{1,3}[hd]|\d{4}-\d\d-\d\d[T ][\d:.+Z-]{0,20}', start_time):
                    raise ValueError("start_time must look like '24h', '7d' or an ISO datetime")
                return result_summary(await tools.call('ha_get_history', {
                    'entity_ids': check_entities(entity_ids), 'start_time': start_time,
                    'limit': limit, 'minimal_response': True}))

            @server.tool(name='get_dashboard')
            async def get_dashboard(url_path: str | None = None, list_only: bool = False,
                                    view_path: str | None = None, entity_id: str | None = None,
                                    card_type: str | None = None, heading: str | None = None,
                                    query: str | None = None, include_config: bool = False,
                                    include_screenshot: bool = False):
                """Lovelace dashboards. list_only=true lists them; url_path (e.g. 'dashboard-heating',
                'default' for the main one) reads one dashboard's config, optionally only view_path.
                entity_id ('*' wildcard), card_type, heading or query search cards (include_config adds
                each card's config). include_screenshot also returns rendered images if HA supports it."""
                args = {'list_only': list_only, 'include_config': include_config,
                        'include_screenshot': include_screenshot}
                for key, value, pattern in (('url_path', url_path, r'[a-z0-9_-]{1,64}'),
                                            ('view_path', view_path, r'[A-Za-z0-9_-]{1,64}'),
                                            ('entity_id', entity_id, r'[a-z_*]+\.[a-z0-9_*]+'),
                                            ('card_type', card_type, r'[A-Za-z0-9_:-]{1,64}')):
                    if value is not None:
                        if not re.fullmatch(pattern, value):
                            raise ValueError(f'invalid {key}')
                        args[key] = value
                for key, value in (('heading', heading), ('query', query)):
                    if value is not None:
                        if not 0 < len(value) <= 100:
                            raise ValueError(f'{key} must be 1-100 characters')
                        args[key] = value
                text, images = await tools.call_content('ha_config_get_dashboard', args)
                return [result_summary(text, MAX_CONFIG_RESULT), *images[:3]] if images \
                    else result_summary(text, MAX_CONFIG_RESULT)

            @server.tool(name='get_automation_config')
            async def get_automation_config(automation_id: str) -> str:
                """Source of an automation (triggers, conditions, actions) by entity_id
                (automation.xyz) or unique id."""
                if not re.fullmatch(CONFIG_ID_RE, automation_id):
                    raise ValueError('invalid automation id')
                return result_summary(await tools.call('ha_config_get_automation',
                                                       {'identifier': automation_id}), MAX_CONFIG_RESULT)

            @server.tool(name='get_script_config')
            async def get_script_config(script_id: str) -> str:
                """Source of a script (sequence, fields, mode) by entity_id (script.xyz) or id."""
                if not re.fullmatch(CONFIG_ID_RE, script_id):
                    raise ValueError('invalid script id')
                return result_summary(await tools.call('ha_config_get_script',
                                                       {'script_id': script_id}), MAX_CONFIG_RESULT)

            @server.tool(name='get_image')
            async def get_image(entity_id: str):
                """The current picture of an image.* entity (e.g. a vacuum map) or a snapshot of a
                camera.* entity, to look at."""
                if re.fullmatch(r'camera\.[a-z0-9_]+', entity_id):
                    text, images = await tools.call_content('ha_get_camera_image', {'entity_id': entity_id})
                    if not images:
                        raise UpstreamError(f'no snapshot (larger than 4 MB or not an image): {text[:200]}')
                    return [result_summary(text, 500), images[0]]
                if not re.fullmatch(r'image\.[a-z0-9_]+', entity_id):
                    raise ValueError('entity_id must be an image.* or camera.* entity')
                return await tools.image(entity_id)

            @server.tool(name='get_calendar_events')
            async def get_calendar_events(entity_id: str, start: str | None = None, end: str | None = None,
                                          max_results: EventLimit = 20) -> str:
                """Events of a calendar.* entity (default: the next 7 days). start/end are ISO
                datetimes; pass end whenever you pass start."""
                if not re.fullmatch(r'calendar\.[a-z0-9_]+', entity_id):
                    raise ValueError('entity_id must be a calendar.* entity')
                args = {'entity_id': entity_id, 'max_results': max_results}
                for key, value in (('start', start), ('end', end)):
                    if value is not None:
                        if not re.fullmatch(ISO_TIME_RE, value):
                            raise ValueError(f'{key} must be an ISO datetime')
                        args[key] = value
                return result_summary(await tools.call('ha_config_get_calendar_events', args))

            @server.tool(name='get_todo_items')
            async def get_todo_items(entity_id: str | None = None, status: str | None = None) -> str:
                """Without entity_id: all todo lists (e.g. shopping lists). With a todo.* entity:
                its items; status 'needs_action' (open) or 'completed' filters them."""
                args = {}
                if entity_id is not None:
                    if not re.fullmatch(r'todo\.[a-z0-9_]+', entity_id):
                        raise ValueError('entity_id must be a todo.* entity')
                    args['entity_id'] = entity_id
                if status is not None:
                    if status not in ('needs_action', 'completed'):
                        raise ValueError("status must be 'needs_action' or 'completed'")
                    args['status'] = status
                return result_summary(await tools.call('ha_get_todo', args))

            @server.tool(name='get_logbook')
            async def get_logbook(entity_id: str | None = None, hours_back: LogbookHours = 24,
                                  limit: LogbookLimit = 50) -> str:
                """The logbook (what happened, and what triggered it), newest first, optionally
                for one entity, up to 7 days back."""
                args = {'source': 'logbook', 'hours_back': hours_back, 'limit': limit}
                if entity_id is not None:
                    args['entity_id'] = check_entities([entity_id])[0]
                return result_summary(await tools.call('ha_get_logs', args))

            @server.tool(name='get_traces')
            async def get_traces(automation_id: str, run_id: str | None = None, limit: TraceLimit = 5) -> str:
                """Recent runs of an automation.* or script.* entity: what triggered them, which
                conditions passed, which actions ran. run_id gives one run in detail."""
                if not re.fullmatch(r'(automation|script)\.[a-z0-9_]+', automation_id):
                    raise ValueError('automation_id must be an automation.* or script.* entity')
                args = {'automation_id': automation_id, 'limit': limit}
                if run_id is not None:
                    if not re.fullmatch(r'[0-9A-Za-z._-]{1,64}', run_id):
                        raise ValueError('invalid run_id')
                    args['run_id'] = run_id
                return result_summary(await tools.call('ha_get_automation_traces', args), MAX_CONFIG_RESULT)
        else:
            @server.tool(name='get_live_context')
            async def get_live_context() -> str:
                """Current state of all entities exposed to Home Assistant's Assist."""
                return result_summary(await tools.call('GetLiveContext', {}))

        @server.tool(name='read_skill')
        async def read_skill(name: str) -> str:
            """Read one of the household's skills (the admin's instructions for a topic) by name."""
            for skill in tools.skills():
                if skill['name'] == name:
                    return skill['body']
            raise ValueError(f'no skill named {name!r}')

        app = server.streamable_http_app(streamable_http_path='/mcp', stateless_http=True)

        async def guarded(scope, receive, send):
            if scope['type'] == 'http':
                if not tools.token_valid(dict(scope['headers']).get(b'authorization')):
                    await send({'type': 'http.response.start', 'status': 401, 'headers': []})
                    await send({'type': 'http.response.body', 'body': b''})
                    return
            await app(scope, receive, send)
        return guarded

    async def serve(self, port: int) -> None:
        """Run the proxy on 127.0.0.1:port inside the bot's event loop."""
        import uvicorn

        class Server(uvicorn.Server):
            # The bot owns SIGTERM/SIGINT handling.
            def capture_signals(self):
                return contextlib.nullcontext()

        server = Server(uvicorn.Config(self.asgi_app(), host='127.0.0.1', port=port,
                                       log_level='warning', lifespan='on'))
        try:
            await server.serve()
        except asyncio.CancelledError:
            # uvicorn skips its shutdown when cancelled; close the socket properly.
            server.should_exit = True
            await server.shutdown()
            raise


def result_summary(text: str, limit: int = MAX_RESULT) -> str:
    """Trim a tool result for Claude; pass through compact JSON."""
    try:
        text = json.dumps(json.loads(text), separators=(',', ':'), ensure_ascii=False)
    except ValueError:
        pass
    return text[:limit]
