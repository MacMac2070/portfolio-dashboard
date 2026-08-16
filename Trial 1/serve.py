#!/usr/bin/env python3
"""Static dev server for the dashboard.

Plain `python3 -m http.server` sends no Cache-Control, so browsers apply
heuristic caching and keep serving stale CSS/JS through an edit-reload loop.
This sends no-store on everything, which is what you want while iterating.
"""
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class NoCacheHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    root = Path(__file__).resolve().parent
    handler = partial(NoCacheHandler, directory=str(root))
    with ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Serving {root} on http://localhost:{port}", flush=True)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
