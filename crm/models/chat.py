"""Website chatbot conversations (visitor <-> assistant). Read-only history for managers."""
from __future__ import annotations

import uuid

from django.db import models
from django.utils import timezone


class ChatSession(models.Model):
    """One visitor's conversation. `token` is the unguessable id the browser keeps."""
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    last_message_at = models.DateTimeField(default=timezone.now, db_index=True)
    visitor_name = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=20, blank=True)
    email = models.EmailField(blank=True)
    page_url = models.CharField(max_length=300, blank=True)
    referrer = models.CharField(max_length=300, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    message_count = models.PositiveIntegerField(default=0)
    lead = models.ForeignKey('crm.Lead', null=True, blank=True, on_delete=models.SET_NULL, related_name='chat_sessions')
    lead_captured_at = models.DateTimeField(null=True, blank=True)
    interest = models.CharField(max_length=200, blank=True, help_text='Package or service the visitor asked about.')
    manager_alerted_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ['-last_message_at']

    def __str__(self):
        return self.visitor_name or f'Visitor {str(self.token)[:8]}'


class ChatMessage(models.Model):
    ROLES = [('visitor', 'Visitor'), ('bot', 'Assistant')]
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=8, choices=ROLES)
    content = models.TextField()
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    engine = models.CharField(max_length=40, blank=True, help_text='Model name, or "rules" for built-in answers.')

    class Meta:
        ordering = ['created_at', 'id']

    def __str__(self):
        return f'{self.role}: {self.content[:40]}'
