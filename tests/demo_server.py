"""Run the app against simulated SIM800C modems (no hardware): python tests/demo_server.py"""
import logging
import os
import sys
import threading
import time

import uvicorn

sys.path.insert(0, os.path.dirname(__file__))
from fake_sim800 import FakeSim800  # noqa: E402

from smsmonster.api import create_app  # noqa: E402
from smsmonster.config import Settings  # noqa: E402
from smsmonster.db import Database  # noqa: E402
from smsmonster.events import EventBus  # noqa: E402
from smsmonster.manager import ModemManager  # noqa: E402

logging.basicConfig(level=logging.INFO)
settings = Settings()
settings.baudrates = [115200]
fakes = {"/dev/ttyUSB0": FakeSim800(imei="861234567890123"), "/dev/ttyUSB1": FakeSim800(imei="861234567890456")}
db = Database(settings.db_path)
bus = EventBus()
manager = ModemManager(settings, db, bus, open_port=lambda dev, baud: fakes[dev], discover=lambda: list(fakes))
app = create_app(settings, db, bus, manager)


def traffic():
    pdu = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"
    time.sleep(3)
    fakes["/dev/ttyUSB0"].deliver_sms(pdu)
    while True:
        time.sleep(float(os.environ.get("DEMO_INTERVAL", "30")))
        fakes["/dev/ttyUSB1"].deliver_sms(pdu)


threading.Thread(target=traffic, daemon=True).start()
uvicorn.run(app, host=settings.host, port=settings.port)
