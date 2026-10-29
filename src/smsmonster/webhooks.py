"""Delivers bus events to configured webhook URLs (HTTP POST, optional HMAC signature)."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

import httpx

from .db import Database

log = logging.getLogger(__name__)
WEBHOOK_EVENTS = ("message.received", "message.sent", "message.failed", "message.updated", "call.received", "modem.updated", "modem.removed")
MAX_PENDING = 200


class WebhookDispatcher:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._client = httpx.AsyncClient(timeout=15)
        self._pending = 0

    async def close(self) -> None:
        await self._client.aclose()

    def __call__(self, event: str, data: dict) -> None:
        hooks = [h for h in self.db.list_webhooks() if h["enabled"] and (event in h["events"] or "*" in h["events"])]
        for h in hooks:
            if self._pending >= MAX_PENDING:
                log.warning("webhook backlog full, dropping %s for %s", event, h["url"])
                continue
            self._pending += 1
            asyncio.ensure_future(self._deliver_tracked(h, event, data))

    async def _deliver_tracked(self, hook: dict, event: str, data: dict) -> None:
        try:
            await self._deliver(hook, event, data)
        finally:
            self._pending -= 1

    async def _deliver(self, hook: dict, event: str, data: dict) -> None:
        payload = json.dumps({"event": event, "data": data}, default=str).encode()
        headers = {"Content-Type": "application/json", "X-SMSMonster-Event": event}
        if hook.get("secret"):
            headers["X-SMSMonster-Signature"] = "sha256=" + hmac.new(hook["secret"].encode(), payload, hashlib.sha256).hexdigest()
        for attempt in range(3):
            try:
                r = await self._client.post(hook["url"], content=payload, headers=headers)
                if r.status_code < 400:
                    return
                log.warning("webhook %s returned %s", hook["url"], r.status_code)
            except httpx.HTTPError as e:
                log.warning("webhook %s failed: %s", hook["url"], e)
            await asyncio.sleep(2 * (attempt + 1))
