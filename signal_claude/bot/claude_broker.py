"""Claude broker: the only way the bot can run Claude Code.

Runs as the unprivileged ``node`` user and owns the subscription OAuth token;
the bot (``homebot`` user) never holds it and needs no privileges to switch
users. The bot sends one JSON line ``{"prompt": str, "schema": object}`` over a
Unix socket and receives ``{"returncode": int, "stdout": str}`` or
``{"error": "timeout" | "bad_request" | "busy" | "spawn"}``. Optionally the
request carries ``mcp_token`` and ``tools``: Claude then gets exactly those
read tools of the bot's local Home Assistant proxy (``CLAUDE_MCP_URL``).

The command line is fixed here: no built-in tools, no settings, hooks,
plugins or other MCP servers, no session persistence. The client cannot add
flags, choose the binary, the MCP URL or environment variables. On Linux the
peer UID is checked with SO_PEERCRED.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import socket
import struct
import sys
import tempfile
from dataclasses import dataclass

LOG = logging.getLogger('claude-broker')
MAX_REQUEST = 1 << 20   # bytes per request line
MAX_PROMPT = 200_000    # characters
MAX_OUTPUT = 1 << 20    # bytes of Claude stdout returned


TOOL_RE = r'[a-z_]{1,40}'
TOKEN_RE = r'[A-Za-z0-9_-]{20,64}'


def claude_command(claude_bin: str, schema: dict, mcp_config: str | None = None,
                   tools: list[str] = ()) -> list[str]:
    """Verified with Claude Code 2.1.306. --tools '' removes every built-in tool
    (shell, files, web); --setting-sources '' loads no user/project settings
    (hooks, permissions, MCP servers); --strict-mcp-config limits MCP servers
    to --mcp-config. (--safe-mode is not used: it silently drops --mcp-config.)
    In dontAsk mode only the listed MCP tools are usable."""
    cmd = [claude_bin, '-p', '--tools', '', '--setting-sources', '', '--disable-slash-commands',
           '--strict-mcp-config']
    if mcp_config and tools:
        cmd += ['--mcp-config', mcp_config, '--allowedTools', ','.join(f'mcp__ha__{t}' for t in tools)]
    return cmd + ['--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                  '--no-session-persistence', '--max-turns', '12',
                  '--output-format', 'json', '--json-schema', json.dumps(schema)]


@dataclass
class Broker:
    token: str
    claude_home: str
    claude_bin: str = 'claude'
    timeout: float = 150
    allowed_uid: int | None = None  # required on Linux in production
    mcp_url: str = 'http://127.0.0.1:8765/mcp'
    allowed_tools: tuple[str, ...] = ('search_entities', 'get_states', 'get_overview', 'get_history',
                                      'get_live_context')

    def __post_init__(self):
        self.busy = asyncio.Lock()

    def env(self) -> dict[str, str]:
        # Built from scratch: never ANTHROPIC_API_KEY (usage billing) or any
        # other inherited secret.
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'SSL_CERT_FILE') if key in os.environ}
        env.update({
            'HOME': self.claude_home,
            'CLAUDE_CONFIG_DIR': f'{self.claude_home}/config',
            'CLAUDE_CODE_OAUTH_TOKEN': self.token,
            'DISABLE_AUTOUPDATER': '1',
            'CI': '1',
        })
        return env

    def peer_allowed(self, writer: asyncio.StreamWriter) -> bool:
        if self.allowed_uid is None:
            return True
        sock = writer.get_extra_info('socket')
        if not hasattr(socket, 'SO_PEERCRED') or sock is None:
            return False  # fail closed where the peer cannot be identified
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
        _pid, uid, _gid = struct.unpack('3i', creds)
        return uid == self.allowed_uid

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self.peer_allowed(writer):
                LOG.warning('Rejected connection from an unauthorized peer')
                return
            try:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                request = json.loads(line)
            except (ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError):
                return await self.reply(writer, {'error': 'bad_request'})
            if not self.valid_request(request):
                return await self.reply(writer, {'error': 'bad_request'})
            if self.busy.locked():
                return await self.reply(writer, {'error': 'busy'})
            async with self.busy:
                await self.reply(writer, await self.run(request['prompt'], request['schema'],
                                                        request.get('mcp_token'), request.get('tools') or []))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def valid_request(self, request: object) -> bool:
        import re
        if (not isinstance(request, dict) or not {'prompt', 'schema'} <= set(request)
                or set(request) - {'prompt', 'schema', 'mcp_token', 'tools'}):
            return False
        if (not isinstance(request['prompt'], str) or len(request['prompt']) > MAX_PROMPT
                or not isinstance(request['schema'], dict)):
            return False
        token, tools = request.get('mcp_token'), request.get('tools')
        if token is not None and (not isinstance(token, str) or not re.fullmatch(TOKEN_RE, token)):
            return False
        if tools is not None and (not isinstance(tools, list) or len(tools) > 10 or token is None
                                  or not all(isinstance(t, str) and t in self.allowed_tools for t in tools)):
            return False
        return True

    async def run(self, prompt: str, schema: dict, mcp_token: str | None = None,
                  tools: list[str] = ()) -> dict:
        with tempfile.TemporaryDirectory(prefix='homebot-claude-') as work:
            env = {**self.env(), 'TMPDIR': work}
            mcp_config = None
            if mcp_token and tools:
                # A file in the private work dir, not argv: argv is visible to
                # every user in the container.
                mcp_config = os.path.join(work, 'mcp.json')
                with open(mcp_config, 'w') as f:
                    json.dump({'mcpServers': {'ha': {
                        'type': 'http', 'url': self.mcp_url,
                        'headers': {'Authorization': f'Bearer {mcp_token}'}}}}, f)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *claude_command(self.claude_bin, schema, mcp_config, tools), cwd=work, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL)
            except OSError as err:
                LOG.error('Cannot start Claude: %s', type(err).__name__)
                return {'error': 'spawn'}
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return {'error': 'timeout'}
            return {'returncode': proc.returncode,
                    'stdout': stdout[:MAX_OUTPUT].decode('utf-8', errors='replace')}

    @staticmethod
    async def reply(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(json.dumps(obj).encode() + b'\n')
        await writer.drain()

    async def serve(self, path: str) -> asyncio.AbstractServer:
        if os.path.exists(path):
            os.unlink(path)
        server = await asyncio.start_unix_server(self.handle, path=path, limit=MAX_REQUEST)
        # Access is controlled by the socket directory's permissions plus the
        # peer-UID check; the socket file itself must be connectable.
        os.chmod(path, 0o666)
        return server


async def amain() -> None:
    env = os.environ
    token = env.get('CLAUDE_CODE_OAUTH_TOKEN', '')
    if not token:
        raise SystemExit('CLAUDE_CODE_OAUTH_TOKEN is not set')
    if os.geteuid() == 0:
        raise SystemExit('The Claude broker must not run as root')
    allowed = env.get('BROKER_ALLOWED_UID')
    if sys.platform.startswith('linux') and not allowed:
        raise SystemExit('Set BROKER_ALLOWED_UID to the bot user id')
    broker = Broker(token=token, claude_home=env.get('CLAUDE_HOME', '/data/claude'),
                    allowed_uid=int(allowed) if allowed else None,
                    mcp_url=env.get('CLAUDE_MCP_URL', 'http://127.0.0.1:8765/mcp'))
    path = env.get('CLAUDE_SOCKET', '/run/homebot-claude/claude.sock')
    server = await broker.serve(path)
    LOG.info('Claude broker ready')
    async with server:
        await server.serve_forever()


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    os.umask(0o077)
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
