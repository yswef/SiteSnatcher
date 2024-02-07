"""
url_utils.py
------------
Pure functions that convert remote URLs into deterministic local file paths
and decide whether a URL belongs to the site being mirrored.

No network access happens here, so this module is fully unit-testable.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import posixpath
import re
from urllib.parse import urlsplit, urlunsplit, unquote, quote

# Characters that are illegal (or troublesome) in file names on Windows/macOS/Linux
_ILLEGAL = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_DOTS = re.compile(r'^\.+$')

SKIP_SCHEMES = ("mailto:", "javascript:", "data:", "tel:", "sms:", "about:", "blob:", "ftp:")

HTML_TYPES = {"text/html", "application/xhtml+xml", "application/xhtml"}
CSS_TYPES = {"text/css"}

_EXT_BY_TYPE = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
}


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def normalize_url(url: str) -> str:
    """Canonical form of a URL: lowercase scheme/host, no fragment, no default port."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "http").lower()
    netloc = parts.netloc.lower()

    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[:-3]
    elif scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[:-4]

    path = parts.path or "/"
    trailing = path.endswith("/")
    path = posixpath.normpath(path)
    if path == ".":
        path = "/"
    if trailing and not path.endswith("/"):
        path += "/"

    return urlunsplit((scheme, netloc, path, parts.query, ""))


def strip_fragment(url: str) -> tuple[str, str]:
    """Return (url_without_fragment, '#fragment' or '')."""
    if "#" in url:
        base, frag = url.split("#", 1)
        return base, "#" + frag
    return url, ""


def is_skippable(href: str) -> bool:
    href = (href or "").strip()
    if not href or href.startswith("#"):
        return True
    return href.lower().startswith(SKIP_SCHEMES)


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


def base_domain(host: str) -> str:
    """'www.example.com' -> 'example.com' (naive but adequate for mirroring)."""
    host = host.split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def same_site(url: str, root_host: str, include_subdomains: bool) -> bool:
    host = host_of(url).split(":")[0]
    root = root_host.split(":")[0]
    if not host:
        return False
    if include_subdomains:
        rb = base_domain(root)
        return host == rb or host.endswith("." + rb)
    return base_domain(host) == base_domain(root)


# --------------------------------------------------------------------------- #
# Local path mapping
# --------------------------------------------------------------------------- #
def _sanitize(segment: str, max_len: int = 90) -> str:
    seg = _ILLEGAL.sub("_", segment).strip().rstrip(". ")
    if not seg or _DOTS.match(seg):
        seg = "_"
    if len(seg) > max_len:
        digest = hashlib.md5(segment.encode("utf-8", "ignore")).hexdigest()[:8]
        seg = seg[: max_len - 9] + "_" + digest
    return seg


def _ext_for(content_type: str | None) -> str:
    if not content_type:
        return ""
    ct = content_type.split(";")[0].strip().lower()
    if ct in _EXT_BY_TYPE:
        return _EXT_BY_TYPE[ct]
    guessed = mimetypes.guess_extension(ct) or ""
    return ".jpg" if guessed == ".jpe" else guessed


def local_path_for(url: str, content_type: str | None = None) -> str:
    """
    Map a URL to a relative POSIX path under the output folder, mirroring the
    remote directory structure:

        https://site.com/            -> site.com/index.html
        https://site.com/a/b.html    -> site.com/a/b.html
        https://site.com/a/b/        -> site.com/a/b/index.html
        https://site.com/about       -> site.com/about.html   (if HTML)
        https://site.com/p?id=3      -> site.com/p_q8c1f0a2.html
    """
    parts = urlsplit(url)
    host = _sanitize(parts.netloc.replace(":", "_") or "localhost")

    path = unquote(parts.path or "/")
    segments = [s for s in path.split("/") if s not in ("", ".", "..")]

    if not segments or path.endswith("/"):
        segments.append("index.html")

    segments = [_sanitize(s) for s in segments]
    stem, ext = os.path.splitext(segments[-1])

    ct = (content_type or "").split(";")[0].strip().lower()
    is_html = ct in HTML_TYPES

    if is_html and ext.lower() not in (".html", ".htm"):
        # /product.php served as HTML -> product.php.html so browsers render it
        stem = stem + ext.replace(".", "_") if ext else stem
        ext = ".html"
    elif not ext:
        ext = _ext_for(ct) or (".html" if not ct else "")

    if parts.query:
        stem += "_q" + hashlib.md5(parts.query.encode("utf-8", "ignore")).hexdigest()[:8]

    segments[-1] = _sanitize(stem) + ext
    return posixpath.join(host, *segments)


def relative_link(from_rel: str, to_rel: str) -> str:
    """URL-encoded relative path from one local file to another."""
    rel = posixpath.relpath(to_rel, posixpath.dirname(from_rel) or ".")
    return quote(rel, safe="/._-~()!$&'*+,;=:@")


def to_fs_path(root: str, rel_posix: str) -> str:
    return os.path.join(root, *rel_posix.split("/"))
