"""Manual Chromium integration smoke test using a loopback HTTP server."""

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from periodical_collector.browser import PlaywrightBrowser


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/guide")
            self.end_headers()
        elif self.path == "/guide":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", "guide=opened; Path=/")
            self.end_headers()
            self.wfile.write(
                '<button onclick="document.querySelector(\'#item\').hidden=false">下载中心</button>'
                '<p hidden id="item"><a href="/file">稿件模板</a></p>'.encode()
            )
        elif self.path == "/file" and "guide=opened" in self.headers.get("Cookie", ""):
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", 'attachment; filename="guide.pdf"')
            self.end_headers()
            self.wfile.write(b"%PDF-1.4\n%%EOF")
        elif self.path == "/blocked":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/forbidden")
            self.end_headers()
        elif self.path == "/forbidden":
            self.server.blocked_target_reached = True
            self.send_error(403)
        else:
            self.send_error(404)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.blocked_target_reached = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    browser = PlaywrightBrowser(4)
    base = f"http://127.0.0.1:{server.server_port}"
    allowed = {"127.0.0.1"}
    try:
        pages = browser.inspect(base + "/", allowed)
        assert pages[-1].url == base + "/guide", pages
        assert pages[-1].path == ["下载中心"], pages
        assert b'<p id="item">' in pages[-1].content
        assert browser.download(base + "/file", allowed).filename == "guide.pdf"
        try:
            browser.inspect(base + "/blocked", allowed)
        except Exception:
            pass
        else:
            raise AssertionError("Outside host redirect was not blocked")
        assert not server.blocked_target_reached
    finally:
        browser.close()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
