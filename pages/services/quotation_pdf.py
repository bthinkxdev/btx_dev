"""Clean branded PDF quotations for the public pricing packages."""
from __future__ import annotations

import io

from crm.billing_constants import ASSETS, COMPANY

PACKAGES = {
    'online-store': {
        'name': 'Online Store',
        'price': '₹34,999',
        'tagline': 'Your own online shop, ready to take orders and payments from day one.',
        'maintenance': '₹3,500 per year',
        'sections': [
            ('Your website', [
                'Mobile-friendly website that works perfectly on phones',
                'Up to 200 products',
                'Product search and category pages',
                'Free SSL security certificate (the padlock customers trust)',
            ]),
            ('Taking orders and payments', [
                'Online payment by UPI, cards and netbanking',
                'Cash on Delivery option',
                'Shopping cart and simple checkout',
                'Automatic GST invoice on every order',
                'Email alert the moment an order comes in',
            ]),
            ('Managing your shop', [
                'Order and stock management dashboard',
                'Stock updates automatically as items sell',
            ]),
            ('Reaching customers', [
                'WhatsApp chat button',
                'Customer enquiry form',
                'Google-ready setup so people can find you',
            ]),
            ('Support', [
                '1 year free technical support',
            ]),
        ],
    },
    'online-store-marketing': {
        'name': 'Online Store + Marketing',
        'price': '₹49,999',
        'tagline': 'A complete online shop, plus the tools that bring customers back and make your ads work.',
        'maintenance': '₹5,000 per year',
        'sections': [
            ('Your website', [
                'Mobile-friendly website that works perfectly on phones',
                'Premium custom design built around your brand',
                'Up to 500 products',
                'Product search and category pages',
                'Faster loading pages, which means more sales',
                'Free SSL security certificate (the padlock customers trust)',
            ]),
            ('Taking orders and payments', [
                'Online payment by UPI, cards and netbanking',
                'Cash on Delivery option',
                'Shopping cart and simple checkout',
                'Automatic GST invoice on every order',
                'Email alert the moment an order comes in',
            ]),
            ('Managing your shop', [
                'Order and stock management dashboard',
                'Stock updates automatically as items sell',
            ]),
            ('Winning back lost sales', [
                'Abandoned cart recovery, brings back people who almost bought',
                'Discount coupons and special offers',
                'Wishlist and favourites',
            ]),
            ('Marketing and advertising', [
                'Facebook and Meta Ads tracking setup',
                'Google Analytics setup',
                'Google-ready setup so people can find you',
            ]),
            ('WhatsApp and customer contact', [
                'WhatsApp chat button',
                'Collect customer WhatsApp numbers at checkout',
                'Automatic WhatsApp follow-up messages',
                'Live chat with your customers',
            ]),
            ('Trust and reviews', [
                'Customer reviews and star ratings',
                'Customer enquiry form',
            ]),
            ('Delivery', [
                'Multi-courier delivery, best rate on every order',
                'Order tracking your customers can follow',
            ]),
            ('Support', [
                '24×7 support for 1 full year',
            ]),
        ],
    },
    'complete-growth': {
        'name': 'Complete Growth',
        'price': '₹74,999',
        'tagline': 'A complete online shop with loyalty, rewards and insights that turn buyers into regulars.',
        'maintenance': '₹7,500 per year',
        'sections': [
            ('Your website', [
                'Mobile-friendly website that works perfectly on phones',
                'Premium custom design built around your brand',
                'Unlimited products, no cap at any point',
                'Product search and category pages',
                'Faster loading pages, which means more sales',
                'Free SSL security certificate (the padlock customers trust)',
            ]),
            ('Taking orders and payments', [
                'Online payment by UPI, cards and netbanking',
                'Cash on Delivery option',
                'Shopping cart and simple checkout',
                'Automatic GST invoice on every order',
                'Email alert the moment an order comes in',
            ]),
            ('Managing your shop', [
                'Order and stock management dashboard',
                'Stock updates automatically as items sell',
                'Advanced sales dashboard with full reporting',
            ]),
            ('Winning back lost sales', [
                'Abandoned cart recovery, brings back people who almost bought',
                'Discount coupons and special offers',
                'Wishlist and favourites',
            ]),
            ('Marketing and advertising', [
                'Facebook and Meta Ads tracking setup',
                'Google Analytics setup',
                'Google-ready setup so people can find you',
            ]),
            ('WhatsApp and customer contact', [
                'WhatsApp chat button',
                'WhatsApp popup that captures every visitor',
                'Collect customer WhatsApp numbers at checkout',
                'Automatic WhatsApp follow-up messages',
                'Live chat with your customers',
            ]),
            ('Loyalty and repeat customers', [
                'Loyalty points and reward system',
                'Gift cards and store wallet',
                'Customer buying insights and reports',
            ]),
            ('Trust and reviews', [
                'Customer reviews and star ratings',
                'Customer enquiry form',
            ]),
            ('Delivery', [
                'Multi-courier delivery, best rate on every order',
                'Branded order tracking page under your own name',
                'Automatic failed delivery (RTO) handling',
            ]),
            ('Support and growth', [
                'Priority issue resolution',
                'Business growth consultation with our team',
                'High priority support for 1 full year',
            ]),
        ],
    },
}


