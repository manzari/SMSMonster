"""Per-modem worker thread: owns the serial port, speaks AT to a SIM800C, receives and sends SMS."""
from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import pdu as pdulib
from .config import Settings
from .db import Database, now_iso
from .events import EventBus

log = logging.getLogger(__name__)

URC_PREFIXES = ("+CMTI:", "+CMT:", "+CDS:", "+CDSI:", "RING", "+CLIP:", "+CUSD:", "NO CARRIER", "BUSY",
                "SMS Ready", "Call Ready", "NORMAL POWER DOWN", "UNDER-VOLTAGE", "OVER-VOLTAGE", "+CPIN:", "+CRING")
CME_ERRORS = {10: "SIM not inserted", 11: "SIM PIN required", 12: "SIM PUK required", 13: "SIM failure", 14: "SIM busy",
              16: "incorrect password", 30: "no network service", 313: "SIM failure", 314: "SIM busy"}
CMS_ERRORS = {300: "ME failure", 301: "SMS service of ME reserved", 302: "operation not allowed", 303: "operation not supported",
              304: "invalid PDU mode parameter", 305: "invalid text mode parameter", 310: "SIM not inserted", 311: "SIM PIN required",
              313: "SIM failure", 314: "SIM busy", 321: "invalid memory index", 322: "SIM memory full", 330: "SMSC address unknown",
              331: "no network service", 332: "network timeout", 500: "unknown error"}
REG_STATES = {0: "not_registered", 1: "home", 2: "searching", 3: "denied", 4: "unknown", 5: "roaming"}


class ModemError(Exception):
    pass


class ModemTimeout(ModemError):
    pass


class ModemGone(ModemError):
    """Serial port went away."""


class SimNotReady(ModemError):
    """SIM missing/locked: port stays open, init is retried periodically."""


@dataclass
class ModemState:
    id: str
    device: str
    status: str = "connecting"
    name: str | None = None
    phone_number: str | None = None
    imei: str | None = None
    iccid: str | None = None
    imsi: str | None = None
    model: str | None = None
    firmware: str | None = None
    operator: str | None = None
    rssi: int | None = None
    signal_dbm: int | None = None
    signal_percent: int | None = None
    registration: str = "unknown"
    baudrate: int | None = None
    last_error: str | None = None
    last_seen: str | None = None
    connected_at: str | None = None
    counters: dict = field(default_factory=lambda: {"received": 0, "sent": 0, "failed": 0})


def _short_device(device: str) -> str:
    if "://" in device:
        return device.split("://", 1)[1].rstrip("/")
    return os.path.basename(device.rstrip("/"))


