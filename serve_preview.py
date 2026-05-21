#!/usr/bin/env python3
"""Serve preview.html from the repo root (default port 8090).

Use this when charger_web.py is not running.  If Flask is already up
(e.g. on 8088), open http://<host>:<port>/preview.html on that server
instead — do not start a second http.server on the same port.
"""
import argparse
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def do_GET(self):
        if self.path in ("", "/", "/index.html"):
            self.path = "/preview.html"
        return super().do_GET()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8090,
                    help="listen port (default 8090; avoid 8088 if Flask uses it)")
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), PreviewHandler)
    url = f"http://{args.host}:{args.port}/preview.html"
    print(f"Serving {ROOT}")
    print(f"Open {url}  (root / redirects here)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
