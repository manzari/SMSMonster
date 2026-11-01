"""Pushes modems, message counters and the latest messages into Home Assistant as entities and fires HA events.

Works through the Supervisor proxy (add-on, SUPERVISOR_TOKEN) or against any Home Assistant URL with a long-lived token.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx

from .db import Database
from .manager import ModemManager

log = logging.getLogger(__name__)
PREFIX = "sms_monster"
MODEM_PREFIX = f"sensor.{PREFIX}_modem_"
EVENTS = ("message.received", "message.sent", "message.failed", "call.received", "modem.removed")
MAX_QUEUE = 500


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "unknown"


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    return url if url.endswith("/api") else url + "/api"


def _timestamp(*candidates) -> str | None:
    """First parseable ISO timestamp, made timezone-aware (HA's timestamp device class requires it)."""
    for c in candidates:
        if not c:
            continue
        try:
            dt = datetime.fromisoformat(str(c))
        except ValueError:
            continue
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    return None


def modem_entities(m: dict) -> list[tuple[str, dict]]:
    """(entity_id, body) pairs for one modem dict (ModemWorker.to_dict())."""
    key = slug(m["id"])
    label = m.get("name") or m["id"]
    attrs = {k: m.get(k) for k in ("device", "name", "phone_number", "imei", "iccid", "imsi", "model", "firmware", "operator",
                                   "registration", "rssi", "signal_dbm", "signal_percent", "baudrate", "last_error",
                                   "last_seen", "connected_at", "queued")}
    attrs.update({"friendly_name": f"SMS Monster {label}", "icon": "mdi:sim", "modem_id": m["id"],
                  **{f"{k}_total": v for k, v in (m.get("counters") or {}).items()}})
    pct = m.get("signal_percent")
    signal = {"state": pct if pct is not None else "unknown",
              "attributes": {"friendly_name": f"SMS Monster {label} signal", "icon": "mdi:signal", "unit_of_measurement": "%",
                             "state_class": "measurement", "rssi": m.get("rssi"), "signal_dbm": m.get("signal_dbm"),
                             "modem_id": m["id"]}}
    return [(f"{MODEM_PREFIX}{key}", {"state": m.get("status") or "unknown", "attributes": attrs}),
            (f"{MODEM_PREFIX}{key}_signal", signal)]


def message_entity(event: str, msg: dict) -> tuple[str, dict] | None:
    if event == "message.received":
        eid, name, icon, ts = "last_received", "SMS Monster last received SMS", "mdi:message-arrow-left", _timestamp(msg.get("received_at"), msg.get("created_at"))
    elif event == "message.sent":
        eid, name, icon, ts = "last_sent", "SMS Monster last sent SMS", "mdi:message-arrow-right", _timestamp(msg.get("sent_at"), msg.get("created_at"))
    elif event == "message.failed":
        eid, name, icon, ts = "last_failed", "SMS Monster last failed SMS", "mdi:message-alert", _timestamp(msg.get("created_at"))
    elif event == "call.received":
        eid, name, icon, ts = "last_call", "SMS Monster last call", "mdi:phone-incoming", _timestamp(msg.get("received_at"), msg.get("created_at"))
    else:
        return None
    attrs = {"friendly_name": name, "icon": icon, "device_class": "timestamp",
             **{k: msg.get(k) for k in ("id", "number", "text", "modem_id", "status", "error", "parts_total", "parts_received")}}
    return f"sensor.{PREFIX}_{eid}", {"state": ts or "unknown", "attributes": attrs}


def counter_entities(stats: dict, modems: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for key, name, icon in (("received", "received SMS", "mdi:inbox-arrow-down"), ("sent", "sent SMS", "mdi:send"),
                            ("failed", "failed SMS", "mdi:alert-circle"), ("queued", "queued SMS", "mdi:tray-full")):
        attrs = {"friendly_name": f"SMS Monster {name}", "icon": icon, "unit_of_measurement": "messages",
                 "state_class": "measurement" if key == "queued" else "total_increasing"}
        out.append((f"sensor.{PREFIX}_{key}", {"state": stats.get(key, 0), "attributes": attrs}))
    ready = [m for m in modems if m.get("status") == "ready"]
    out.append((f"sensor.{PREFIX}_modems", {"state": len(modems), "attributes": {
        "friendly_name": "SMS Monster modems", "icon": "mdi:cellphone-link", "ready": len(ready),
        "modems": [{"id": m["id"], "name": m.get("name"), "status": m.get("status"), "device": m.get("device")} for m in modems]}}))
    return out


class HomeAssistant:
    def __init__(self, db: Database, manager: ModemManager, url: str, token: str, resync_interval: float = 60.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.db, self.manager = db, manager
        self.url = normalize_url(url)
        self.resync_interval = resync_interval
        self._client = httpx.AsyncClient(base_url=self.url, headers={"Authorization": f"Bearer {token}"}, timeout=10, transport=transport)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._tasks: list[asyncio.Task] = []
        self._modem_entities: dict[str, str] = {}  # device -> entity_id, so a renamed id (ttyUSB0 -> IMEI) drops the old entity
        self._modem_status: dict[str, str] = {}
        self._online: bool | None = None

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._worker()), asyncio.create_task(self._resync_loop())]

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()

    def __call__(self, event: str, data: dict) -> None:
        """EventBus listener; runs on the event loop."""
        if event == "modem.updated":
            self._enqueue(self._push_modem, data)
        elif event == "modem.removed":
            self._enqueue(self._remove_modem, data)
            self._enqueue(self._push_counters)
        elif event in ("message.received", "message.sent", "message.failed", "call.received", "message.updated"):
            self._enqueue(self._push_message, event, data)
        if event in EVENTS:
            self._enqueue(self._fire, event, data)

    def _enqueue(self, fn, *args) -> None:
        try:
            self._queue.put_nowait((fn, args))
        except asyncio.QueueFull:
            log.warning("Home Assistant update backlog full, dropping %s", fn.__name__)

    async def _worker(self) -> None:
        while True:
            fn, args = await self._queue.get()
            try:
                await fn(*args)
            except Exception:
                log.exception("Home Assistant update failed")

    async def _resync_loop(self) -> None:
        while True:
            self._enqueue(self.resync)
            await asyncio.sleep(self.resync_interval)

    # ---- pushes ----
    async def resync(self) -> None:
        """Push everything and drop entities for modems that no longer exist (HA forgets REST states on restart)."""
        modems = [w.to_dict() for w in self.manager.list()]
        for m in modems:
            await self._push_modem(m)
        await self._push_counters(modems)
        await self._push_last_messages()
        stale = await self._stale_entities({e for m in modems for e, _ in modem_entities(m)})
        for eid in stale:
            await self._delete(eid)

    async def _push_modem(self, m: dict) -> None:
        entities = modem_entities(m)
        old = self._modem_entities.get(m["device"])
        if old and old != entities[0][0]:
            await self._delete(old)
            await self._delete(old + "_signal")
        self._modem_entities[m["device"]] = entities[0][0]
        for eid, body in entities:
            await self._set_state(eid, body)
        if self._modem_status.get(m["device"]) != m.get("status"):
            self._modem_status[m["device"]] = m.get("status")
            await self._set_state(*counter_entities({}, [w.to_dict() for w in self.manager.list()])[-1])

    async def _remove_modem(self, data: dict) -> None:
        self._modem_status.pop(data.get("device", ""), None)
        eid = self._modem_entities.pop(data.get("device", ""), None) or f"{MODEM_PREFIX}{slug(data.get('id', ''))}"
        await self._delete(eid)
        await self._delete(eid + "_signal")

    async def _push_message(self, event: str, msg: dict) -> None:
        ent = message_entity(event, msg)
        if ent:
            await self._set_state(*ent)
        await self._push_counters()

    async def _push_counters(self, modems: list[dict] | None = None) -> None:
        stats = await asyncio.to_thread(self.db.message_stats)
        if modems is None:
            modems = [w.to_dict() for w in self.manager.list()]
        for eid, body in counter_entities(stats, modems):
            await self._set_state(eid, body)

    async def _push_last_messages(self) -> None:
        for event, kw in (("message.received", {"direction": "in", "kind": "sms"}), ("message.sent", {"direction": "out", "status": "sent"}),
                          ("message.failed", {"direction": "out", "status": "failed"}), ("call.received", {"kind": "call"})):
            rows = await asyncio.to_thread(lambda kw=kw: self.db.list_messages(limit=1, **kw))
            if rows:
                ent = message_entity(event, rows[0])
                if ent:
                    await self._set_state(*ent)

    async def _stale_entities(self, keep: set[str]) -> list[str]:
        try:
            r = await self._client.get("/states")
            r.raise_for_status()
            ids = [s.get("entity_id", "") for s in r.json()]
        except (httpx.HTTPError, ValueError):
            return []
        return [e for e in ids if e.startswith(MODEM_PREFIX) and e not in keep]

    # ---- HTTP ----
    async def _set_state(self, entity_id: str, body: dict) -> None:
        await self._request("POST", f"/states/{entity_id}", json=body)

    async def _delete(self, entity_id: str) -> None:
        await self._request("DELETE", f"/states/{entity_id}", ok=(404,))

    async def _fire(self, event: str, data: dict) -> None:
        await self._request("POST", f"/events/smsmonster_{event.replace('.', '_')}", json=data)

    async def _request(self, method: str, path: str, ok: tuple[int, ...] = (), **kw) -> None:
        try:
            r = await self._client.request(method, path, **kw)
        except httpx.HTTPError as e:
            self._set_online(False, str(e))
            return
        if r.status_code >= 400 and r.status_code not in ok:
            self._set_online(False, f"{method} {path} -> {r.status_code} {r.text[:200]}")
            return
        self._set_online(True)

    def _set_online(self, online: bool, detail: str = "") -> None:
        if online and self._online is not True:
            log.info("Home Assistant integration: connected to %s", self.url)
        elif not online and self._online is not False:
            log.warning("Home Assistant integration: %s unreachable (%s); will keep retrying", self.url, detail)
        self._online = online
