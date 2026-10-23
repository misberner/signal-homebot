"""Claude broker: the only way the bot can run Claude Code.

Runs as the unprivileged ``node`` user and owns the subscription OAuth token;
the bot (``homebot`` user) never holds it and needs no privileges to switch
users. The bot sends one JSON line ``{"prompt": str, "schema": object}`` over a
Unix socket. While Claude works, the broker sends progress lines
``{"progress": {"tool": name, "detail": str}}`` (one per allowed tool call);
the last line is ``{"returncode": int, "stdout": <final result JSON>}`` or
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
import shutil
import socket
import struct
import sys
import tempfile
from dataclasses import dataclass

LOG = logging.getLogger('claude-broker')
MAX_REQUEST = 40 << 20  # bytes per request line (photos included)
IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp')
MAX_IMAGES = 4
MAX_IMAGE_B64 = 7 << 20  # 5 MB images, base64-encoded
MAX_PROMPT = 200_000    # characters
MAX_OUTPUT = 1 << 20    # bytes of Claude stdout returned
MAX_LINE = 8 << 20      # bytes per stream-json line (one tool result)
MAX_STREAM = 64 << 20   # bytes of stream-json read per run


TOOL_RE = r'[a-z_]{1,40}'
TOKEN_RE = r'[A-Za-z0-9_-]{20,64}'


MODELS = ('opus', 'fable', 'sonnet', 'haiku')


def claude_command(claude_bin: str, schema: dict, mcp_config: str | None = None,
                   tools: list[str] = (), system: str = '', model: str = '',
                   stream_input: bool = False) -> list[str]:
    """Verified with Claude Code 2.1.306. --tools '' removes every built-in tool
    (shell, files, web); --setting-sources '' loads no user/project settings
    (hooks, permissions, MCP servers); --strict-mcp-config limits MCP servers
    to --mcp-config. (--safe-mode is not used: it silently drops --mcp-config.)
    In dontAsk mode only the listed MCP tools are usable."""
    cmd = [claude_bin, '-p', '--tools', '', '--setting-sources', '', '--disable-slash-commands',
           '--strict-mcp-config']
    if mcp_config and tools:
        cmd += ['--mcp-config', mcp_config, '--allowedTools', ','.join(f'mcp__ha__{t}' for t in tools)]
    if system:
        cmd += ['--append-system-prompt', system]
    if model:
        cmd += ['--model', model]
    if stream_input:  # the prompt arrives as one stream-json user message (text + photos)
        cmd += ['--input-format', 'stream-json']
    # stream-json (needs --verbose) reports tool calls as they happen; its final
    # "result" event is the same envelope as --output-format json.
    return cmd + ['--permission-mode', 'dontAsk', '--permission-prompts', 'none',
                  '--no-session-persistence', '--max-turns', '12',
                  '--output-format', 'stream-json', '--verbose', '--json-schema', json.dumps(schema)]


def progress_detail(args: object) -> str:
    """A short, plain summary of a tool call's arguments for the status message."""
    if not isinstance(args, dict):
        return ''
    for key in ('query', 'entity_ids', 'domains', 'entity_id', 'url_path', 'automation_id', 'script_id', 'name'):
        value = args.get(key)
        if isinstance(value, list) and value:
            first = str(value[0])[:40]
            return first if len(value) == 1 else f'{first} +{len(value) - 1}'
        if isinstance(value, str) and value:
            return value[:40]
    return ''


