"""In-memory SIM800C simulator exposing a pyserial-like interface (read/write/close/in_waiting)."""
from __future__ import annotations

import re
import threading
import time


class FakeSim800:
    def __init__(self, imei="861234567890123", baudrate=115200, sim="READY", pin=None, cms_error=None):
        self.imei = imei
        self.baudrate = baudrate
        self.sim = sim  # READY | SIM PIN | NONE
        self.pin = pin
        self.cms_error = cms_error  # if set, AT+CMGS fails with this code
        self.inbox: dict[int, str] = {}
        self.sent: list[str] = []
        self.commands: list[str] = []
        self.deleted: list[int] = []
        self.echo = True
        self.closed = False
        self.ussd_reply = '+CUSD: 0,"Your balance is 5.00 EUR",15'
        self._rx = b""
        self._cmd = b""
        self._prompt = False
        self._lock = threading.Lock()
        self.opened = 0

    # ---- pyserial-ish ----
    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def read(self, n: int) -> bytes:
        with self._lock:
            if self._rx:
                out, self._rx = self._rx[:n], self._rx[n:]
                return out
        time.sleep(0.02)
        return b""

    def write(self, data: bytes) -> int:
        if self._prompt:
            self._cmd += data
            if b"\x1a" in self._cmd:
                pdu, _, rest = self._cmd.partition(b"\x1a")
                self._cmd = rest
                self._prompt = False
                self.sent.append(pdu.decode().strip())
                if self.cms_error:
                    self._out(f"+CMS ERROR: {self.cms_error}")
                else:
                    self._out(f"+CMGS: {len(self.sent)}", "OK")
            return len(data)
        self._cmd += data
        while b"\r" in self._cmd:
            line, _, self._cmd = self._cmd.partition(b"\r")
            cmd = line.decode().strip()
            if cmd:
                self._handle(cmd)
        return len(data)

    def close(self) -> None:
        self.closed = True

    # ---- test helpers ----
    def push(self, *lines: str) -> None:
        self._out(*lines)

    def deliver_sms(self, pdu_hex: str) -> int:
        idx = max(self.inbox, default=0) + 1
        self.inbox[idx] = pdu_hex
        self._out(f'+CMTI: "SM",{idx}')
        return idx

    def ring(self, number: str) -> None:
        self._out("RING", f'+CLIP: "{number}",145,"",0,"",0')

    def _out(self, *lines: str) -> None:
        with self._lock:
            for l in lines:
                self._rx += b"\r\n" + l.encode("latin-1") + b"\r\n"

    # ---- AT handling ----
    def _handle(self, cmd: str) -> None:
        self.commands.append(cmd)
        if self.echo:
            self._out(cmd)
        u = cmd.upper()
        if u == "AT":
            self._out("OK")
        elif u == "ATE0":
            self.echo = False
            self._out("OK")
        elif u == "AT+CGMM":
            self._out("SIMCOM_SIM800C", "OK")
        elif u == "AT+CGMR":
            self._out("Revision:1418B05SIM800C24", "OK")
        elif u == "AT+CGSN":
            self._out(self.imei, "OK")
        elif u == "AT+CPIN?":
            if self.sim == "NONE":
                self._out("+CME ERROR: 10")
            else:
                self._out(f"+CPIN: {self.sim}", "OK")
        elif u.startswith("AT+CPIN="):
            if self.sim == "SIM PIN" and cmd.split("=", 1)[1].strip('"') == self.pin:
                self.sim = "READY"
                self._out("OK")
            else:
                self._out("+CME ERROR: 16")
        elif u == "AT+CIMI":
            self._out("262011234567890", "OK")
        elif u == "AT+CCID":
            self._out("89490200001234567890", "OK")
        elif u == "AT+CNUM":
            self._out('+CNUM: "","+491701234567",145', "OK")
        elif u == "AT+CSQ":
            self._out("+CSQ: 20,0", "OK")
        elif u == "AT+CREG?":
            self._out("+CREG: 0,1", "OK")
        elif u == "AT+COPS?":
            self._out('+COPS: 0,0,"Telekom.de"', "OK")
        elif u.startswith("AT+CPMS"):
            self._out("+CPMS: 0,30,0,30,0,30", "OK")
        elif u.startswith("AT+CMGR="):
            idx = int(cmd.split("=")[1])
            if idx in self.inbox:
                self._out(f'+CMGR: 0,"",{len(self.inbox[idx]) // 2 - 1}', self.inbox[idx], "OK")
            else:
                self._out("+CMS ERROR: 321")
        elif u.startswith("AT+CMGD="):
            idx = int(cmd.split("=")[1])
            self.deleted.append(idx)
            self.inbox.pop(idx, None)
            self._out("OK")
        elif u == "AT+CMGL=4":
            lines = []
            for idx, pdu in sorted(self.inbox.items()):
                lines += [f'+CMGL: {idx},1,"",{len(pdu) // 2 - 1}', pdu]
            self._out(*lines, "OK")
        elif u.startswith("AT+CMGS="):
            self._prompt = True
            with self._lock:
                self._rx += b"\r\n> "
        elif u.startswith("AT+CUSD="):
            self._out("OK")
            self._out(self.ussd_reply)
        elif u.startswith(("AT+CMEE", "AT+CMGF", "AT+CSCS", "AT+CNMI", "AT+CLIP", "AT+CSMP", "ATH", "AT+CFUN")):
            self._out("OK")
        else:
            self._out("ERROR")


class DeadPort:
    """A port that never answers (used to exercise baudrate probing)."""

    in_waiting = 0

    def read(self, n):
        time.sleep(0.02)
        return b""

    def write(self, data):
        return len(data)

    def close(self):
        pass


def opener(fake: FakeSim800):
    def open_port(device: str, baudrate: int):
        fake.opened += 1
        return fake if baudrate == fake.baudrate else DeadPort()
    return open_port
