"""Claude broker: the only way the bot can run Claude Code.

Runs as the unprivileged ``node`` user and owns the subscription OAuth token;
the bot (``homebot`` user) never holds it and needs no privileges to switch
users. The bot sends one JSON line ``{"prompt": str, "schema": object}`` over a
Unix socket and receives ``{"returncode": int, "stdout": str}`` or
``{"error": "timeout" | "bad_request" | "busy" | "spawn"}``.

The command line is fixed here: no tools, no MCP, no customizations, no
session persistence. The client cannot add flags, choose the binary or set
environment variables. On Linux the peer UID is checked with SO_PEERCRED.
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


def claude_command(claude_bin: str, schema: dict) -> list[str]:
    # --safe-mode: no CLAUDE.md, hooks, skills, plugins or MCP servers.
    # --tools '': no built-in tools. Verified with Claude Code 2.1.306.
    return [claude_bin, '-p', '--safe-mode', '--tools', '',
            '--disallowedTools', 'mcp__*', '--strict-mcp-config',
            '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
            '--no-session-persistence', '--max-turns', '3',
            '--output-format', 'json', '--json-schema', json.dumps(schema)]


@dataclass
class Broker:
    token: str
    claude_home: str
    claude_bin: str = 'claude'
    timeout: float = 150
    allowed_uid: int | None = None  # required on Linux in production

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
            if (not isinstance(request, dict) or set(request) != {'prompt', 'schema'}
                    or not isinstance(request['prompt'], str) or len(request['prompt']) > MAX_PROMPT
                    or not isinstance(request['schema'], dict)):
                return await self.reply(writer, {'error': 'bad_request'})
            if self.busy.locked():
                return await self.reply(writer, {'error': 'busy'})
            async with self.busy:
                await self.reply(writer, await self.run(request['prompt'], request['schema']))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def run(self, prompt: str, schema: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix='homebot-claude-') as work:
            env = {**self.env(), 'TMPDIR': work}
            try:
                proc = await asyncio.create_subprocess_exec(
                    *claude_command(self.claude_bin, schema), cwd=work, env=env,
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
                    allowed_uid=int(allowed) if allowed else None)
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
