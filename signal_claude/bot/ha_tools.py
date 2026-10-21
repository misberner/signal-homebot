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
    'ha_mcp': ('search_entities', 'get_states', 'get_overview', 'get_history'),
    'hass': ('get_live_context',),
}
MAX_RESULT = 30000
# Module level: tool signatures are resolved from module globals.
EntityIds = Annotated[list[str], Field(min_length=1, max_length=25)]
HistoryIds = Annotated[list[str], Field(min_length=1, max_length=10)]
SearchLimit = Annotated[int, Field(ge=1, le=25)]
HistoryLimit = Annotated[int, Field(ge=1, le=200)]
PREFLIGHT_TTL = 60


class UpstreamError(RuntimeError):
    pass


class HaTools:
    def __init__(self, cfg):
        self.cfg = cfg
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
        async with self.upstream() as (client, names):
            if tool == 'GetLiveContext':
                tool = next(n for n in LIVE_CONTEXT_TOOLS if n in names)
            result = await client.call_tool(tool, args)
            text = '\n'.join(getattr(c, 'text', '') for c in result.content)
            if result.is_error:
                raise UpstreamError(f'{tool} failed: {text[:300]}')
            return text

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
        else:
            @server.tool(name='get_live_context')
            async def get_live_context() -> str:
                """Current state of all entities exposed to Home Assistant's Assist."""
                return result_summary(await tools.call('GetLiveContext', {}))

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


def result_summary(text: str) -> str:
    """Trim a tool result for Claude; pass through compact JSON."""
    try:
        text = json.dumps(json.loads(text), separators=(',', ':'), ensure_ascii=False)
    except ValueError:
        pass
    return text[:MAX_RESULT]
