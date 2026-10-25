"""Runs the MCP Business Manager server as its own standalone process, separate from the
main WSGI site (config/wsgi.py). Put a TLS-terminating reverse proxy in front of this in
production - this command binds plain HTTP only."""
from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Run the remote MCP Business Manager server (Streamable HTTP).'

    def add_arguments(self, parser):
        parser.add_argument('--host', default=getattr(settings, 'MCP_HTTP_HOST', '127.0.0.1'))
        parser.add_argument('--port', type=int, default=getattr(settings, 'MCP_HTTP_PORT', 8800))

    def handle(self, *args, host, port, **opts):
        import uvicorn

        from crm.mcp.server import app
        self.stdout.write(self.style.SUCCESS(
            f'MCP Business Manager server listening on http://{host}:{port}'
            f'{getattr(settings, "MCP_HTTP_PATH", "/mcp")} - put this behind TLS before exposing it remotely.'))
        uvicorn.run(app, host=host, port=port, log_level='info')
