"""The consent server — run BY HAND, once per bank, then stopped.

Local-first stand-in for the two Vercel functions in the build plan:

    /oauth/start?bank=lloyds|hsbc   -> redirect to the aggregator's consent page
    /oauth/callback?code=...&state=  -> exchange the code, store the tokens

Nothing here runs on a schedule and nothing needs to stay up: bank consent
requires the human logging into the bank's own page, so this listens on
127.0.0.1:5180 only while that is happening.

    /opt/anaconda3/bin/python3 api/server.py     (Ctrl-C when done)

At migration time these two handlers become /expense-tracker/api/oauth-start
and oauth-callback on Vercel; the logic is already split to make that a move,
not a rewrite.
"""
from __future__ import annotations

import logging
import secrets
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import aggregator_client, store  # noqa: E402

PORT = 5180
log = logging.getLogger("expense-oauth")

# state -> bank, so the callback knows which consent is completing. In-memory
# is fine: this process lives only for the minutes a consent takes.
_pending: dict[str, str] = {}


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            if url.path == "/oauth/start":
                return self._start(query)
            if url.path == "/oauth/callback":
                return self._callback(query)
            return self._respond(404, "unknown path — /oauth/start?bank=lloyds|hsbc")
        except NotImplementedError as exc:
            # The aggregator stub: the wiring works, the provider does not
            # exist yet. Say exactly what is missing.
            return self._respond(501, str(exc))
        except Exception as exc:  # noqa: BLE001 — a consent flow surfaces errors to the human
            log.exception("oauth error")
            return self._respond(500, f"{type(exc).__name__}: {exc}")

    def _start(self, query: dict) -> None:
        bank = query.get("bank", "")
        if bank not in aggregator_client.BANKS:
            return self._respond(400, f"bank must be one of {aggregator_client.BANKS}")
        state = secrets.token_urlsafe(16)
        _pending[state] = bank
        auth_url = aggregator_client.client().get_auth_url(bank, state)
        self.send_response(302)
        self.send_header("Location", auth_url)
        self.end_headers()

    def _callback(self, query: dict) -> None:
        state = query.get("state", "")
        bank = _pending.pop(state, None)
        if not bank:
            return self._respond(400, "unknown or reused state — restart at /oauth/start")
        code = query.get("code", "")
        if not code:
            return self._respond(400, f"no authorisation code in callback: {query}")
        token = aggregator_client.client().exchange_token(code)
        with store.connect() as conn:
            store.save_connection(
                conn, bank=bank,
                access_token=token["access_token"],
                refresh_token=token.get("refresh_token"),
                consent_expires_at=token["consent_expires_at"])
        self._respond(200, f"{bank} connected — consent stored, expires "
                           f"{token['consent_expires_at']}. You can close this tab.")

    def _respond(self, status: int, message: str) -> None:
        body = message.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # noqa: A003
        log.info("%s " + fmt, self.address_string(), *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    with HTTPServer(("127.0.0.1", PORT), Handler) as httpd:
        log.info("consent server on http://127.0.0.1:%d — "
                 "/oauth/start?bank=lloyds then follow the redirects", PORT)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("done")


if __name__ == "__main__":
    main()
