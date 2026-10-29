"""Discovers serial devices, starts/stops a ModemWorker per device, resolves modem ids."""
from __future__ import annotations

import glob
import logging
import os
import threading
from typing import Callable

from .config import Settings
from .db import Database
from .events import EventBus
from .modem import ModemWorker

log = logging.getLogger(__name__)


def discover_devices(explicit: list[str] | None) -> list[str]:
    if explicit:
        return [d for d in explicit if "://" in d or os.path.exists(d)]
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    found: dict[str, str] = {}
    for d in by_id:
        found.setdefault(os.path.realpath(d), d)
    for d in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")):
        found.setdefault(os.path.realpath(d), d)
    return list(found.values())


class ModemManager:
    def __init__(self, settings: Settings, db: Database, bus: EventBus,
                 open_port: Callable[[str, int], object] | None = None,
                 discover: Callable[[], list[str]] | None = None) -> None:
        self.settings = settings
        self.db = db
        self.bus = bus
        self._open_port = open_port
        self._discover = discover or (lambda: discover_devices(settings.device_list()))
        self.workers: dict[str, ModemWorker] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._scan_loop, name="modem-scanner", daemon=True)
        self._lock = threading.Lock()

    def start(self) -> None:
        self.scan()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self.workers.values())
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=5)

    def _scan_loop(self) -> None:
        while not self._stop.wait(self.settings.scan_interval):
            try:
                self.scan()
            except Exception:
                log.exception("device scan failed")

    def scan(self) -> None:
        devices = self._discover()
        with self._lock:
            for dev in devices:
                if dev not in self.workers:
                    log.info("starting worker for %s", dev)
                    w = ModemWorker(dev, self.settings, self.db, self.bus, open_port=self._open_port)
                    self.workers[dev] = w
                    w.start()
            for dev in list(self.workers):
                w = self.workers[dev]
                if dev not in devices and "://" not in dev and not os.path.exists(dev) and w.state.status == "disconnected":
                    log.info("device %s removed, stopping worker", dev)
                    w.stop()
                    del self.workers[dev]
                    self.bus.publish("modem.removed", {"id": w.state.id, "device": dev})

    def list(self) -> list[ModemWorker]:
        with self._lock:
            return list(self.workers.values())

    def get(self, modem_id: str) -> ModemWorker | None:
        for w in self.list():
            if w.state.id == modem_id or w.device == modem_id or os.path.basename(w.device) == modem_id:
                return w
        return None

    def pick_ready(self) -> ModemWorker | None:
        ready = [w for w in self.list() if w.ready and w.state.registration in ("home", "roaming")]
        return ready[0] if ready else None
