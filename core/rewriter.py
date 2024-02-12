"""
rewriter.py
-----------
Two responsibilities:

1. EXTRACT  - find every link/asset reference inside an HTML or CSS document.
2. REWRITE  - replace those references with relative paths that work offline.

Both phases share the same reference table, so a link is always rewritten
exactly the way it was discovered (no drift between passes).
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .url_utils import is_skippable, normalize_url, strip_fragment

# tag -> attributes that hold a single URL
ASSET_ATTRS = {
    "link": ["href"],
    "script": ["src"],
    "img": ["src", "data-src", "data-original", "data-lazy-src"],
    "source": ["src"],
    "video": ["src", "poster"],
    "audio": ["src"],
    "embed": ["src"],
    "object": ["data"],
    "input": ["src"],
    "track": ["src"],
    "body": ["background"],
    "td": ["background"],
    "th": ["background"],
    "table": ["background"],
    "use": ["href", "xlink:href"],
}

PAGE_ATTRS = {
    "a": ["href"],
    "area": ["href"],
    "iframe": ["src"],
    "frame": ["src"],
}

SRCSET_ATTRS = ["srcset", "data-srcset"]

CSS_URL_RE = re.compile(r"""url\(\s*(['"]?)(?!['"]?data:)(.*?)\1\s*\)""", re.I | re.S)
CSS_IMPORT_RE = re.compile(r"""@import\s+(['"])(.*?)\1""", re.I)


def make_soup(markup: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(markup, "lxml")
    except Exception:
        return BeautifulSoup(markup, "html.parser")


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _split_srcset(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]


def extract_from_html(html: str, page_url: str) -> tuple[list[str], list[str]]:
    """Return (page_urls, asset_urls) as absolute, normalized URLs."""
    soup = make_soup(html)
    pages: list[str] = []
    assets: list[str] = []

    base_url = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(page_url, base_tag["href"])

    def add(bucket: list[str], raw: str) -> None:
        if is_skippable(raw):
            return
        clean, _ = strip_fragment(raw.strip())
        if not clean:
            return
        try:
            bucket.append(normalize_url(urljoin(base_url, clean)))
        except Exception:
            pass

    for tag_name, attrs in ASSET_ATTRS.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                if tag.has_attr(attr):
                    add(assets, tag[attr])

    for tag_name, attrs in PAGE_ATTRS.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                if tag.has_attr(attr):
                    add(pages, tag[attr])

    for tag in soup.find_all(True):
        for attr in SRCSET_ATTRS:
            if tag.has_attr(attr):
                for candidate in _split_srcset(tag[attr]):
                    add(assets, candidate.split()[0])
        if tag.has_attr("style"):
            for u in extract_from_css(tag["style"], base_url):
                assets.append(u)

    for style in soup.find_all("style"):
        for u in extract_from_css(style.get_text() or "", base_url):
            assets.append(u)

    return pages, assets


def extract_from_css(css_text: str, css_url: str) -> list[str]:
    found: list[str] = []
    for regex, group in ((CSS_URL_RE, 2), (CSS_IMPORT_RE, 2)):
        for match in regex.finditer(css_text or ""):
            raw = match.group(group).strip()
            if is_skippable(raw):
                continue
            clean, _ = strip_fragment(raw)
            if not clean:
                continue
            try:
                found.append(normalize_url(urljoin(css_url, clean)))
            except Exception:
                pass
    return found


# --------------------------------------------------------------------------- #
# Rewriting
# --------------------------------------------------------------------------- #
def rewrite_html(html: str, page_url: str, resolve) -> str:
    """
    `resolve(absolute_url) -> local_relative_link | None`
    Returning None leaves the reference pointing at the original absolute URL.
    """
    soup = make_soup(html)

    base_url = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base_url = urljoin(page_url, base_tag["href"])
        base_tag.decompose()  # a <base> tag would break relative offline links

    def fix(raw: str) -> str | None:
        if is_skippable(raw):
            return None
        clean, frag = strip_fragment(raw.strip())
        if not clean:
            return None
        try:
            absolute = normalize_url(urljoin(base_url, clean))
        except Exception:
            return None
        target = resolve(absolute)
        if target is None:
            return absolute + frag  # keep it usable online
        return target + frag

    all_attrs = {}
    for mapping in (ASSET_ATTRS, PAGE_ATTRS):
        for tag_name, attrs in mapping.items():
            all_attrs.setdefault(tag_name, []).extend(attrs)

    for tag_name, attrs in all_attrs.items():
        for tag in soup.find_all(tag_name):
            for attr in attrs:
                if tag.has_attr(attr):
                    new = fix(tag[attr])
                    if new:
                        tag[attr] = new

    for tag in soup.find_all(True):
        for attr in SRCSET_ATTRS:
            if tag.has_attr(attr):
                parts = []
                for candidate in _split_srcset(tag[attr]):
                    bits = candidate.split()
                    new = fix(bits[0])
                    bits[0] = new if new else bits[0]
                    parts.append(" ".join(bits))
                tag[attr] = ", ".join(parts)
        if tag.has_attr("style"):
            tag["style"] = rewrite_css(tag["style"], base_url, resolve)

    for style in soup.find_all("style"):
        if style.string:
            style.string.replace_with(rewrite_css(style.get_text(), base_url, resolve))

    return str(soup)


def rewrite_css(css_text: str, css_url: str, resolve) -> str:
    def repl_url(match: re.Match) -> str:
        quote_char, raw = match.group(1), match.group(2).strip()
        new = _resolve_ref(raw, css_url, resolve)
        return f"url({quote_char}{new}{quote_char})"

    def repl_import(match: re.Match) -> str:
        quote_char, raw = match.group(1), match.group(2).strip()
        new = _resolve_ref(raw, css_url, resolve)
        return f"@import {quote_char}{new}{quote_char}"

    text = CSS_URL_RE.sub(repl_url, css_text or "")
    return CSS_IMPORT_RE.sub(repl_import, text)


def _resolve_ref(raw: str, base: str, resolve) -> str:
    if is_skippable(raw):
        return raw
    clean, frag = strip_fragment(raw)
    try:
        absolute = normalize_url(urljoin(base, clean))
    except Exception:
        return raw
    target = resolve(absolute)
    return (target + frag) if target else (absolute + frag)
