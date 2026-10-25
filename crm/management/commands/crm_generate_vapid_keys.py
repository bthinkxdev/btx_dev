import base64

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.core.management.base import BaseCommand
from py_vapid import Vapid02


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


class Command(BaseCommand):
    help = (
        'Generate a new VAPID keypair for Web Push and print .env lines to paste in. '
        'Run this once per project — every environment that should deliver push to the '
        'same subscribed devices must share the same keypair, so do not re-run this in '
        'production once devices have subscribed (it invalidates every subscription).'
    )

    def handle(self, *args, **opts):
        vapid = Vapid02()
        vapid.generate_keys()
        public_raw = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
        private_der = vapid.private_pem()
        # private_pem() returns PEM; store the raw base64 DER body (no header/footer/newlines)
        # since that is the compact form pywebpush/py_vapid also accept back in.
        body = b''.join(
            line for line in private_der.splitlines() if b'-----' not in line
        )
        self.stdout.write('Add these to your .env (and copy the same pair to every environment):\n')
        self.stdout.write(f'VAPID_PUBLIC_KEY={_b64url(public_raw)}')
        self.stdout.write(f'VAPID_PRIVATE_KEY={body.decode("ascii")}')
        self.stdout.write('VAPID_CLAIM_EMAIL=mailto:hr@bthinkx.com')
