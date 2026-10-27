import time

import pytest

from smsmonster.config import Settings
from smsmonster.db import Database
from smsmonster.events import EventBus


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.baudrates = [115200]
    s.status_interval = 1.0
    s.sweep_interval = 0.5
    s.scan_interval = 0.2
    s.part_timeout = 1.0
    return s


@pytest.fixture
def db(settings):
    d = Database(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def bus():
    return EventBus()


def wait_for(cond, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        res = cond()
        if res:
            return res
        time.sleep(interval)
    raise AssertionError("condition not met in time")
