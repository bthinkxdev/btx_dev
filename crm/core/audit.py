"""Append-only audit log and security events (Part 13.5, 3.6).

Never logged: passwords, tokens, gateway secrets, card data, document bodies.
"""
from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal

from django.db import transaction

from .middleware import client_ip, current_context

logger = logging.getLogger('crm.audit')

SECRET_KEYS = {'password', 'secret', 'token', 'gateway_secret', 'webhook_secret', 'totp_secret', 'card',
               'token_hash', 'token_nonce', 'share_nonce', 'share_token_hash', 'gateway_secret_enc',
               'gateway_webhook_secret_enc'}


def _clean(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: ('[redacted]' if any(s in k.lower() for s in SECRET_KEYS) else _clean(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    if hasattr(value, 'pk'):
        return str(value)
    return value


def diff(before: dict, after: dict) -> dict:
    """{'field': [old, new]} for changed fields only."""
    out = {}
    for key in set(before) | set(after):
        b, a = before.get(key), after.get(key)
        if _clean(b) != _clean(a):
            out[key] = [_clean(b), _clean(a)]
    return _clean(out)


def snapshot(obj, fields) -> dict:
    return {f: getattr(obj, f, None) for f in fields}


def log(action: str, obj=None, *, actor=None, changes=None, reason: str = '', result: str = 'success',
        object_type: str = '', object_id: str = '', object_repr: str = '', actor_type: str = '', channel: str = '',
        job_id: str = '') -> None:
    """Write one audit row. Safe to call inside or outside a transaction."""
    from crm.models import AuditLog
    ctx = current_context()
    actor = actor if actor is not None else ctx.get('user')
    if obj is not None:
        object_type = object_type or obj._meta.model_name
        object_id = object_id or str(obj.pk)
        try:
            object_repr = object_repr or str(obj)[:200]
        except Exception:  # pragma: no cover
            object_repr = object_id
    if actor is not None and getattr(actor, 'is_authenticated', False):
        a_type, a_id, a_name = actor_type or 'user', actor.pk, (actor.get_full_name() or actor.get_username())[:150]
    else:
        a_type, a_id, a_name = actor_type or ('job' if job_id else 'system'), None, ''
    AuditLog.objects.create(
        actor_type=a_type, actor_id=a_id, actor_name=a_name, action=action,
        object_type=object_type[:80], object_id=str(object_id)[:64], object_repr=object_repr[:200],
        result=result, changes=_clean(changes or {}), reason=reason or '',
        request_id=ctx.get('request_id', ''), job_id=job_id, ip=ctx.get('ip'),
        channel=channel or ctx.get('channel', 'web'),
    )


def security_event(kind: str, *, user=None, username: str = '', request=None, detail: str = '') -> None:
    from crm.models import SecurityEvent
    try:
        with transaction.atomic():
            SecurityEvent.objects.create(
                kind=kind,
                user=user if (user is not None and getattr(user, 'is_authenticated', False)) else None,
                username=(username or (user.get_username() if user is not None and getattr(user, 'is_authenticated', False) else ''))[:150],
                ip=client_ip(request) if request is not None else current_context().get('ip'),
                user_agent=(request.META.get('HTTP_USER_AGENT', '')[:300] if request is not None else ''),
                path=(request.path[:300] if request is not None else ''),
                detail=detail[:500],
            )
    except Exception:  # never let security logging break the request
        logger.exception('Could not write security event %s', kind)


def _row_digest(row) -> str:
    data = json.dumps({
        'id': row.id, 'at': row.created_at.isoformat(), 'actor': row.actor_id, 'action': row.action,
        'type': row.object_type, 'oid': row.object_id, 'result': row.result, 'changes': row.changes,
        'reason': row.reason,
    }, sort_keys=True, default=str)
    return hashlib.sha256(data.encode()).hexdigest()


def write_checkpoint() -> object | None:
    """Hash every audit row since the last checkpoint into a chain (tamper evidence)."""
    from crm.models import AuditCheckpoint, AuditLog
    last = AuditCheckpoint.objects.order_by('-last_id').first()
    start = last.last_id if last else 0
    rows = list(AuditLog.objects.filter(id__gt=start).order_by('id')[:50000])
    if not rows:
        return None
    h = hashlib.sha256((last.chain_hash if last else '').encode())
    for row in rows:
        h.update(_row_digest(row).encode())
    return AuditCheckpoint.objects.create(
        first_id=rows[0].id, last_id=rows[-1].id, row_count=len(rows),
        previous_hash=last.chain_hash if last else '', chain_hash=h.hexdigest(),
    )


def verify_chain() -> tuple[bool, str]:
    """Recompute every checkpoint; returns (ok, message)."""
    from crm.models import AuditCheckpoint, AuditLog
    prev = ''
    for cp in AuditCheckpoint.objects.order_by('last_id'):
        rows = AuditLog.objects.filter(id__gte=cp.first_id, id__lte=cp.last_id).order_by('id')
        h = hashlib.sha256(prev.encode())
        count = 0
        for row in rows.iterator():
            h.update(_row_digest(row).encode())
            count += 1
        if count != cp.row_count or h.hexdigest() != cp.chain_hash or cp.previous_hash != prev:
            return False, f'Audit rows {cp.first_id}-{cp.last_id} do not match their checkpoint.'
        prev = cp.chain_hash
    return True, 'All audit checkpoints verified.'