class ModemWorker(threading.Thread):
    def __init__(self, device: str, settings: Settings, db: Database, bus: EventBus,
                 open_port: Callable[[str, int], object] | None = None) -> None:
        super().__init__(name=f"modem-{_short_device(device)}", daemon=True)
        self.device = device
        self.settings = settings
        self.db = db
        self.bus = bus
        self._open_port = open_port or _open_serial
        self.state = ModemState(id=_short_device(device), device=device)
        self._stop_evt = threading.Event()
        self._jobs: queue.Queue = queue.Queue()
        self._port = None
        self._buf = b""
        self._pending_read: list[int] = []
        self._pending_cdsi: list[int] = []
        self._next_status = 0.0
        self._next_sweep = 0.0
        self._next_send = 0.0
        self._retry_delay = 2.0
        self._identified = False
        self._modem_ready = False
        self._next_init = 0.0
        self._last_cusd: str | None = None
        self._reconnect = False
        self._last_call: tuple[str, float] | None = None

    # ---------------- public API (any thread) ----------------
    def stop(self) -> None:
        self._stop_evt.set()

    def request_reconnect(self) -> None:
        self._reconnect = True

    def submit(self, fn: Callable[["ModemWorker"], object]) -> concurrent.futures.Future:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._jobs.put((fn, fut))
        return fut

    def to_dict(self) -> dict:
        d = asdict(self.state)
        d["queued"] = self._jobs.qsize()
        return d

    @property
    def ready(self) -> bool:
        return self.state.status == "ready"

    @property
    def port_open(self) -> bool:
        return self._port is not None

    # ---------------- thread main ----------------
    def run(self) -> None:
        log.info("[%s] worker started", self.state.id)
        while not self._stop_evt.is_set():
            try:
                if self._port is None:
                    self._connect()
                    self._retry_delay = 2.0
                if not self._modem_ready and time.monotonic() >= self._next_init:
                    try:
                        self._init_modem()
                    except SimNotReady as e:
                        self._set_error(str(e))
                        self._next_init = time.monotonic() + 10
                self._loop_once()
            except ModemGone as e:
                self._disconnect(f"device gone: {e}")
            except ModemError as e:
                self._set_error(str(e))
                self._disconnect(str(e))
            except Exception as e:  # pragma: no cover - defensive
                log.exception("[%s] unexpected error", self.state.id)
                self._set_error(f"{type(e).__name__}: {e}")
                self._disconnect(str(e))
            if self._port is None and not self._stop_evt.is_set():
                self._stop_evt.wait(self._retry_delay)
                self._retry_delay = min(self._retry_delay * 2, 30)
        self._disconnect("stopped", publish=False)
        log.info("[%s] worker stopped", self.state.id)

    def _loop_once(self) -> None:
        if self._reconnect:
            self._reconnect = False
            raise ModemError("reconnect requested")
        line = self._readline(0.2)
        if line:
            self._handle_urc(line)
        while self._pending_read:
            self._read_and_delete(self._pending_read.pop(0))
        try:
            fn, fut = self._jobs.get_nowait()
        except queue.Empty:
            pass
        else:
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(self))
                except Exception as e:
                    fut.set_exception(e)
        if not self._modem_ready:
            return
        now = time.monotonic()
        if now >= self._next_status:
            self._next_status = now + self.settings.status_interval
            self._poll_status()
        if now >= self._next_sweep:
            self._next_sweep = now + self.settings.sweep_interval
            self._sweep_storage()
            self._flush_expired_parts()
        if self.ready and self.state.registration in ("home", "roaming") and now >= self._next_send:
            self._send_next()

    # ---------------- connection ----------------
    def _connect(self) -> None:
        self._set_status("connecting")
        for baud in self.settings.baudrates:
            try:
                port = self._open_port(self.device, baud)
            except Exception as e:
                raise ModemGone(str(e))
            self._port = port
            self._buf = b""
            if self._probe():
                self.state.baudrate = baud
                log.info("[%s] modem answering at %d baud", self.state.id, baud)
                break
            self._close_port()
        else:
            raise ModemError("no AT response on any baudrate")
        self._modem_ready = False
        self._next_init = 0

    def _probe(self) -> bool:
        for _ in range(3):
            try:
                self._write(b"AT\r")
                deadline = time.monotonic() + 0.6
                while time.monotonic() < deadline:
                    line = self._readline(0.2)
                    if line == "OK":
                        return True
            except ModemGone:
                return False
        return False

    def _init_modem(self) -> None:
        st = self.state
        self._set_status("initializing")
        self._command("ATE0")
        self._command("AT+CMEE=1")
        st.model = self._first_line(self._command("AT+CGMM"))
        st.firmware = self._first_line(self._command("AT+CGMR")).replace("Revision:", "").strip()
        imei = self._first_line(self._command("AT+CGSN"))
        if not imei.isdigit():
            raise ModemError(f"unexpected IMEI response: {imei!r}")
        st.imei = imei
        st.id = imei
        self._ensure_sim_ready()
        st.imsi = self._first_line(self._command("AT+CIMI"))
        st.iccid = self._first_line(self._command("AT+CCID", ok_on_error=True)).strip('"')
        self._command("AT+CMGF=0")
        self._command("AT+CSCS=\"GSM\"", ok_on_error=True)
        cnmi = "2,1,0,1,0" if self.settings.delivery_reports else "2,1,0,0,0"
        self._command(f"AT+CNMI={cnmi}")
        self._command("AT+CPMS=\"SM\",\"SM\",\"SM\"", ok_on_error=True)
        self._command("AT+CLIP=1", ok_on_error=True)
        self._command("AT+CSMP=17,167,0,0", ok_on_error=True)
        num = self._parse_cnum(self._command("AT+CNUM", ok_on_error=True))
        row = self.db.upsert_modem(imei, iccid=st.iccid, imsi=st.imsi, model=st.model, firmware=st.firmware)
        st.name = row.get("name") or None
        st.phone_number = row.get("phone_number") or num or None
        if not self._identified:
            self.db.requeue_stuck(imei)
            self._identified = True
        st.connected_at = now_iso()
        st.last_error = None
        self._modem_ready = True
        self._set_status("ready")
        self._next_status = 0
        self._next_sweep = time.monotonic() + 3
        self._poll_status()

    def _ensure_sim_ready(self) -> None:
        deadline = time.monotonic() + 30
        while True:
            lines, err = self._command("AT+CPIN?", ok_on_error=True, timeout=5, return_error=True)
            cpin = self._first_line(lines).replace("+CPIN:", "").strip()
            if cpin == "READY":
                return
            if cpin == "SIM PIN":
                if self.settings.sim_pin:
                    self._command(f"AT+CPIN=\"{self.settings.sim_pin}\"", timeout=10)
                    time.sleep(2)
                    continue
                self._set_status("sim_pin_required")
                raise SimNotReady("SIM PIN required but SMSMONSTER_SIM_PIN not set")
            if cpin == "SIM PUK":
                self._set_status("sim_locked")
                raise SimNotReady("SIM is PUK locked")
            if err and ("10" in err or "not inserted" in err.lower()):
                self._set_status("no_sim")
                raise SimNotReady("SIM not inserted (check that the SIM is seated, then Restart)")
            if time.monotonic() > deadline:
                self._set_status("error")
                raise ModemError(f"SIM not ready: {cpin or err}")
            time.sleep(1)

    def _disconnect(self, reason: str, publish: bool = True) -> None:
        self._close_port()
        self._modem_ready = False
        if self.state.status != "disconnected":
            log.warning("[%s] disconnected: %s", self.state.id, reason)
        if self.state.status not in ("sim_pin_required", "sim_locked", "no_sim", "error"):
            self.state.status = "disconnected"
        self.state.registration = "unknown"
        self.state.rssi = self.state.signal_dbm = self.state.signal_percent = None
        if publish:
            self._publish_state()

    def _close_port(self) -> None:
        if self._port is not None:
            try:
                self._port.close()
            except Exception:
                pass
        self._port = None

    # ---------------- status ----------------
    def _poll_status(self) -> None:
        st = self.state
        csq = self._first_line(self._command("AT+CSQ")).replace("+CSQ:", "").strip()
        try:
            rssi = int(csq.split(",")[0])
        except ValueError:
            rssi = 99
        if rssi == 99:
            st.rssi = st.signal_dbm = st.signal_percent = None
        else:
            st.rssi = rssi
            st.signal_dbm = -113 + 2 * rssi
            st.signal_percent = min(100, round(rssi * 100 / 31))
        creg = self._first_line(self._command("AT+CREG?")).replace("+CREG:", "").strip()
        try:
            st.registration = REG_STATES.get(int(creg.split(",")[1]), "unknown")
        except (ValueError, IndexError):
            st.registration = "unknown"
        if st.registration in ("home", "roaming"):
            cops = self._first_line(self._command("AT+COPS?", ok_on_error=True))
            m = re.search(r'"([^"]*)"', cops)
            st.operator = m.group(1) if m else None
        else:
            st.operator = None
        st.last_seen = now_iso()
        self._publish_state()

    def _set_status(self, status: str) -> None:
        if self.state.status != status:
            self.state.status = status
            self._publish_state()

    def _set_error(self, msg: str) -> None:
        self.state.last_error = msg
        log.error("[%s] %s", self.state.id, msg)

    def _publish_state(self) -> None:
        self.bus.publish("modem.updated", self.to_dict())

    # ---------------- URCs ----------------
    def _is_urc(self, line: str) -> bool:
        return line.startswith(URC_PREFIXES)

    def _handle_urc(self, line: str) -> None:
        if line.startswith("+CMTI:"):
            try:
                self._pending_read.append(int(line.split(",")[-1]))
            except ValueError:
                log.warning("[%s] bad CMTI: %s", self.state.id, line)
        elif line.startswith("+CDSI:"):
            try:
                self._pending_read.append(int(line.split(",")[-1]))
            except ValueError:
                pass
        elif line.startswith("+CLIP:"):
            m = re.match(r'\+CLIP:\s*"([^"]*)"', line)
            number = m.group(1) if m else ""
            self._on_call(number)
        elif line.startswith("RING") or line.startswith("+CRING"):
            log.debug("[%s] ring", self.state.id)
        elif line.startswith("+CUSD:"):
            self._last_cusd = line
        elif line.startswith("NORMAL POWER DOWN"):
            raise ModemError("modem powered down")
        elif line.startswith("UNDER-VOLTAGE") or line.startswith("OVER-VOLTAGE"):
            self._set_error(line)
        elif line.startswith("+CPIN:") and "READY" not in line and self.ready:
            raise ModemError(f"SIM state changed: {line}")
        else:
            log.debug("[%s] urc: %s", self.state.id, line)

    def _on_call(self, number: str) -> None:
        if self._last_call and time.monotonic() - self._last_call[1] < 10 and self._last_call[0] == number:
            return
        self._last_call = (number, time.monotonic())
        log.info("[%s] incoming call from %s", self.state.id, number or "unknown")
        msg = self.db.insert_message(kind="call", direction="in", modem_id=self.state.id, number=number or "unknown",
                                     text="", status="received", received_at=now_iso())
        self.bus.publish("call.received", msg)
        if self.settings.hangup_calls:
            try:
                self._command("ATH", ok_on_error=True)
            except ModemError:
                pass

    # ---------------- receiving ----------------
    def _read_and_delete(self, index: int) -> None:
        lines = self._command(f"AT+CMGR={index}", timeout=10)
        pdu_hex = next((l for l in lines if not l.startswith("+CMGR") and re.fullmatch(r"[0-9A-Fa-f]+", l)), None)
        if pdu_hex:
            self._process_pdu(pdu_hex)
        else:
            log.warning("[%s] CMGR %d returned no PDU: %s", self.state.id, index, lines)
        if self.settings.delete_from_sim:
            self._command(f"AT+CMGD={index}", ok_on_error=True)

    def _sweep_storage(self) -> None:
        lines = self._command("AT+CMGL=4", timeout=30)
        index = None
        for line in lines:
            if line.startswith("+CMGL:"):
                try:
                    index = int(line.split(":", 1)[1].split(",")[0])
                except ValueError:
                    index = None
            elif index is not None and re.fullmatch(r"[0-9A-Fa-f]+", line):
                self._process_pdu(line)
                if self.settings.delete_from_sim:
                    self._command(f"AT+CMGD={index}", ok_on_error=True)
                index = None

    def _process_pdu(self, pdu_hex: str) -> None:
        try:
            sms = pdulib.decode_pdu(pdu_hex)
        except pdulib.PduError as e:
            log.warning("[%s] undecodable PDU (%s): %s", self.state.id, e, pdu_hex)
            self._store_incoming("unknown", f"[undecodable PDU] {pdu_hex}", None)
            return
        if sms.kind == "status_report":
            self._on_status_report(sms)
            return
        if sms.kind != "deliver":
            log.info("[%s] ignoring stored %s PDU", self.state.id, sms.kind)
            return
        ts = sms.timestamp.isoformat() if sms.timestamp else None
        if sms.concat and sms.concat.total > 1:
            parts = self.db.add_part(self.state.id, sms.number, sms.concat.ref, sms.concat.total, sms.concat.seq, sms.text, ts)
            if parts is None:
                self.bus.publish("message.partial", {"modem_id": self.state.id, "number": sms.number,
                                                     "ref": sms.concat.ref, "total": sms.concat.total, "seq": sms.concat.seq})
                return
            text = "".join(p["text"] for p in parts)
            self._store_incoming(sms.number, text, parts[0]["received_at"], parts_total=sms.concat.total, parts_received=len(parts))
        else:
            self._store_incoming(sms.number, sms.text, ts)

    def _flush_expired_parts(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=self.settings.part_timeout)).replace(microsecond=0).isoformat()
        for parts in self.db.expired_part_groups(cutoff):
            if parts and parts[0]["modem_id"] == self.state.id:
                text = "".join(p["text"] for p in parts)
                self._store_incoming(parts[0]["number"], text, parts[0]["received_at"],
                                     parts_total=parts[0]["total"], parts_received=len(parts))

    def _store_incoming(self, number: str, text: str, received_at: str | None, parts_total: int = 1, parts_received: int = 1) -> None:
        msg = self.db.insert_message(direction="in", modem_id=self.state.id, number=number, text=text, status="received",
                                     received_at=received_at or now_iso(), parts_total=parts_total, parts_received=parts_received)
        self.state.counters["received"] += 1
        log.info("[%s] SMS from %s: %s", self.state.id, number, text[:80])
        self.bus.publish("message.received", msg)

    def _on_status_report(self, sms: pdulib.DecodedSms) -> None:
        mr = sms.extra.get("mr")
        rows = self.db.list_messages(direction="out", modem_id=self.state.id, limit=50)
        for row in rows:
            if row["reference"] == mr and row["status"] in ("sent", "delivered"):
                status = "delivered" if sms.extra.get("status", 0) == 0 else "sent"
                msg = self.db.update_message(row["id"], status=status, error=None if status == "delivered" else f"delivery status {sms.extra.get('status')}")
                self.bus.publish("message.updated", msg)
                return

    # ---------------- sending ----------------
    def _send_next(self) -> None:
        msg = self.db.claim_next_outgoing(self.state.id)
        if not msg:
            self._next_send = time.monotonic() + 0.5
            return
        self.bus.publish("message.updated", msg)
        try:
            ref = self.send_sms(msg["number"], msg["text"])
        except ModemError as e:
            err = str(e)
            if msg["attempts"] < self.settings.send_retries:
                upd = self.db.update_message(msg["id"], status="queued", error=err)
                self._next_send = time.monotonic() + 10
                log.warning("[%s] send to %s failed (attempt %d): %s", self.state.id, msg["number"], msg["attempts"], err)
            else:
                upd = self.db.update_message(msg["id"], status="failed", error=err)
                self.state.counters["failed"] += 1
                log.error("[%s] send to %s failed permanently: %s", self.state.id, msg["number"], err)
                self.bus.publish("message.failed", upd)
            self.bus.publish("message.updated", upd)
            if isinstance(e, ModemGone):
                raise
            return
        upd = self.db.update_message(msg["id"], status="sent", sent_at=now_iso(), error=None, reference=ref)
        self.state.counters["sent"] += 1
        log.info("[%s] SMS sent to %s", self.state.id, msg["number"])
        self.bus.publish("message.sent", upd)
        self.bus.publish("message.updated", upd)

    def send_sms(self, number: str, text: str) -> int | None:
        """Send (possibly multipart) SMS synchronously on the worker thread; returns last message reference."""
        try:
            parts = pdulib.encode_submit(number, text, request_status=self.settings.delivery_reports)
        except pdulib.PduError as e:
            raise ModemError(str(e))
        ref = None
        for part in parts:
            self._write(f"AT+CMGS={part.tpdu_len}\r".encode())
            self._wait_prompt(10)
            self._write(part.hex.encode() + b"\x1a")
            lines = self._command(None, timeout=90)
            for l in lines:
                if l.startswith("+CMGS:"):
                    try:
                        ref = int(l.split(":")[1].strip().split(",")[0])
                    except ValueError:
                        pass
        return ref

    # ---------------- jobs available via submit() ----------------
    def job_at(self, command: str, timeout: float = 10) -> list[str]:
        lines, err = self._command(command, timeout=timeout, ok_on_error=True, return_error=True)
        return lines + ([err] if err else [])

    def job_reset(self) -> None:
        """Reboot the modem (AT+CFUN=1,1) and re-initialise afterwards; needed after inserting a SIM."""
        self._command("AT+CFUN=1,1", timeout=10, ok_on_error=True)
        self._modem_ready = False
        self._next_init = time.monotonic() + 8
        self._set_status("connecting")

    def job_ussd(self, code: str, timeout: float = 30) -> dict:
        self._last_cusd = None
        self._command(f'AT+CUSD=1,"{code}",15', timeout=10)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and self._last_cusd is None:
            line = self._readline(0.5)
            if line:
                self._handle_urc(line) if self._is_urc(line) else None
        raw = self._last_cusd
        if raw is None:
            raise ModemTimeout("no USSD response")
        m = re.match(r'\+CUSD:\s*(\d+)(?:,"(.*)"(?:,(\d+))?)?', raw, re.S)
        status, text, dcs = (int(m.group(1)), m.group(2) or "", int(m.group(3) or 0)) if m else (0, raw, 0)
        return {"status": status, "text": _decode_ussd(text, dcs), "raw": raw}

    # ---------------- serial primitives ----------------
    def _write(self, data: bytes) -> None:
        if self._port is None:
            raise ModemGone("port closed")
        try:
            self._port.write(data)
        except Exception as e:
            raise ModemGone(str(e))

    def _readline(self, timeout: float) -> str | None:
        if self._port is None:
            raise ModemGone("port closed")
        deadline = time.monotonic() + timeout
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = self._buf[:nl].strip(b"\r\n ")
                self._buf = self._buf[nl + 1:]
                if line:
                    return line.decode("latin-1")
                continue
            if self._buf.strip() == b">":
                self._buf = b""
                return ">"
            if time.monotonic() >= deadline:
                return None
            try:
                chunk = self._port.read(max(1, getattr(self._port, "in_waiting", 0) or 0))
            except Exception as e:
                raise ModemGone(str(e))
            if chunk:
                self._buf += chunk
            elif not self._buf:
                continue

    def _wait_prompt(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._readline(min(0.5, max(0.01, deadline - time.monotonic())))
            if line == ">":
                return
            if line is None:
                continue
            if line.startswith("ERROR") or line.startswith("+CMS ERROR") or line.startswith("+CME ERROR"):
                raise ModemError(_describe_error(line))
            if self._is_urc(line):
                self._handle_urc(line)
        raise ModemTimeout("no prompt for AT+CMGS")

    def _command(self, cmd: str | None, timeout: float = 5, ok_on_error: bool = False, return_error: bool = False):
        """Send cmd and collect response lines until OK/ERROR. Returns lines, or (lines, error) if return_error."""
        if cmd is not None:
            self._write(cmd.encode() + b"\r")
        lines: list[str] = []
        error: str | None = None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ModemTimeout(f"timeout waiting for response to {cmd or 'data'}")
            line = self._readline(min(0.5, remaining))
            if line is None:
                continue
            if line == "OK":
                break
            if line == "ERROR" or line.startswith("+CME ERROR") or line.startswith("+CMS ERROR"):
                error = _describe_error(line)
                break
            if cmd and line == cmd:
                continue  # echo
            if self._is_urc(line) and not (cmd and line.startswith("+CPIN:") and cmd.startswith("AT+CPIN")):
                self._handle_urc(line)
                continue
            lines.append(line)
        if error and not ok_on_error:
            raise ModemError(f"{cmd}: {error}")
        return (lines, error) if return_error else lines

    @staticmethod
    def _first_line(lines: list[str]) -> str:
        return lines[0].strip() if lines else ""

    @staticmethod
    def _parse_cnum(lines: list[str]) -> str | None:
        for l in lines:
            m = re.match(r'\+CNUM:\s*"[^"]*",\s*"([^"]+)"', l)
            if m:
                return m.group(1)
        return None


def _describe_error(line: str) -> str:
    m = re.match(r"\+(CME|CMS) ERROR:\s*(.*)", line)
    if not m:
        return line
    kind, val = m.group(1), m.group(2).strip()
    table = CME_ERRORS if kind == "CME" else CMS_ERRORS
    if val.isdigit() and int(val) in table:
        return f"{kind} error {val}: {table[int(val)]}"
    return f"{kind} error: {val}"


def _decode_ussd(text: str, dcs: int) -> str:
    text = text.replace('\\"', '"')
    if dcs == 72 or (dcs in (17, 68) and re.fullmatch(r"[0-9A-Fa-f]+", text) and len(text) % 4 == 0):
        try:
            return bytes.fromhex(text).decode("utf-16-be", errors="replace")
        except ValueError:
            return text
    if dcs in (0, 15) and re.fullmatch(r"[0-9A-Fa-f]+", text) and len(text) % 2 == 0 and len(text) > 6:
        data = bytes.fromhex(text)
        return pdulib.gsm7_decode(pdulib.unpack7(data, len(data) * 8 // 7))
    return text


def _open_serial(device: str, baudrate: int):
    import serial  # pyserial, imported lazily so tests run without hardware

    kwargs = dict(baudrate=baudrate, timeout=0.1, write_timeout=2, rtscts=False, dsrdtr=False)
    port = serial.serial_for_url(device, **kwargs) if "://" in device else serial.Serial(device, **kwargs)
    try:
        port.reset_input_buffer()
    except Exception:
        pass
    return port
