"""One-line alerts from the nightly job: a macOS notification, optionally mail.

The job has always logged what went wrong; the problem was that nobody reads
a log at 23:35. `send()` puts the same sentence on screen, and on the mail
account named in config.local.json when one is, so a missed night is noticed
the morning after rather than the week after.

Nothing here may raise. An alert that fails must not turn a warning into a
second failure, so every channel is wrapped and the function returns whether
any of them delivered.

    "alerts": {
      "smtp": {"host": "smtp.example.com", "port": 587, "user": "…",
               "password": "…", "from": "…", "to": "…"}
    }
"""
from __future__ import annotations

import json
import logging
import smtplib
import subprocess
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger("notify")

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.local.json"
TIMEOUT = 10.0


def _smtp_config() -> dict | None:
    try:
        doc = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return None
    smtp = (doc.get("alerts") or {}).get("smtp") or {}
    return smtp if smtp.get("host") and smtp.get("to") else None


def _desktop(title: str, body: str) -> bool:
    """A Notification Centre banner via osascript. Works from a LaunchAgent
    because the agent runs in the user's GUI session."""
    script = (f"display notification {json.dumps(body)} "
              f"with title {json.dumps(title)}")
    try:
        done = subprocess.run(["osascript", "-e", script], capture_output=True,
                              timeout=TIMEOUT, check=False)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("notify: desktop notification failed: %s", exc)
        return False


def _mail(title: str, body: str, smtp: dict) -> bool:
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = smtp.get("from") or smtp.get("user") or smtp["to"]
    msg["To"] = smtp["to"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(smtp["host"], int(smtp.get("port") or 587), timeout=TIMEOUT) as conn:
            conn.starttls()
            if smtp.get("user"):
                conn.login(smtp["user"], smtp.get("password") or "")
            conn.send_message(msg)
        return True
    except Exception as exc:  # network, auth, TLS — all mean "not delivered"
        log.warning("notify: mail failed: %s", str(exc)[:160])
        return False


def send(title: str, body: str) -> bool:
    """Deliver on every configured channel. True if any of them took it."""
    delivered = _desktop(title, body)
    smtp = _smtp_config()
    if smtp:
        delivered = _mail(title, body, smtp) or delivered
    log.info("notify: %s — %s (%s)", title, body, "delivered" if delivered else "no channel")
    return delivered


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(send("Portfolio dashboard", " ".join(sys.argv[1:]) or "test notification"))
