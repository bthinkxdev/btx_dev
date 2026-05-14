"""Encrypted credential vault + audit trail."""

from __future__ import annotations

from typing import Any

from django.db import transaction

from ..crypto import decrypt_ciphertext, encrypt_plaintext
from ..models import CredentialAuditLog, Project, ProjectCredential


def log_audit(
    credential: ProjectCredential,
    action: str,
    *,
    user,
    ip: str | None,
) -> CredentialAuditLog:
    return CredentialAuditLog.objects.create(
        credential=credential,
        action=action,
        user=user if user and getattr(user, 'is_authenticated', False) else None,
        ip=ip or None,
    )


@transaction.atomic
def save_credential(
    *,
    project: Project,
    user,
    ip: str | None,
    instance: ProjectCredential,
    plain_password: str | None = None,
    plain_secret: str | None = None,
    **field_values: Any,
) -> ProjectCredential:
    is_new = instance._state.adding
    for key, val in field_values.items():
        if hasattr(instance, key) and key not in (
            'password_encrypted',
            'secret_key_encrypted',
        ):
            setattr(instance, key, val)
    if plain_password is not None:
        instance.password_encrypted = (
            encrypt_plaintext(plain_password) if plain_password else ''
        )
    if plain_secret is not None:
        instance.secret_key_encrypted = (
            encrypt_plaintext(plain_secret) if plain_secret else ''
        )
    if user and getattr(user, 'is_authenticated', False):
        if is_new:
            instance.created_by = user
        instance.updated_by = user
    instance.save()
    log_audit(
        instance,
        'credential_created' if is_new else 'credential_edited',
        user=user,
        ip=ip,
    )
    return instance


def decrypt_password_for_user(credential: ProjectCredential, *, user) -> str:
    from ..rbac import credential_decrypt_allowed

    if not credential_decrypt_allowed(user, credential):
        return ''
    return decrypt_ciphertext(credential.password_encrypted)


def decrypt_secret_for_user(credential: ProjectCredential, *, user) -> str:
    from ..rbac import credential_decrypt_allowed

    if not credential_decrypt_allowed(user, credential):
        return ''
    return decrypt_ciphertext(credential.secret_key_encrypted)


def record_view(credential: ProjectCredential, *, user, ip: str | None) -> None:
    log_audit(credential, 'credential_viewed', user=user, ip=ip)


def record_copy(credential: ProjectCredential, *, user, ip: str | None) -> None:
    log_audit(credential, 'credential_copied', user=user, ip=ip)


def record_visibility_change(credential: ProjectCredential, *, user, ip: str | None) -> None:
    log_audit(credential, 'visibility_changed', user=user, ip=ip)


def queryset_for_user(user, project: Project):
    """Credentials this user is allowed to enumerate for a project."""
    from ..rbac import credential_allowed_for_role

    qs = ProjectCredential.objects.filter(project=project).order_by('-updated_at')
    return [c for c in qs if credential_allowed_for_role(user, c)]
