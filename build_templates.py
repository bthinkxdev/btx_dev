"""Copy new/*.html to templates/pages/ and replace assets/ and .html links for Django."""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
NEW_DIR = os.path.join(BASE, 'new')
OUT_DIR = os.path.join(BASE, 'templates', 'pages')

URL_MAP = {
    'index.html': "{% url 'pages:index' %}",
    'contact.html': "{% url 'pages:contact' %}",
    'services.html': "{% url 'pages:services' %}",
    'portfolio.html': "{% url 'pages:portfolio' %}",
    'about.html': "{% url 'pages:about' %}",
    'blog.html': "{% url 'pages:blog' %}",
}

os.makedirs(OUT_DIR, exist_ok=True)

for name in ['index.html', 'contact.html', 'services.html', 'portfolio.html', 'about.html', 'blog.html', '404.html']:
    path = os.path.join(NEW_DIR, name)
    if not os.path.isfile(path):
        continue
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # Add {% load static %} after <!DOCTYPE or at top of <head>
    if '{% load static %}' not in content:
        content = content.replace('<head>', '<head>\n  {% load static %}', 1)

    # Replace "assets/..." with "{% static 'assets/...' %}" so attribute quotes are kept
    def replace_asset(m):
        path = m.group(1)  # assets/...
        return '"{% static \'' + path + "' %}\""
    content = re.sub(r'"(assets/[^"]+)"', replace_asset, content)

    # Replace .html links with {% url %}
    for filename, url_tag in URL_MAP.items():
        content = content.replace(f'href="{filename}"', f'href="{url_tag}"')
        content = content.replace(f"href='{filename}'", f"href='{url_tag}'")

    out_path = os.path.join(OUT_DIR, name)
    with open(out_path, 'w', encoding='utf-8', newline='') as f:
        f.write(content)
    print('Wrote', out_path)

print('Done.')
