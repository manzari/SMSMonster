"""Delivers bus events to configured targets: plain webhooks (HTTP POST, optional HMAC) or ntfy topics."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from urllib.parse import urlsplit, urlunsplit

import httpx

from .db import Database

log = logging.getLogger(__name__)
WEBHOOK_EVENTS = ("message.received", "message.sent", "message.failed", "message.updated", "call.received", "modem.updated", "modem.removed")
WEBHOOK_TYPES = ("webhook", "ntfy")
MAX_PENDING = 200


def _who(d: dict) -> str:
    return str(d.get("number") or "unknown")


def ntfy_message(event: str, data: dict) -> dict:
    """Map a bus event to ntfy title/message/tags/priority."""
    text = str(data.get("text") or "")
    if event == "message.received":
        return {"title": f"SMS from {_who(data)}", "message": text or "(empty)", "tags": ["envelope"]}
    if event == "message.sent":
        return {"title": f"SMS sent to {_who(data)}", "message": text, "tags": ["outbox_tray"]}
    if event == "message.failed":
        err = data.get("error") or "unknown error"
        return {"title": f"SMS to {_who(data)} failed", "message": f"{err}\n\n{text}".strip(), "tags": ["warning"], "priority": 4}
    if event == "message.updated":
        return {"title": f"SMS to {_who(data)} {data.get('status') or 'updated'}", "message": text, "tags": ["speech_balloon"]}
    if event == "call.received":
        return {"title": f"Call from {_who(data)}", "message": f"Incoming call on modem {data.get('modem_id') or '?'}", "tags": ["telephone_receiver"]}
    if event == "modem.updated":
        name = data.get("name") or data.get("id") or "modem"
        bits = [str(data.get("status") or ""), str(data.get("operator") or "")]
        if data.get("signal_percent") is not None:
            bits.append(f"{data['signal_percent']}% signal")
        if data.get("last_error"):
            bits.append(str(data["last_error"]))
        return {"title": f"Modem {name}", "message": ", ".join(b for b in bits if b), "tags": ["satellite_antenna"]}
    if event == "modem.removed":
        return {"title": "Modem removed", "message": f"{data.get('id') or '?'} on {data.get('device') or '?'}", "tags": ["electric_plug"], "priority": 4}
    return {"title": f"SMS Monster: {event}", "message": str(data.get("message") or json.dumps(data, default=str)), "tags": ["monster"]}


def split_ntfy_url(url: str) -> tuple[str, str]:
    """'https://ntfy.sh/alerts' -> ('https://ntfy.sh/', 'alerts')."""
    u = urlsplit(url)
    base, _, topic = u.path.rstrip("/").rpartition("/")
    return urlunsplit((u.scheme, u.netloc, base + "/", "", "")), topic


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
        if hook.get("type") == "ntfy":
            url, topic = split_ntfy_url(hook["url"])
            if not topic:
                log.warning("ntfy %s has no topic", hook["url"])
                return
            payload = json.dumps({"topic": topic, **ntfy_message(event, data)}, ensure_ascii=False).encode()
            headers = {"Content-Type": "application/json"}
            if hook.get("secret"):
                sec = hook["secret"]
                headers["Authorization"] = ("Basic " + base64.b64encode(sec.encode()).decode()) if ":" in sec else "Bearer " + sec
        else:
            url = hook["url"]
            payload = json.dumps({"event": event, "data": data}, default=str).encode()
            headers = {"Content-Type": "application/json", "X-SMSMonster-Event": event}
            if hook.get("secret"):
                headers["X-SMSMonster-Signature"] = "sha256=" + hmac.new(hook["secret"].encode(), payload, hashlib.sha256).hexdigest()
        for attempt in range(3):
            try:
                r = await self._client.post(url, content=payload, headers=headers)
                if r.status_code < 400:
                    return
                log.warning("webhook %s returned %s", hook["url"], r.status_code)
            except httpx.HTTPError as e:
                log.warning("webhook %s failed: %s", hook["url"], e)
            await asyncio.sleep(2 * (attempt + 1))
