"""Generates simple branded cover images for blog posts.

BlogPost.featured_image is a required field with no ready-made artwork
pipeline. Rather than leave posts imageless (they're excluded from the
blog listing without one) or fabricate a fake screenshot, this renders a
clean typographic cover in the site's own palette. Swap these for real
custom graphics whenever better artwork is available — see
pages/management/commands/seed_blog_posts.py.
"""
from __future__ import annotations

import io

from django.core.files.base import ContentFile

WIDTH, HEIGHT = 1200, 630

BG_TOP = (9, 9, 11)
BG_BOTTOM = (19, 19, 23)
TEXT_PRIMARY = (244, 244, 246)
TEXT_MUTED = (152, 153, 168)
ACCENT = (165, 164, 250)
TEAL = (94, 234, 212)

CATEGORY_ACCENTS = {
    'ecommerce': ACCENT,
    'digital-marketing': TEAL,
}

FONT_CANDIDATES_BOLD = [
    r'C:\Windows\Fonts\seguibl.ttf',
    r'C:\Windows\Fonts\arialbd.ttf',
]
FONT_CANDIDATES_REGULAR = [
    r'C:\Windows\Fonts\segoeui.ttf',
    r'C:\Windows\Fonts\arial.ttf',
]


def _load_font(candidates, size):
    from PIL import ImageFont

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ''
    for word in words:
        trial = f'{current} {word}'.strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def generate_cover(title: str, category_slug: str, kicker: str) -> ContentFile:
    """Render a 1200x630 branded cover and return it as a ContentFile (PNG)."""
    from PIL import Image, ImageDraw

    accent = CATEGORY_ACCENTS.get(category_slug, ACCENT)

    img = Image.new('RGB', (WIDTH, HEIGHT), BG_TOP)
    draw = ImageDraw.Draw(img)

    for y in range(HEIGHT):
        t = y / HEIGHT
        r = int(BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * t)
        g = int(BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * t)
        b = int(BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * t)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b))

    draw.rectangle([(0, 0), (10, HEIGHT)], fill=accent)
    draw.ellipse([(WIDTH - 420, -220), (WIDTH + 180, 380)], outline=accent, width=2)

    kicker_font = _load_font(FONT_CANDIDATES_REGULAR, 30)
    draw.text((72, 72), kicker.upper(), font=kicker_font, fill=accent)

    title_font = _load_font(FONT_CANDIDATES_BOLD, 58)
    lines = _wrap_text(draw, title, title_font, WIDTH - 144)[:4]
    y = 190
    for line in lines:
        draw.text((72, y), line, font=title_font, fill=TEXT_PRIMARY)
        y += 70

    brand_font = _load_font(FONT_CANDIDATES_BOLD, 32)
    draw.text((72, HEIGHT - 90), 'BThinkX', font=brand_font, fill=TEXT_PRIMARY)
    sub_font = _load_font(FONT_CANDIDATES_REGULAR, 22)
    draw.text((210, HEIGHT - 84), 'bthinkx.com', font=sub_font, fill=TEXT_MUTED)

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return ContentFile(buf.read())