@dataclass
class Broker:
    token: str
    claude_home: str
    claude_bin: str = 'claude'
    timeout: float = 150
    allowed_uid: int | None = None  # required on Linux in production
    mcp_url: str = 'http://127.0.0.1:8765/mcp'
    allowed_tools: tuple[str, ...] = ('search_entities', 'get_states', 'get_overview', 'get_history',
                                      'get_dashboard', 'get_automation_config', 'get_script_config',
                                      'get_image', 'get_calendar_events', 'get_todo_items',
                                      'get_logbook', 'get_traces', 'get_live_context', 'read_skill')

    def __post_init__(self):
        self.busy = asyncio.Lock()

    def env(self) -> dict[str, str]:
        # Built from scratch: never ANTHROPIC_API_KEY (usage billing) or any
        # other inherited secret.
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ', 'SSL_CERT_FILE') if key in os.environ}
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
                async def progress(event: dict) -> None:
                    await self.reply(writer, {'progress': event})
                await self.reply(writer, await self.run(request['prompt'], request['schema'],
                                                        request.get('mcp_token'), request.get('tools') or [],
                                                        request.get('system', ''), request.get('model', ''),
                                                        progress, request.get('images') or []))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def valid_request(self, request: object) -> bool:
        import re
        if (not isinstance(request, dict) or not {'prompt', 'schema'} <= set(request)
                or set(request) - {'prompt', 'schema', 'mcp_token', 'tools', 'system', 'model', 'images'}):
            return False
        images = request.get('images', [])
        if not isinstance(images, list) or len(images) > MAX_IMAGES or not all(
                isinstance(i, dict) and set(i) == {'media_type', 'data'} and i['media_type'] in IMAGE_TYPES
                and isinstance(i['data'], str) and 0 < len(i['data']) <= MAX_IMAGE_B64 for i in images):
            return False
        if (not isinstance(request['prompt'], str) or len(request['prompt']) > MAX_PROMPT
                or not isinstance(request['schema'], dict)):
            return False
        system = request.get('system', '')
        if not isinstance(system, str) or len(system) > 20_000:
            return False
        if request.get('model', '') not in ('', *MODELS):
            return False
        token, tools = request.get('mcp_token'), request.get('tools')
        if token is not None and (not isinstance(token, str) or not re.fullmatch(TOKEN_RE, token)):
            return False
        if tools is not None and (not isinstance(tools, list) or len(tools) > len(self.allowed_tools) or token is None
                                  or not all(isinstance(t, str) and t in self.allowed_tools for t in tools)):
            return False
        return True

    async def run(self, prompt: str, schema: dict, mcp_token: str | None = None,
                  tools: list[str] = (), system: str = '', model: str = '', progress=None,
                  images: list[dict] = ()) -> dict:
        # A fixed, emptied working directory: Claude Code puts the cwd into its
        # system prompt, so a random one per request defeats prompt caching
        # (measured: 1.1k vs 2.8k cached tokens). Requests are serialized.
        work = os.path.join(self.claude_home, 'work')
        try:
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work, mode=0o700)
        except OSError as err:
            LOG.error('Cannot prepare the work directory: %s', type(err).__name__)
            return {'error': 'spawn'}
        with tempfile.TemporaryDirectory(prefix='homebot-mcp-') as private:
            env = {**self.env(), 'TMPDIR': private}
            mcp_config = None
            if mcp_token and tools:
                # A private file, not argv: argv is visible to every user in the container.
                mcp_config = os.path.join(private, 'mcp.json')
                with open(mcp_config, 'w') as f:
                    json.dump({'mcpServers': {'ha': {
                        'type': 'http', 'url': self.mcp_url,
                        'headers': {'Authorization': f'Bearer {mcp_token}'}}}}, f)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *claude_command(self.claude_bin, schema, mcp_config, tools, system, model, bool(images)),
                    cwd=work, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    # stream-json puts each tool result on one line; HA reads
                    # easily exceed asyncio's default 64 KiB line limit.
                    limit=MAX_LINE)
            except OSError as err:
                LOG.error('Cannot start Claude: %s', type(err).__name__)
                return {'error': 'spawn'}
            try:
                result = await asyncio.wait_for(self.collect(proc, prompt, set(tools), progress, images),
                                                timeout=self.timeout)
            except asyncio.TimeoutError:
                return {'error': 'timeout'}
            except ValueError:  # a line longer than MAX_LINE
                LOG.warning('Claude output line too long')
                return {'error': 'output'}
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            return {'returncode': proc.returncode, 'stdout': result}

    async def collect(self, proc, prompt: str, tools: set[str], progress, images: list[dict] = ()) -> str:
        """Feed the prompt, relay allowed tool calls as progress, return the
        final "result" event (or '' if Claude produced none)."""
        if images:
            content = [{'type': 'text', 'text': prompt}] + [
                {'type': 'image', 'source': {'type': 'base64', **image}} for image in images]
            proc.stdin.write(json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}})
                             .encode() + b'\n')
        else:
            proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        result, read = '', 0
        while line := await proc.stdout.readline():
            read += len(line)
            if read > MAX_STREAM:
                proc.kill()
                break
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'result':
                result = line[:MAX_OUTPUT].decode('utf-8', errors='replace')
            elif event.get('type') == 'assistant' and progress is not None:
                for block in (event.get('message') or {}).get('content') or []:
                    name = str(block.get('name', '')) if isinstance(block, dict) else ''
                    if block.get('type') == 'tool_use' and name.removeprefix('mcp__ha__') in tools:
                        await progress({'tool': name.removeprefix('mcp__ha__'),
                                        'detail': progress_detail(block.get('input'))})
        await proc.wait()
        return result

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
