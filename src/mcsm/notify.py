from __future__ import annotations

import logging

from .http import HttpClient

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, http: HttpClient, discord_webhook: str = ""):
        self.http = http
        self.discord_webhook = discord_webhook

    def send(self, message: str) -> None:
        log.info("%s", message)
        if not self.discord_webhook:
            return
        try:
            self.http.post_json(self.discord_webhook, {"content": message[:2000], "username": "mcsm"})
        except Exception as e:  # a notification failure must never break an upgrade
            log.warning("discord notification failed: %s", e)
