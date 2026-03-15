"""Refactor full page templates to extend base.html (content only)."""
import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).parent / "templates" / "pages"

PAGES = [
    ("services.html", "<!-- PAGE HERO -->", "<!-- FOOTER -->"),
    ("portfolio.html", "<!-- PAGE HERO -->", "<!-- FOOTER -->"),
    ("about.html", "<!-- PAGE HERO -->", "<!-- FOOTER -->"),
    ("blog.html", "<!-- PAGE HERO -->", "<!-- FOOTER -->"),
    ("404.html", "<!-- 404 CONTENT -->", "</main>"),
]

def extract_style(content):
    m = re.search(r"<style>(.*?)</style>", content, re.DOTALL)
    return m.group(1).strip() if m else None

def extract_title(content):
    m = re.search(r"<title>(.*?)</title>", content)
    return m.group(1).strip() if m else "BThinkX Dev"

def extract_description(content):
    m = re.search(r'<meta name="description" content="(.*?)"', content)
    return m.group(1).strip() if m else None

def refactor(filename, start_marker, end_marker):
    path = TEMPLATES_DIR / filename
    if not path.exists():
        print(f"Skip {filename} (not found)")
        return
    content = path.read_text(encoding="utf-8")
    if "{% extends 'pages/base.html' %}" in content:
        print(f"Skip {filename} (already extends base)")
        return

    style = extract_style(content)
    title = extract_title(content)
    desc = extract_description(content)

    idx_start = content.find(start_marker)
    idx_end = content.find(end_marker)
    if idx_start == -1 or idx_end == -1:
        print(f"Skip {filename}: markers not found (start={idx_start}, end={idx_end})")
        return

    if "404" in filename:
        content_block = content[idx_start : idx_end + len("</main>")]
    else:
        content_block = content[idx_start:idx_end].rstrip()

    parts = [
        "{% extends 'pages/base.html' %}",
        "{% load static %}",
        "",
        "{% block title %}" + title + "{% endblock %}",
    ]
    if desc:
        parts.append('{% block meta_description %}' + desc + "{% endblock %}")
        parts.append("")
    if style:
        parts.append("{% block extra_head %}")
        parts.append("<style>")
        parts.append(style)
        parts.append("</style>")
        parts.append("{% endblock %}")
        parts.append("")
    parts.append("{% block content %}")
    parts.append(content_block)
    parts.append("{% endblock %}")

    path.write_text("\n".join(parts), encoding="utf-8")
    print(f"Refactored {filename}")

def main():
    for filename, start, end in PAGES:
        refactor(filename, start, end)

if __name__ == "__main__":
    main()
