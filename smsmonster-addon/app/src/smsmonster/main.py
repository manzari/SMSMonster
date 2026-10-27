"""Entrypoint: `smsmonster` or `python -m smsmonster`."""
from __future__ import annotations

import logging
import os

import uvicorn

from .api import create_app
from .config import Settings
from .db import Database
from .events import EventBus
from .manager import ModemManager


def build_app():
    settings = Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(settings.data_dir, exist_ok=True)
    db = Database(settings.db_path)
    bus = EventBus()
    manager = ModemManager(settings, db, bus)
    app = create_app(settings, db, bus, manager)
    app.state.settings = settings
    return app


def main() -> None:
    app = build_app()
    s = app.state.settings
    uvicorn.run(app, host=s.host, port=s.port, log_level=s.log_level.lower(), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
