"""Optional alert outside the web page, for people running Radar on a server.

If RADAR_NOTIFY_URL is set, POST {"title", "message"} to it (works with ntfy.sh, a Home Assistant or n8n webhook, ...).
Off by default: the one-command Mac install only shows a banner in the app, so someone who has stopped using Radar
is never pestered. Best effort: never raises.
"""
import logging
import os
import threading

import httpx

log = logging.getLogger("radar.notify")


def send(title: str, message: str) -> None:
    url = os.environ.get("RADAR_NOTIFY_URL")
    if not url:
        return

    def post():
        try:
            httpx.post(url, json={"title": title, "message": message}, timeout=10)
        except Exception:
            log.warning("notify webhook failed")
    threading.Thread(target=post, daemon=True).start()
