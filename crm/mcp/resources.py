"""Exposes the CRM's own human-written role manuals (docs/crm/user-manuals/*.txt) as MCP
resources, so the connected AI reads the business's own vocabulary instead of us
re-authoring documentation for it."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings

MANUALS_DIR = Path(settings.BASE_DIR) / 'docs' / 'crm' / 'user-manuals'


def _manual_slugs() -> dict:
    """{slug: filename} for every NN_role_name.txt file, slug = role_name without the number."""
    out = {}
    if not MANUALS_DIR.is_dir():
        return out
    for f in sorted(MANUALS_DIR.glob('*.txt')):
        slug = f.stem.split('_', 1)[1] if '_' in f.stem else f.stem
        out[slug] = f.name
    return out


def register(mcp):
    @mcp.resource('glossary://business-manuals')
    def list_manuals() -> str:
        """Index of every available business-manual resource slug."""
        slugs = _manual_slugs()
        return '\n'.join(f'{slug} -> glossary://business-manual/{slug}' for slug in slugs) or 'No manuals found.'

    @mcp.resource('glossary://business-manual/{slug}')
    def read_manual(slug: str) -> str:
        """The full text of one role's user manual, in the CRM's own business vocabulary."""
        filename = _manual_slugs().get(slug)
        if not filename:
            return f'No manual found for "{slug}". Read glossary://business-manuals for the index.'
        return (MANUALS_DIR / filename).read_text(encoding='utf-8')
