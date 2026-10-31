"""Run the app against simulated SIM800C modems (no hardware): python tests/demo_server.py"""
import itertools
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import uvicorn

sys.path.insert(0, os.path.dirname(__file__))
from fake_sim800 import FakeSim800  # noqa: E402

from smsmonster.api import create_app  # noqa: E402
from smsmonster.config import Settings  # noqa: E402
from smsmonster.db import Database  # noqa: E402
from smsmonster.events import EventBus  # noqa: E402
from smsmonster.manager import ModemManager  # noqa: E402
from smsmonster.pdu import encode_address, gsm7_encode, pack7  # noqa: E402

logging.basicConfig(level=logging.INFO)
settings = Settings()
settings.baudrates = [115200]
fakes = {"/dev/ttyUSB0": FakeSim800(imei="861234567890123"), "/dev/ttyUSB1": FakeSim800(imei="861234567890456")}
db = Database(settings.db_path)
bus = EventBus()
manager = ModemManager(settings, db, bus, open_port=lambda dev, baud: fakes[dev], discover=lambda: list(fakes))
app = create_app(settings, db, bus, manager)

INCOMING = [
    ("+31641600986", "How are you?"),
    ("+491511234567", "Your verification code is 482913. It expires in 10 minutes."),
    ("+4915221987654", "Paket 00340434161234567890 wird heute zwischen 14:00 und 16:00 zugestellt."),
    ("+447700900123", "Reminder: dentist appointment tomorrow at 09:30. Reply STOP to opt out."),
    ("+491701112233", "Bin in 10 Minuten da, wartest du unten? 🙂"),
    ("+33612345678", "Temperature alarm: server room at 31.4 °C"),
]


def deliver_pdu(sender: str, text: str) -> str:
    now = datetime.now(timezone.utc)
    ts = bytes(int(f"{v % 100:02d}"[::-1], 16) for v in (now.year, now.month, now.day, now.hour, now.minute, now.second)) + b"\x00"
    septets = gsm7_encode(text)
    if septets is not None:
        dcs, ud, udl = 0x00, pack7(septets), len(septets)
    else:
        ud = text.encode("utf-16-be")
        dcs, udl = 0x08, len(ud)
    return (b"\x00\x04" + encode_address(sender) + bytes([0x00, dcs]) + ts + bytes([udl]) + ud).hex().upper()


def traffic():
    interval = float(os.environ.get("DEMO_INTERVAL", "30"))
    time.sleep(3)
    for i, (sender, text) in enumerate(INCOMING[:3]):
        list(fakes.values())[i % 2].deliver_sms(deliver_pdu(sender, text))
        time.sleep(0.5)
    fakes["/dev/ttyUSB0"].ring("+491701112233")
    for i, (sender, text) in enumerate(itertools.cycle(INCOMING[3:])):
        time.sleep(interval)
        list(fakes.values())[i % 2].deliver_sms(deliver_pdu(sender, text))


threading.Thread(target=traffic, daemon=True).start()
uvicorn.run(app, host=settings.host, port=settings.port)
