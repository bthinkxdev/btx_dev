"""BThinkX LLP company details used in generated PDFs (quotations, etc.)."""

from pathlib import Path

from django.conf import settings

BASE_DIR = Path(settings.BASE_DIR)

COMPANY = {
    'legal_name': 'BThinkX LLP',
    'address_lines': [
        '2nd Floor, Suffis Arcade',
        'Pattom, Trivandrum, Kerala, India',
    ],
    'pin': '695004',
    'phone': '95441 96763',
    'reg_no': 'ACS-9851',
    'email': getattr(settings, 'DEFAULT_FROM_EMAIL', 'hr@bthinkx.com'),
}

ASSETS = {
    'logo': BASE_DIR / 'new' / 'assets' / 'images' / 'logo.jpg',
    'seal': BASE_DIR / 'new' / 'assets' / 'images' / 'btxseal.png',
    'signature': BASE_DIR / 'new' / 'assets' / 'images' / 'signature-veena.png',
}
