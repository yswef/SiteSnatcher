"""
selftest.py — verification harness (no external network needed).

Runs a tiny website on 127.0.0.1, mirrors it with the real engine, then asserts
that every link inside the mirror points to a file that actually exists on disk.

Usage:  python selftest.py
"""
import http.server
import os
import shutil
import socketserver
import tempfile
import threading

from core.crawler import CrawlConfig, Crawler
from core.rewriter import make_soup
from core.url_utils import local_path_for

SITE = {
    "index.html": """<html><head><link rel="stylesheet" href="/assets/style.css"></head>
        <body><h1>Home</h1>
        <a href="/about">About</a>
        <a href="blog/">Blog</a>
        <a href="/search?q=hello">Search</a>
        <img src="assets/logo.png" srcset="assets/logo.png 1x, assets/logo.png 2x">
        <script src="/assets/app.js"></script></body></html>""",
    "about/index.html": """<html><body><a href="/">Home</a>
        <div style="background:url('../assets/logo.png')">x</div></body></html>""",
    "blog/index.html": """<html><body><a href="../about">About</a>
        <img src="/assets/logo.png"></body></html>""",
    "assets/style.css": "@import 'fonts.css';\nbody{background:url(logo.png)}",
    "assets/fonts.css": "h1{color:red}",
    "assets/app.js": "console.log(1)",
    "assets/logo.png": "\x89PNG-fake",
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        path = self.path.split("?")[0].lstrip("/")
        if path in ("", "search"):
            path = "index.html"
        if path in ("about", "blog"):
            path += "/index.html"
        if path.endswith("/"):
            path += "index.html"
        body = SITE.get(path)
        if body is None:
            self.send_error(404)
            return
        ctype = ("text/html" if path.endswith(".html") else
                 "text/css" if path.endswith(".css") else
                 "text/javascript" if path.endswith(".js") else "image/png")
        data = body.encode("utf-8", "replace")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def unit_tests():
    cases = [
        ("http://s.com/", "text/html", "s.com/index.html"),
        ("http://s.com/a/b.html", "text/html", "s.com/a/b.html"),
        ("http://s.com/a/b/", "text/html", "s.com/a/b/index.html"),
        ("http://s.com/about", "text/html", "s.com/about.html"),
        ("http://s.com/p.php", "text/html", "s.com/p_php.html"),
        ("http://s.com/i/x.png", "image/png", "s.com/i/x.png"),
    ]
    for url, ctype, expected in cases:
        got = local_path_for(url, ctype)
        assert got == expected, f"{url} -> {got} (expected {expected})"
    assert local_path_for("http://s.com/p?q=1", "text/html").startswith("s.com/p_q")
    print("unit tests: path mapping OK")


def e2e():
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        out = tempfile.mkdtemp(prefix="mirror_")
        events = []
        cfg = CrawlConfig(
            start_url=f"http://127.0.0.1:{port}/",
            out_dir=out,
            max_depth=3,
            max_pages=50,
            workers=4,
            obey_robots=False,
        )
        crawler = Crawler(cfg, lambda kind, payload: events.append((kind, payload)))
        crawler.run()
        httpd.shutdown()

        files = sorted(
            os.path.relpath(os.path.join(root, f), out).replace("\\", "/")
            for root, _, fs in os.walk(out) for f in fs
        )
        print("\nmirror tree:")
        for f in files:
            print("  ", f)

        broken = []
        for rel in files:
            if not rel.endswith(".html"):
                continue
            full = os.path.join(out, rel)
            soup = make_soup(open(full, encoding="utf-8").read())
            for tag, attr in (("a", "href"), ("img", "src"),
                              ("link", "href"), ("script", "src")):
                for node in soup.find_all(tag):
                    href = node.get(attr)
                    if not href or href.startswith(("http", "#", "data:")):
                        continue
                    target = os.path.normpath(
                        os.path.join(os.path.dirname(full), href.split("#")[0])
                    )
                    if not os.path.exists(target):
                        broken.append(f"{rel} -> {href}")

        css = open(os.path.join(out, f"127.0.0.1_{port}", "assets", "style.css"),
                   encoding="utf-8").read()
        print("\nrewritten css:", css.replace("\n", " | "))

        stats = crawler.stats
        print(f"\npages={stats.pages} assets={stats.assets} failed={stats.failed}")
        if broken:
            print("BROKEN LINKS:")
            for b in broken:
                print("  ", b)
        else:
            print("all local links resolve to existing files ✔")
        shutil.rmtree(out, ignore_errors=True)
        return not broken


if __name__ == "__main__":
    unit_tests()
    ok = e2e()
    print("\nRESULT:", "PASS" if ok else "FAIL")
