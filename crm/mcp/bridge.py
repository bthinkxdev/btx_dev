"""Serve the MCP app from inside the Django site, so the connector needs no extra process or web-server rule.

The MCP app is ASGI; the site may run under a synchronous WSGI server. Each worker process lazily
starts one background thread with its own event loop that runs the app (stateless JSON mode, so
every request is self-contained and nothing streams). `handle()` turns a Django request into an
ASGI call on that loop and returns the app's answer as a Django response.
"""
from __future__ import annotations

import asyncio
import threading

from django.http import HttpResponse, JsonResponse

TIMEOUT = 90

_lock = threading.Lock()
_runner = None


class _Runner:
    def __init__(self):
        from .server import app, inner
        self.app, self.inner = app, inner
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.failed: BaseException | None = None
        threading.Thread(target=self._run, name='mcp-bridge', daemon=True).start()
        if not self.ready.wait(30) or self.failed:
            raise RuntimeError(f'MCP bridge failed to start: {self.failed!r}')

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._serve())

    async def _serve(self):
        try:
            async with self.inner.router.lifespan_context(self.inner):    # starts the MCP session manager
                self.ready.set()
                await asyncio.Event().wait()                               # run until the process exits
        except BaseException as exc:                                       # noqa: BLE001 - surfaced in __init__
            self.failed = exc
            self.ready.set()

    async def call(self, scope, body: bytes):
        from asgiref.sync import sync_to_async
        from django.db import close_old_connections
        await sync_to_async(close_old_connections, thread_sensitive=True)()
        sent_body = False
        done = asyncio.Event()
        out = {'status': 500, 'headers': [], 'body': bytearray()}

        async def receive():
            nonlocal sent_body
            if not sent_body:
                sent_body = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await done.wait()
            return {'type': 'http.disconnect'}

        async def send(message):
            if message['type'] == 'http.response.start':
                out['status'], out['headers'] = message['status'], message.get('headers', [])
            elif message['type'] == 'http.response.body':
                out['body'] += message.get('body', b'')
                if not message.get('more_body'):
                    done.set()

        try:
            await self.app(scope, receive, send)
        finally:
            done.set()
            await sync_to_async(close_old_connections, thread_sensitive=True)()
        return out


def _get_runner() -> _Runner:
    global _runner
    with _lock:
        if _runner is None:
            _runner = _Runner()
        return _runner


def handle(request, path: str | None = None) -> HttpResponse:
    """Run `request` through the MCP app. `path` overrides the URL path the app sees (alias routes)."""
    path = path or request.path
    scope = {
        'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1', 'method': request.method,
        'scheme': 'https' if request.is_secure() else 'http', 'path': path, 'raw_path': path.encode(),
        'root_path': '', 'query_string': request.META.get('QUERY_STRING', '').encode('latin-1'),
        'headers': [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in request.headers.items()],
        'client': (request.META.get('REMOTE_ADDR', ''), 0), 'server': (request.get_host().split(':')[0], 443),
    }
    try:
        runner = _get_runner()
    except ImportError as exc:      # the mcp package is not installed in this environment
        return JsonResponse({'error': 'mcp_not_installed', 'detail': f'Run: pip install -r requirements.txt ({exc})'}, status=503)
    future = asyncio.run_coroutine_threadsafe(runner.call(scope, request.body), runner.loop)
    out = future.result(timeout=TIMEOUT)
    response = HttpResponse(bytes(out['body']), status=out['status'])
    for key, value in out['headers']:
        name = key.decode('latin-1')
        if name.lower() not in ('content-length', 'transfer-encoding', 'connection'):
            response[name] = value.decode('latin-1')
    return response
