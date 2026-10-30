"""MCP connector sign-in: OAuth tokens, and the owner consent page claude.ai sends people to."""
from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from django.urls import reverse
from django.utils import timezone
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from crm.mcp import auth, oauth
from crm.models import McpOAuthRequest, McpOAuthToken

from .test_mcp_tools import McpBusinessManagerFixture

CALLBACK = 'https://claude.ai/api/mcp/auth_callback'


class OAuthFlowTests(McpBusinessManagerFixture):
    def setUp(self):
        super().setUp()
        self.info = OAuthClientInformationFull(client_id='cid-1', client_name='Claude', redirect_uris=[CALLBACK],
                                               token_endpoint_auth_method='none')
        oauth.save_client(self.info)

    def pending(self, state='xyz'):
        url = oauth.park_request(self.info, AuthorizationParams(
            state=state, scopes=None, code_challenge='c' * 43, redirect_uri=CALLBACK, redirect_uri_provided_explicitly=True))
        return McpOAuthRequest.objects.get(pk=url.rstrip('/').split('/')[-1])

    def tokens(self):
        code = oauth.issue_code(self.pending(), self.u['owner'])
        return oauth.exchange_code('cid-1', code)

    def test_authorize_points_at_the_crm_consent_page(self):
        req = self.pending()
        self.assertEqual(req.params['scopes'], ['mcp'])
        self.assertTrue(req.is_open())

    def test_code_is_single_use(self):
        code = oauth.issue_code(self.pending(), self.u['owner'])
        self.assertIsNotNone(oauth.load_code('cid-1', code))
        oauth.exchange_code('cid-1', code)
        self.assertIsNone(oauth.load_code('cid-1', code))
        with self.assertRaises(TokenError):
            oauth.exchange_code('cid-1', code)

    def test_code_is_bound_to_its_client(self):
        code = oauth.issue_code(self.pending(), self.u['owner'])
        self.assertIsNone(oauth.load_code('someone-else', code))

    def test_access_token_resolves_to_the_service_account(self):
        t = self.tokens()
        actor = oauth.resolve_actor(t.access_token)
        self.assertEqual(actor[0].pk, self.bm.pk)
        self.assertEqual(oauth.verify(t.access_token).scopes, ['mcp'])
        self.assertIsNone(oauth.resolve_actor('nope'))
        self.assertIsNone(oauth.resolve_actor(t.refresh_token))      # a refresh token is not an access token

    def test_expired_and_revoked_tokens_stop_working(self):
        t = self.tokens()
        McpOAuthToken.objects.filter(kind='access').update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(oauth.verify(t.access_token))
        t = self.tokens()
        oauth.revoke_family(t.refresh_token)                          # revoking one revokes the pair
        self.assertIsNone(oauth.verify(t.access_token))
        self.assertIsNone(oauth.load_refresh('cid-1', t.refresh_token))

    def test_refresh_rotates_and_cannot_widen_scope(self):
        t = self.tokens()
        with self.assertRaises(TokenError):
            oauth.exchange_refresh('cid-1', t.refresh_token, ['mcp', 'admin'])
        new = oauth.exchange_refresh('cid-1', t.refresh_token, [])
        self.assertNotEqual(new.access_token, t.access_token)
        self.assertIsNone(oauth.verify(t.access_token))               # old pair is gone
        with self.assertRaises(TokenError):
            oauth.exchange_refresh('cid-1', t.refresh_token, [])      # old refresh token cannot be replayed
        self.assertIsNotNone(oauth.verify(new.access_token))

    def test_secrets_are_stored_hashed(self):
        t = self.tokens()
        stored = list(McpOAuthToken.objects.values_list('token_hash', flat=True))
        self.assertNotIn(t.access_token, stored)
        self.assertNotIn(t.refresh_token, stored)

    def test_static_keys_still_work(self):
        _, raw = auth.mint_key(self.bm, label='desktop', created_by=self.u['owner'])
        self.assertEqual(oauth.resolve_actor(raw)[0].pk, self.bm.pk)
        self.assertEqual(oauth.verify(raw).client_id.startswith('static-key-'), True)


