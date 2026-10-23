"""Render broker: draws Home Assistant cards with HA's own frontend.

Runs as the unprivileged ``render`` user, separate from the bot, because it
drives a web browser (headless Chromium, see render/render.mjs). The bot
sends one JSON line (a request checked by ``cards.check_request``) over a
Unix socket and gets back ``{"png": base64}`` or ``{"error": str}``. Only the
broker holds the HA token it logs in with; the browser may only load Home
Assistant's own origin. One render at a time; on Linux the peer UID is
checked with SO_PEERCRED.
"""
from __future__ import annotations
import asyncio
import base64
import json
import logging
import os
import socket
import struct
import sys
import tempfile
from dataclasses import dataclass

from .cards import CardError, check_request

LOG = logging.getLogger('render-broker')
MAX_REQUEST = 64 << 10
MAX_PNG = 12 << 20
SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'render', 'render.mjs')


@dataclass
class RenderBroker:
    ha_url: str
    ha_token: str
    command: tuple[str, ...] = ('node', SCRIPT)
    chromium: str = '/usr/bin/chromium'
    timeout: float = 75
    allowed_uid: int | None = None

    def __post_init__(self):
        self.busy = asyncio.Lock()

    def env(self, home: str) -> dict[str, str]:
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ}
        env.update({'HOME': home, 'TMPDIR': home, 'HA_URL': self.ha_url, 'HA_TOKEN': self.ha_token,
                    'CHROMIUM': self.chromium, 'XDG_CONFIG_HOME': home, 'XDG_CACHE_HOME': home})
        return env

    def peer_allowed(self, writer: asyncio.StreamWriter) -> bool:
        if self.allowed_uid is None:
            return True
        sock = writer.get_extra_info('socket')
        if not hasattr(socket, 'SO_PEERCRED') or sock is None:
            return False
        _pid, uid, _gid = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                                              struct.calcsize('3i')))
        return uid == self.allowed_uid

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not self.peer_allowed(writer):
                LOG.warning('Rejected connection from an unauthorized peer')
                return
            try:
                request = check_request(json.loads(await asyncio.wait_for(reader.readline(), timeout=10)))
            except (ValueError, asyncio.TimeoutError, asyncio.LimitOverrunError) as err:
                return await self.reply(writer, {'error': str(err) if isinstance(err, CardError) else 'bad request'})
            async with self.busy:  # one browser at a time
                await self.reply(writer, await self.render(request))
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def render(self, request: dict) -> dict:
        with tempfile.TemporaryDirectory(prefix='homebot-render-') as home:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self.command, cwd=home, env=self.env(home), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=MAX_PNG * 2)
            except OSError as err:
                LOG.error('Cannot start the renderer: %s', type(err).__name__)
                return {'error': 'renderer unavailable'}
            try:
                out, _ = await asyncio.wait_for(proc.communicate(json.dumps(request).encode()), timeout=self.timeout)
            except asyncio.TimeoutError:
                return {'error': 'rendering took too long'}
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
        try:
            result = json.loads(out.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {'error': 'renderer failed'}
        if isinstance(result, dict) and isinstance(result.get('png'), str):
            try:
                png = base64.b64decode(result['png'], validate=True)
            except ValueError:
                return {'error': 'renderer failed'}
            if png.startswith(b'\x89PNG') and len(png) <= MAX_PNG:
                return {'png': result['png']}
            return {'error': 'renderer failed'}
        error = result.get('error') if isinstance(result, dict) else None
        return {'error': str(error)[:200] if error else 'renderer failed'}

    @staticmethod
    async def reply(writer: asyncio.StreamWriter, obj: dict) -> None:
        writer.write(json.dumps(obj).encode() + b'\n')
        await writer.drain()

    async def serve(self, path: str) -> asyncio.AbstractServer:
        if os.path.exists(path):
            os.unlink(path)
        server = await asyncio.start_unix_server(self.handle, path=path, limit=MAX_REQUEST)
        os.chmod(path, 0o666)  # access: the directory's permissions plus the peer-UID check
        return server


async def amain() -> None:
    env = os.environ
    if os.geteuid() == 0:
        raise SystemExit('The render broker must not run as root')
    allowed = env.get('BROKER_ALLOWED_UID')
    if sys.platform.startswith('linux') and not allowed:
        raise SystemExit('Set BROKER_ALLOWED_UID to the bot user id')
    broker = RenderBroker(ha_url=env['HA_URL'], ha_token=env['HA_TOKEN'],
                          chromium=env.get('CHROMIUM', '/usr/bin/chromium'),
                          allowed_uid=int(allowed) if allowed else None)
    server = await broker.serve(env.get('RENDER_SOCKET', '/run/homebot-render/render.sock'))
    LOG.info('Render broker ready')
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
