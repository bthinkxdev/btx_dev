"""OAuth state for the remote MCP server, so claude.ai's "Connect" button can sign in.

Only hashes of codes and tokens are stored (like McpApiKey). Every token acts as the `owner`
service account; `approved_by` records which CRM owner clicked Approve.
"""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone

from .base import USER


class McpOAuthClient(models.Model):
    """An app (e.g. claude.ai) that registered itself with the MCP server."""
    client_id = models.CharField(max_length=80, unique=True)
    info = models.JSONField(default=dict)          # full registration record, incl. redirect URIs
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f'{self.info.get("client_name") or self.client_id}'


class McpOAuthRequest(models.Model):
    """An authorization request waiting for the owner to approve or deny it in the CRM."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client_id = models.CharField(max_length=80)
    params = models.JSONField(default=dict)         # state, scopes, code_challenge, redirect_uri, resource
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    resolved_at = models.DateTimeField(null=True, blank=True)

    def is_open(self) -> bool:
        return self.resolved_at is None and self.expires_at > timezone.now()


class McpOAuthToken(models.Model):
    KINDS = [('code', 'Authorization code'), ('access', 'Access token'), ('refresh', 'Refresh token')]
    kind = models.CharField(max_length=10, choices=KINDS)
    token_hash = models.CharField(max_length=64, unique=True)
    client_id = models.CharField(max_length=80, db_index=True)
    scopes = models.JSONField(default=list)
    resource = models.CharField(max_length=300, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    # authorization codes only
    code_challenge = models.CharField(max_length=128, blank=True)
    redirect_uri = models.CharField(max_length=500, blank=True)
    redirect_uri_provided_explicitly = models.BooleanField(default=True)
    # groups the access + refresh token issued together, so revoking one revokes both
    family = models.UUIDField(default=uuid.uuid4, db_index=True)
    owner = models.ForeignKey(USER, on_delete=models.PROTECT, related_name='+')
    approved_by = models.ForeignKey(USER, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['kind', 'expires_at'])]

    def is_usable(self) -> bool:
        return self.revoked_at is None and (self.expires_at is None or self.expires_at > timezone.now())