class ConsentPageTests(McpBusinessManagerFixture):
    def setUp(self):
        super().setUp()
        info = OAuthClientInformationFull(client_id='cid-2', client_name='Claude', redirect_uris=[CALLBACK],
                                          token_endpoint_auth_method='none')
        oauth.save_client(info)
        url = oauth.park_request(info, AuthorizationParams(
            state='st8', scopes=None, code_challenge='c' * 43, redirect_uri=CALLBACK, redirect_uri_provided_explicitly=True))
        self.req = McpOAuthRequest.objects.get(pk=url.rstrip('/').split('/')[-1])
        self.url = reverse('crm:mcp_authorize', args=[self.req.pk])

    def test_anonymous_is_sent_to_sign_in(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn('login', r['Location'])

    def test_only_an_owner_may_approve(self):
        for key in ('manager', 'sales', 'finance'):
            self.login(key)
            self.assertEqual(self.client.get(self.url).status_code, 403, key)
            self.assertEqual(self.client.post(self.url, {'decision': 'approve'}).status_code, 403, key)
        self.assertFalse(McpOAuthToken.objects.filter(kind='code').exists())

    def test_owner_sees_the_request_then_approves(self):
        self.login('owner')
        page = self.client.get(self.url)
        self.assertContains(page, 'Business Manager')
        r = self.client.post(self.url, {'decision': 'approve'})
        self.assertEqual(r.status_code, 302)
        target = urlparse(r['Location'])
        self.assertEqual(f'{target.scheme}://{target.netloc}{target.path}', CALLBACK)
        q = parse_qs(target.query)
        self.assertEqual(q['state'], ['st8'])
        self.assertEqual(oauth.load_code('cid-2', q['code'][0]).scopes, ['mcp'])
        self.assertEqual(McpOAuthToken.objects.get(kind='code').approved_by_id, self.u['owner'].pk)
        self.assertEqual(self.client.get(self.url).status_code, 410)    # already used

    def test_deny_returns_access_denied_and_issues_nothing(self):
        self.login('owner')
        r = self.client.post(self.url, {'decision': 'deny'})
        self.assertEqual(parse_qs(urlparse(r['Location']).query)['error'], ['access_denied'])
        self.assertFalse(McpOAuthToken.objects.filter(kind='code').exists())

    def test_expired_request_cannot_be_approved(self):
        McpOAuthRequest.objects.filter(pk=self.req.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.login('owner')
        self.assertEqual(self.client.post(self.url, {'decision': 'approve'}).status_code, 410)
        self.assertFalse(McpOAuthToken.objects.filter(kind='code').exists())


class SiteEndpointTests(McpBusinessManagerFixture):
    """The connector is served by the main site (no extra process). Only the database-free paths are
    exercised here; the full sign-in flow runs in OAuthFlowTests above."""

    def test_discovery_is_served_from_the_site_root(self):
        for path in ('/.well-known/oauth-protected-resource/mcp', '/.well-known/oauth-protected-resource'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.json()['scopes_supported'], ['mcp'])
        meta = self.client.get('/.well-known/oauth-authorization-server').json()
        for key in ('authorization_endpoint', 'token_endpoint', 'registration_endpoint'):
            self.assertTrue(meta[key].startswith(meta['issuer'].rstrip('/')), key)
        self.assertEqual(meta['code_challenge_methods_supported'], ['S256'])

    def test_mcp_without_a_token_points_to_the_sign_in(self):
        r = self.client.post('/mcp', '{}', content_type='application/json')
        self.assertEqual(r.status_code, 401)
        self.assertIn('resource_metadata=', r['WWW-Authenticate'])

    def test_no_csrf_token_needed_on_oauth_endpoints(self):
        from django.test import Client
        c = Client(enforce_csrf_checks=True)
        self.assertEqual(c.post('/mcp', '{}', content_type='application/json').status_code, 401)   # not a CSRF 403
        self.assertIn(c.post('/token', {'grant_type': 'authorization_code'}).status_code, (400, 401))   # rejected as a bad client, not as CSRF
