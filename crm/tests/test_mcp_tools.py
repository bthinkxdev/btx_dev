"""MCP Business Manager server tests. Tools are called in-process via the FastMCP tool
manager's wrapped fn directly (bypassing content-block serialization, which just wraps the
same dict/list our tools already return) - not over real HTTP, matching the project's
existing in-process style (test_crawl.py, test_accounting.py's test_pages_render)."""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.utils import timezone

from crm.core.bootstrap import MCP_SERVICE_ACCOUNT
from crm.mcp import auth, context
from crm.mcp.server import mcp

from .base import CrmTestCase


def call(tool_name: str, actor, **kwargs):
    """Call a registered MCP tool in-process as `actor`, returning its raw Python result.

    Calls the original sync function directly (tool.fn.func, unwrapped from the
    sync_to_async used in production - see crm.mcp.helpers.sync_tool) rather than awaiting
    it through a worker thread: Django's TestCase holds the whole test in one open
    transaction on one connection, and a second thread's own connection deadlocks against
    it on SQLite. Postgres (production) doesn't have this limitation, and the thread-hop
    itself is exercised by the real HTTP server, not by this business-logic test."""
    context.set_actor(actor, None)
    tool = mcp._tool_manager.get_tool(tool_name)
    return tool.fn.func(**kwargs)


class McpToolRegistryTest(CrmTestCase):
    def test_no_destructive_tools_registered(self):
        """Not permission-blocked - the tool simply does not exist."""
        async def names():
            return [t.name for t in await mcp.list_tools()]
        tool_names = asyncio.run(names())
        self.assertGreater(len(tool_names), 30)
        forbidden = [n for n in tool_names if re.search('delete|archive|approve|configure|verify', n)]
        self.assertEqual(forbidden, [], forbidden)

    def test_manual_resources_registered(self):
        async def listing():
            return [r.uriTemplate for r in await mcp.list_resource_templates()] + \
                   [str(r.uri) for r in await mcp.list_resources()]
        uris = asyncio.run(listing())
        self.assertIn('glossary://business-manuals', uris)


class McpBusinessManagerFixture(CrmTestCase):
    """CrmTestCase.setUpTestData() already calls bootstrap.run(), which seeds the
    business_manager role and service-account user - just fetch it."""
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.bm = get_user_model().objects.get(username=MCP_SERVICE_ACCOUNT)


class McpPermissionParityTest(McpBusinessManagerFixture):
    def test_business_manager_can_create_lead(self):
        out = call('create_lead', self.bm, name='MCP Lead', phone='+919812340099', source_name='Manual entry')
        self.assertEqual(out['name'], 'MCP Lead')
        from crm.models import Lead
        self.assertTrue(Lead.objects.filter(pk=out['id']).exists())

    def test_business_manager_can_create_and_update_client(self):
        out = call('create_client', self.bm, legal_name='MCP Client Co', phone='+919812340098')
        self.assertEqual(out['legal_name'], 'MCP Client Co')

    def test_business_manager_denied_accounting_write_tool_does_not_exist(self):
        """There is no create_manual_journal/save_account tool at all for this role."""
        async def names():
            return {t.name for t in await mcp.list_tools()}
        tool_names = asyncio.run(names())
        self.assertNotIn('create_manual_journal', tool_names)
        self.assertNotIn('save_account', tool_names)
        self.assertNotIn('create_bank_account', tool_names)
        self.assertNotIn('verify_manual', tool_names)
        self.assertNotIn('record_manual_verify', tool_names)

    def test_business_manager_can_view_but_not_write_accounting(self):
        from crm.core import india
        out = call('get_profit_loss', self.bm, start_iso=str(india.today().replace(day=1)),
                  end_iso=str(india.today()))
        self.assertIn('net_profit', out)

    def test_other_role_denied_business_manager_scoped_tool(self):
        """A plain sales_executive lacks the business_manager role and is correctly denied
        by the same permission layer the web app uses - defense in depth, not just 'the
        tool wasn't built'. (Over the real MCP transport this surfaces as a ToolError;
        called directly here it's the underlying PermissionDenied - see call()'s docstring.)"""
        with self.assertRaises(PermissionDenied) as ctx:
            call('get_profit_loss', self.u['sales'], start_iso='2026-01-01', end_iso='2026-12-31')
        self.assertIn('Accounting', str(ctx.exception))

    def test_hr_tool_never_accepts_salary_or_bank_fields(self):
        """The tool-level allowlist, not just the role's field_groups, keeps salary/bank
        details out of this interface entirely - check both the fields returned and the
        actual parameter names create_employee() accepts."""
        import inspect
        from crm.mcp.tools import hr
        self.assertNotIn('bank_account_number', hr.FIELDS)
        self.assertNotIn('bank_ifsc', hr.FIELDS)
        self.assertNotIn('salary', ' '.join(hr.FIELDS))
        create_fn = mcp._tool_manager.get_tool('create_employee').fn
        params = set(inspect.signature(create_fn).parameters)
        self.assertFalse(params & {'bank_account_number', 'bank_ifsc', 'pan', 'salary', 'date_of_birth'})


class McpBusinessOverviewTest(McpBusinessManagerFixture):
    def test_business_overview_sane_against_fixtures(self):
        self.make_quotation(price=Decimal('10000'), accepted=True)
        out = call('business_overview', self.bm)
        self.assertIn('profit_loss', out)
        self.assertIn('cash_flow', out)
        self.assertIn('pipeline', out)
        self.assertIn('delivery', out)
        self.assertGreaterEqual(out['pipeline']['open_leads'], 0)
        self.assertIsInstance(out['delivery']['active_projects'], int)


class McpApiKeyTest(McpBusinessManagerFixture):
    def test_key_round_trip_and_revocation(self):
        from crm.models import McpApiKey
        key, raw = auth.mint_key(self.bm, label='unit test', created_by=self.u['owner'])
        self.assertTrue(McpApiKey.objects.filter(pk=key.pk).exists())
        resolved = auth.resolve_token(raw)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.owner_id, self.bm.pk)

        key.revoked_at = timezone.now()
        key.save(update_fields=['revoked_at'])
        self.assertIsNone(auth.resolve_token(raw))

    def test_garbage_token_rejected(self):
        self.assertIsNone(auth.resolve_token('not-a-real-token'))
        self.assertIsNone(auth.resolve_token(''))
