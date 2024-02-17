"""
crawler.py
----------
The download engine.

Design (important): the mirror is built in TWO passes.

  Pass 1 - DOWNLOAD : every page/asset is fetched and written to disk exactly
                      as received, while a table {absolute_url -> local_path}
                      is filled in. Nothing is rewritten yet.
  Pass 2 - REWRITE  : once the table is complete, every saved HTML/CSS file is
                      re-read and its links are replaced by relative paths.

Why two passes? A link can point to a page that has not been downloaded yet, so
its final local name is unknown at discovery time. Rewriting at the end removes
that whole class of broken-link bugs.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .rewriter import extract_from_css, extract_from_html, rewrite_css, rewrite_html
from .url_utils import (
    CSS_TYPES,
    HTML_TYPES,
    host_of,
    local_path_for,
    normalize_url,
    relative_link,
    same_site,
    to_fs_path,
)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SiteSnatcher/1.0"
)

# URLs matching these regexes are never queued. They are the classic WordPress
# "infinite queue" traps: API endpoints, feeds, and duplicate ?p= permalinks
# that point at pages already reachable through their pretty URL.
DEFAULT_EXCLUDES = "\n".join([
    r"/wp-json/",
    r"/xmlrpc\.php",
    r"/wp-admin/",
    r"/wp-login\.php",
    r"/feed/?$",
    r"/comments/feed",
    r"[?&]p=\d+",
    r"[?&]page_id=\d+",
    r"[?&]replytocom=",
    r"[?&]attachment_id=",
    r"[?&]share=",
    r"/trackback/?$",
])


@dataclass
class CrawlConfig:
    start_url: str
    out_dir: str
    max_depth: int = 2
    max_pages: int = 200
    workers: int = 8
    delay: float = 0.0
    timeout: int = 20
    include_subdomains: bool = False
    external_assets: bool = True
    obey_robots: bool = True
    user_agent: str = DEFAULT_UA
    max_file_mb: int = 50
    # Only pages whose path starts with this prefix are crawled ("/ar/" mirrors
    # just the Arabic section). Assets are NOT affected - CSS/JS/images usually
    # live outside the prefix (e.g. /wp-content/).
    path_prefix: str = ""
    exclude_patterns: str = DEFAULT_EXCLUDES
    retries: int = 2


@dataclass
class CrawlStats:
    pages: int = 0
    assets: int = 0
    failed: int = 0
    bytes: int = 0
    queued: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def files(self) -> int:
        return self.pages + self.assets

    @property
    def elapsed(self) -> float:
        return time.time() - self.started_at


class Crawler:
    """Runs in a background thread; talks to the UI only through `emit`."""

    def __init__(self, config: CrawlConfig, emit):
        self.cfg = config
        self.emit = emit  # emit(kind: str, payload: dict)

        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()  # set == running

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._active = 0
        self._seen: set[str] = set()

        # url -> local relative path (POSIX)
        self.saved: dict[str, str] = {}
        # files that need pass 2: (url, rel_path, "html" | "css")
        self._textual: list[tuple[str, str, str]] = []

        self.stats = CrawlStats()
        self._robots: dict[str, RobotFileParser | None] = {}
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.cfg.user_agent})
        self._exclude_res: list[re.Pattern] = []

    # ------------------------------------------------------------------ #
    # public control
    # ------------------------------------------------------------------ #
    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()

    def pause(self) -> None:
        self.pause_event.clear()

    def resume(self) -> None:
        self.pause_event.set()

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def _log(self, message: str, level: str = "info") -> None:
        self.emit("log", {"level": level, "message": message})

    def _push_stats(self) -> None:
        self.stats.queued = self._queue.qsize()
        self.emit("stats", {
            "pages": self.stats.pages,
            "assets": self.stats.assets,
            "failed": self.stats.failed,
            "bytes": self.stats.bytes,
            "queued": self.stats.queued,
            "elapsed": self.stats.elapsed,
            "max_pages": self.cfg.max_pages,
        })

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.cfg.obey_robots:
            return True
        host = host_of(url)
        if host not in self._robots:
            parser = RobotFileParser()
            robots_url = urljoin(f"{urlsplit(url).scheme}://{host}/", "robots.txt")
            try:
                response = self._session.get(robots_url, timeout=self.cfg.timeout)
                if response.status_code == 200:
                    parser.parse(response.text.splitlines())
                else:
                    parser = None
            except Exception:
                parser = None
            self._robots[host] = parser
        parser = self._robots.get(host)
        if parser is None:
            return True
        try:
            return parser.can_fetch(self.cfg.user_agent, url)
        except Exception:
            return True

    def _is_excluded(self, url: str) -> bool:
        for regex in self._exclude_res:
            if regex.search(url):
                return True
        return False

    def _matches_prefix(self, url: str) -> bool:
        prefix = (self.cfg.path_prefix or "").strip()
        if not prefix:
            return True
        if not prefix.startswith("/"):
            prefix = "/" + prefix
        path = urlsplit(url).path or "/"
        # "/ar/" must also accept the section root written as "/ar"
        return path.startswith(prefix) or path == prefix.rstrip("/")

    def _enqueue(self, url: str, depth: int, kind: str) -> None:
        if self.stop_event.is_set():
            return
        url = normalize_url(url)
        if not url.startswith(("http://", "https://")):
            return

        if self._is_excluded(url):
            return

        if kind == "page":
            if depth > self.cfg.max_depth:
                return
            if not same_site(url, self.root_host, self.cfg.include_subdomains):
                return
            if not self._matches_prefix(url):
                return
        else:
            if not self.cfg.external_assets and not same_site(
                url, self.root_host, self.cfg.include_subdomains
            ):
                return

        with self._lock:
            if url in self._seen:
                return
            if kind == "page" and self._page_budget_used >= self.cfg.max_pages:
                return
            if kind == "page":
                self._page_budget_used += 1
            self._seen.add(url)

        self._queue.put((url, depth, kind))

    # ------------------------------------------------------------------ #
    # main entry point (blocking)
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        try:
            start = normalize_url(self.cfg.start_url)
            self.root_host = host_of(start)
            self._page_budget_used = 0

            self._exclude_res = []
            for line in (self.cfg.exclude_patterns or "").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    self._exclude_res.append(re.compile(line))
                except re.error as exc:
                    self._log(f"Bad exclude pattern '{line}': {exc}", "warn")

            if not self.root_host:
                self.emit("error", {"message": "Invalid URL."})
                return

            os.makedirs(self.cfg.out_dir, exist_ok=True)
            self._log(f"Target: {start}", "accent")
            self._log(f"Output: {self.cfg.out_dir}")
            if self.cfg.path_prefix.strip():
                self._log(f"Pages restricted to path: {self.cfg.path_prefix.strip()}", "warn")
            self._log(f"Active exclude patterns: {len(self._exclude_res)}")

            self._enqueue(start, 0, "page")

            threads = [
                threading.Thread(target=self._worker, daemon=True)
                for _ in range(max(1, self.cfg.workers))
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            if self.stop_event.is_set():
                self._log("Stopped by user.", "warn")
            else:
                self._log("Download pass finished. Rewriting links…", "accent")

            self._rewrite_pass()
            self._write_entry_shortcut(start)

            self._push_stats()
            self.emit("done", {
                "stopped": self.stop_event.is_set(),
                "pages": self.stats.pages,
                "assets": self.stats.assets,
                "failed": self.stats.failed,
                "bytes": self.stats.bytes,
                "out_dir": self.cfg.out_dir,
                "entry": self.saved.get(start, ""),
            })
        except Exception as exc:  # never let the worker thread die silently
            self.emit("error", {"message": f"{type(exc).__name__}: {exc}"})

    # ------------------------------------------------------------------ #
    # pass 1
    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        while not self.stop_event.is_set():
            try:
                url, depth, kind = self._queue.get(timeout=0.4)
            except queue.Empty:
                with self._lock:
                    if self._active == 0 and self._queue.empty():
                        return
                continue

            with self._lock:
                self._active += 1
            try:
                self.pause_event.wait()
                if not self.stop_event.is_set():
                    self._fetch(url, depth, kind)
            finally:
                with self._lock:
                    self._active -= 1
                self._queue.task_done()
                if self.cfg.delay:
                    time.sleep(self.cfg.delay)

    def _fetch(self, url: str, depth: int, kind: str) -> None:
        if not self._allowed_by_robots(url):
            self._log(f"robots.txt blocked: {url}", "warn")
            return

        attempts = max(1, self.cfg.retries + 1)
        response = None
        content = b""
        content_type = ""
        size = 0

        for attempt in range(1, attempts + 1):
            if self.stop_event.is_set():
                return
            try:
                response = self._session.get(
                    url, timeout=self.cfg.timeout, stream=True, allow_redirects=True
                )
                if response.status_code >= 400:
                    self.stats.failed += 1
                    self._log(f"HTTP {response.status_code}: {url}", "error")
                    return

                limit = self.cfg.max_file_mb * 1024 * 1024
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    if self.stop_event.is_set():
                        return
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        self._log(f"Skipped (>{self.cfg.max_file_mb}MB): {url}", "warn")
                        return
                content = b"".join(chunks)
                content_type = response.headers.get("Content-Type", "")
                break
            except Exception as exc:
                if attempt < attempts:
                    self._log(
                        f"{type(exc).__name__} (retry {attempt}/{attempts - 1}): {url}",
                        "warn",
                    )
                    time.sleep(min(6.0, 1.5 * attempt))
                    continue
                self.stats.failed += 1
                self._log(f"{type(exc).__name__}: {url}", "error")
                return

        if response is None:
            return

        ct = content_type.split(";")[0].strip().lower()
        rel = local_path_for(url, content_type)
        fs_path = to_fs_path(self.cfg.out_dir, rel)
        os.makedirs(os.path.dirname(fs_path), exist_ok=True)

        try:
            with open(fs_path, "wb") as handle:
                handle.write(content)
        except OSError as exc:
            self.stats.failed += 1
            self._log(f"Write failed ({exc}): {rel}", "error")
            return

        with self._lock:
            self.saved[url] = rel
            # a redirect means the final URL differs from the requested one
            final_url = normalize_url(response.url)
            if final_url != url:
                self.saved.setdefault(final_url, rel)
            self.stats.bytes += size
            if ct in HTML_TYPES:
                self.stats.pages += 1
            else:
                self.stats.assets += 1
            if ct in HTML_TYPES or ct in CSS_TYPES:
                self._textual.append((url, rel, "html" if ct in HTML_TYPES else "css"))

        self._log(f"✓ {rel}", "ok")
        self._push_stats()

        if ct in HTML_TYPES:
            text = content.decode(response.encoding or "utf-8", errors="replace")
            pages, assets = extract_from_html(text, url)
            for asset in assets:
                self._enqueue(asset, depth, "asset")
            for page in pages:
                self._enqueue(page, depth + 1, "page")
        elif ct in CSS_TYPES:
            text = content.decode(response.encoding or "utf-8", errors="replace")
            for asset in extract_from_css(text, url):
                self._enqueue(asset, depth, "asset")

    # ------------------------------------------------------------------ #
    # pass 2
    # ------------------------------------------------------------------ #
    def _rewrite_pass(self) -> None:
        total = len(self._textual)
        for index, (url, rel, kind) in enumerate(self._textual, 1):
            fs_path = to_fs_path(self.cfg.out_dir, rel)
            try:
                with open(fs_path, "r", encoding="utf-8", errors="replace") as handle:
                    text = handle.read()

                def resolve(absolute: str, _rel=rel):
                    target = self.saved.get(absolute)
                    if not target:
                        return None
                    return relative_link(_rel, target)

                new_text = (
                    rewrite_html(text, url, resolve)
                    if kind == "html"
                    else rewrite_css(text, url, resolve)
                )
                with open(fs_path, "w", encoding="utf-8", errors="replace") as handle:
                    handle.write(new_text)
            except Exception as exc:
                self._log(f"Rewrite failed for {rel}: {exc}", "error")

            if index % 10 == 0 or index == total:
                self.emit("rewriting", {"done": index, "total": total})

    def _write_entry_shortcut(self, start_url: str) -> None:
        """A root index.html that opens the real entry page."""
        entry = self.saved.get(start_url)
        if not entry:
            return
        path = os.path.join(self.cfg.out_dir, "index.html")
        if os.path.exists(path):
            return
        html = (
            "<!doctype html><meta charset='utf-8'>"
            f"<meta http-equiv='refresh' content='0; url={entry}'>"
            f"<title>Offline mirror</title><p>Opening <a href='{entry}'>{entry}</a>…</p>"
        )
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(html)
        except OSError:
            pass