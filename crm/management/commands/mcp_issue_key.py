from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from crm.core.bootstrap import MCP_SERVICE_ACCOUNT


class Command(BaseCommand):
    help = 'Issue or revoke a bearer API key for the MCP Business Manager server.'

    def add_arguments(self, parser):
        parser.add_argument('--label', help='A name for this integration, e.g. "Claude Desktop".')
        parser.add_argument('--revoke', type=int, help='McpApiKey id to revoke.')
        parser.add_argument('--list', dest='show_list', action='store_true',
                            help='List all keys (never shows the raw token).')

    def handle(self, *args, label=None, revoke=None, show_list=False, **opts):
        from crm.models import McpApiKey
        from crm.mcp.auth import mint_key
        if show_list:
            for k in McpApiKey.objects.all():
                status = 'revoked' if k.revoked_at else 'active'
                self.stdout.write(f'#{k.pk}  {k.label!r}  {status}  last_used={k.last_used_at}')
            return
        if revoke is not None:
            key = McpApiKey.objects.filter(pk=revoke).first()
            if key is None:
                raise CommandError(f'No key with id {revoke}.')
            from django.utils import timezone
            key.revoked_at = timezone.now()
            key.save(update_fields=['revoked_at'])
            self.stdout.write(self.style.SUCCESS(f'Revoked key #{key.pk} ({key.label}).'))
            return
        if not label:
            raise CommandError('Pass --label "name" to issue a new key, --revoke <id>, or --list.')
        User = get_user_model()
        try:
            owner = User.objects.get(username=MCP_SERVICE_ACCOUNT)
        except User.DoesNotExist:
            raise CommandError('Run `manage.py crm_bootstrap` first to create the service account.')
        actor = User.objects.filter(is_superuser=True).first() or owner
        key, raw = mint_key(owner, label=label, created_by=actor)
        self.stdout.write(self.style.SUCCESS(f'Key #{key.pk} issued. This is shown once - store it now:'))
        self.stdout.write(raw)