def _draw_box(c, x, y, w, h, fill=None, stroke=None, radius=0):
    if fill:
        c.setFillColor(fill)
    if stroke:
        c.setStrokeColor(stroke)
        c.setLineWidth(0.6)
    if radius:
        c.roundRect(x, y, w, h, radius, fill=1 if fill else 0, stroke=1 if stroke else 0)
    else:
        c.rect(x, y, w, h, fill=1 if fill else 0, stroke=1 if stroke else 0)


def _hline(c, x1, x2, y, width=0.5, color=None):
    from reportlab.lib import colors

    c.setStrokeColor(color or colors.HexColor('#cccccc'))
    c.setLineWidth(width)
    c.line(x1, y, x2, y)


def render_quote_pdf(package_slug: str, lead=None) -> bytes:
    """Render a clean branded one-package PDF quotation. Returns PDF bytes."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    package = PACKAGES[package_slug]

    page_w, page_h = A4
    margin = 16 * mm
    right_x = page_w - margin
    content_w = page_w - 2 * margin
    bottom_limit = 20 * mm

    black = colors.black
    white = colors.white
    gray = colors.HexColor('#444444')
    light_gray = colors.HexColor('#777777')
    rule = colors.HexColor('#bbbbbb')
    fill_soft = colors.HexColor('#f4f4f4')
    fill_header = colors.HexColor('#1a1a1a')

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f'BThinkX Quotation - {package["name"]}')

    def draw_page_header():
        y = page_h - margin
        c.setFillColor(black)
        c.rect(0, page_h - 3 * mm, page_w, 3 * mm, fill=1, stroke=0)

        header_h = 44
        _draw_box(c, margin, y - header_h, content_w, header_h, fill=fill_soft, stroke=rule, radius=4)

        logo_sz = 12 * mm
        logo_x = margin + 8
        logo_y_center = y - header_h / 2
        text_x = logo_x + logo_sz + 10

        try:
            logo_path = ASSETS['logo']
            if logo_path.exists():
                c.drawImage(
                    ImageReader(str(logo_path)),
                    logo_x,
                    logo_y_center - logo_sz / 2,
                    width=logo_sz,
                    height=logo_sz,
                    mask='auto',
                    preserveAspectRatio=True,
                )
            else:
                text_x = margin + 10
        except Exception:
            text_x = margin + 10

        ty = y - 13
        c.setFont('Helvetica-Bold', 13)
        c.setFillColor(black)
        c.drawString(text_x, ty, COMPANY['legal_name'])
        ty -= 12
        c.setFont('Helvetica', 8)
        c.setFillColor(gray)
        c.drawString(text_x, ty, ', '.join(COMPANY['address_lines']))
        ty -= 10
        c.drawString(text_x, ty, f'Pin {COMPANY["pin"]}  |  Ph {COMPANY["phone"]}  |  Reg {COMPANY["reg_no"]}')

        return y - header_h - 12

    def draw_footer():
        c.setFont('Helvetica', 7.5)
        c.setFillColor(light_gray)
        c.drawString(margin, 10 * mm, f'{COMPANY["legal_name"]} · Reg {COMPANY["reg_no"]} · Ph {COMPANY["phone"]}')
        c.drawRightString(right_x, 10 * mm, 'bthinkx.com')

    def new_page():
        draw_footer()
        c.showPage()
        return draw_page_header()

    y = draw_page_header()

    # Document title band
    title_h = 24
    _draw_box(c, margin, y - title_h, content_w, title_h, fill=fill_header, stroke=0)
    c.setFillColor(white)
    c.setFont('Helvetica-Bold', 11)
    c.drawCentredString(page_w / 2, y - title_h + 7, 'QUOTATION')
    y -= title_h + 14

    # Package name + price
    c.setFont('Helvetica-Bold', 18)
    c.setFillColor(black)
    c.drawString(margin, y, package['name'])
    c.setFont('Helvetica-Bold', 18)
    c.setFillColor(gray)
    c.drawRightString(right_x, y, f'{package["price"]} one-time')
    y -= 18

    c.setFont('Helvetica', 9.5)
    c.setFillColor(gray)
    c.drawString(margin, y, package['tagline'])
    y -= 18

    if lead is not None:
        prepared_for = lead.business_name or lead.name
        c.setFont('Helvetica', 8.5)
        c.setFillColor(light_gray)
        c.drawString(margin, y, f'Prepared for: {lead.name} · {prepared_for}')
        y -= 16

    _hline(c, margin, right_x, y, width=1, color=rule)
    y -= 16

    # Feature sections
    for section_title, features in package['sections']:
        if y - 16 < bottom_limit:
            y = new_page()

        c.setFont('Helvetica-Bold', 10.5)
        c.setFillColor(black)
        c.drawString(margin, y, section_title)
        y -= 14

        c.setFont('Helvetica', 9)
        for feature in features:
            if y - 12 < bottom_limit:
                y = new_page()
            c.setFillColor(colors.HexColor('#2f855a'))
            c.drawString(margin, y, '✓')
            c.setFillColor(gray)
            c.drawString(margin + 12, y, feature)
            y -= 13
        y -= 6

    if y - 26 < bottom_limit:
        y = new_page()

    _hline(c, margin, right_x, y, width=1, color=rule)
    y -= 16
    c.setFont('Helvetica-Bold', 9.5)
    c.setFillColor(black)
    c.drawString(margin, y, 'Maintenance after year 1:')
    c.setFont('Helvetica', 9.5)
    c.setFillColor(gray)
    c.drawString(margin + 130, y, f'{package["maintenance"]} (first year support is included free)')

    draw_footer()
    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
