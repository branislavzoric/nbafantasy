"""Send a message to the B3 admin chat on Telegram (@b3basketball_bot).

Usage:
  python telegram_notify.py "text"                  send a plain message
  python telegram_notify.py --daily <mode> <log>    summary of a run_daily.cmd run (exit code + last lines)

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID in .env. Never raises: an alert must not break the
job that sends it.
"""

import html
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
API = f"https://api.telegram.org/bot{os.getenv('TELEGRAM_BOT_TOKEN', '')}/"
ADMIN = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")


def notify(text, buttons=None):
    """Send `text` (HTML) to the admin chat. buttons = [[(label, callback_data), ...], ...]."""
    if not ADMIN:
        return None
    body = {"chat_id": ADMIN, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row]
                                                    for row in buttons]}
    try:
        return requests.post(API + "sendMessage", json=body, timeout=20).json()
    except requests.RequestException:
        return None


def daily_summary(mode, log_path):
    """The run's exit code and the closing summary Claude wrote at the end of the log."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return notify(f"⚠️ Daily <b>{mode}</b> run: log not found\n<code>{html.escape(log_path)}</code>")
    runs = text.split("=== ")
    last = "=== ".join(runs[-3:]) if len(runs) >= 3 else text  # the latest start ... end block
    m = re.findall(r"end \w+ \(exit (\d+)\)", last)
    code = m[-1] if m else "?"
    body = re.sub(r"=== .*\n?", "", last).strip()
    tail = body[-1500:] if body else "(no output)"
    icon = "✅" if code == "0" else "❌"
    return notify(f"{icon} Daily <b>{mode}</b> run finished (exit {code})\n\n<pre>{html.escape(tail)}</pre>")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--daily":
        daily_summary(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 2:
        notify(" ".join(sys.argv[1:]))
    else:
        print(__doc__)
