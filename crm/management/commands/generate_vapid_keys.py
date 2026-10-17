"""
Generates a fresh VAPID keypair for CRM web push and prints it — paste the
output into .env (or your deployment's secret store). Only run this once per
environment-set: regenerating invalidates every device that already
subscribed under the old key, since the browser ties a subscription to the
public key it was created with.
"""
import base64

from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid02


def b64urlencode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('utf-8').rstrip('=')


class Command(BaseCommand):
    help = 'Generate a new VAPID keypair for CRM web push (prints VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY).'

    def handle(self, *args, **options):
        vapid = Vapid02()
        vapid.generate_keys()

        raw_public = vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        public_b64 = b64urlencode(raw_public)

        raw_private = vapid.private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        private_b64 = b64urlencode(raw_private)

        self.stdout.write(self.style.SUCCESS('New VAPID keypair — add these to .env:'))
        self.stdout.write(f'VAPID_PUBLIC_KEY={public_b64}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={private_b64}')
