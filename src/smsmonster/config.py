"""Runtime configuration from environment variables (prefix SMSMONSTER_)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"SMSMONSTER_{name}", default)


def _bool(name: str, default: bool) -> bool:
    v = _env(name, "").strip().lower()
    return default if v == "" else v in ("1", "true", "yes", "on")


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("PORT", "8080")))
    data_dir: str = field(default_factory=lambda: _env("DATA_DIR", "./data"))
    devices: str = field(default_factory=lambda: _env("DEVICES", "auto"))
    baudrates: list[int] = field(default_factory=lambda: [int(b) for b in _env("BAUDRATES", "115200,9600,57600,38400").split(",") if b.strip()])
    sim_pin: str = field(default_factory=lambda: _env("SIM_PIN", ""))
    api_token: str = field(default_factory=lambda: _env("API_TOKEN", ""))
    trust_ingress: bool = field(default_factory=lambda: _bool("TRUST_INGRESS", False))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "info"))
    status_interval: float = field(default_factory=lambda: float(_env("STATUS_INTERVAL", "30")))
    sweep_interval: float = field(default_factory=lambda: float(_env("SWEEP_INTERVAL", "120")))
    scan_interval: float = field(default_factory=lambda: float(_env("SCAN_INTERVAL", "10")))
    send_retries: int = field(default_factory=lambda: int(_env("SEND_RETRIES", "3")))
    hangup_calls: bool = field(default_factory=lambda: _bool("HANGUP_CALLS", True))
    delete_from_sim: bool = field(default_factory=lambda: _bool("DELETE_FROM_SIM", True))
    delivery_reports: bool = field(default_factory=lambda: _bool("DELIVERY_REPORTS", False))
    part_timeout: float = field(default_factory=lambda: float(_env("PART_TIMEOUT", "600")))
    max_incoming_per_min: int = field(default_factory=lambda: int(_env("MAX_INCOMING_PER_MIN", "120")))
    ha_integration: bool = field(default_factory=lambda: _bool("HA_INTEGRATION", True))
    ha_url: str = field(default_factory=lambda: _env("HA_URL", ""))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN", ""))
    ha_resync_interval: float = field(default_factory=lambda: float(_env("HA_RESYNC_INTERVAL", "60")))
    supervisor_token: str = field(default_factory=lambda: os.environ.get("SUPERVISOR_TOKEN", ""))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "smsmonster.db")

    def homeassistant(self) -> tuple[str, str] | None:
        """(api url, token): explicit SMSMONSTER_HA_URL/TOKEN first, else the Supervisor proxy when running as an add-on."""
        if not self.ha_integration:
            return None
        if self.ha_url and self.ha_token:
            return self.ha_url, self.ha_token
        if self.supervisor_token:
            return "http://supervisor/core/api", self.supervisor_token
        return None

    def device_list(self) -> list[str] | None:
        v = self.devices.strip()
        if not v or v.lower() == "auto":
            return None
        return [d.strip() for d in v.split(",") if d.strip()]
