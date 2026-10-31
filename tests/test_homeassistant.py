"""Home Assistant integration against a fake HA REST API (httpx MockTransport)."""
import json

import httpx
import pytest
from starlette.testclient import TestClient

from conftest import wait_for
from fake_sim800 import FakeSim800, opener
from smsmonster.api import create_app
from smsmonster.homeassistant import message_entity, modem_entities, normalize_url, slug
from smsmonster.manager import ModemManager

INCOMING = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"
MODEM = "sensor.sms_monster_modem_861234567890123"


class FakeHA:
    def __init__(self, status=200):
        self.states: dict[str, dict] = {"sensor.sms_monster_modem_oldimei": {"state": "ready"}, "sensor.other": {"state": "x"}}
        self.events: list[tuple[str, dict]] = []
        self.auth: set[str] = set()
        self.status = status
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.auth.add(request.headers.get("authorization", ""))
        if self.status != 200:
            return httpx.Response(self.status)
        path = "/api/" + request.url.path.split("/api/", 1)[1]  # supervisor proxy prefixes /core
        if request.method == "GET" and path == "/api/states":
            return httpx.Response(200, json=[{"entity_id": e, **b} for e, b in self.states.items()])
        if path.startswith("/api/states/"):
            eid = path.rsplit("/", 1)[1]
            if request.method == "DELETE":
                return httpx.Response(200 if self.states.pop(eid, None) else 404)
            self.states[eid] = json.loads(request.content)
            return httpx.Response(200, json={"entity_id": eid})
        if request.method == "POST" and path.startswith("/api/events/"):
            self.events.append((path.rsplit("/", 1)[1], json.loads(request.content)))
            return httpx.Response(200, json={"message": "Event fired."})
        return httpx.Response(404)


@pytest.fixture
def ha():
    return FakeHA()


@pytest.fixture
def fake():
    return FakeSim800()


@pytest.fixture
def client(settings, db, bus, fake, ha, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supertoken")
    settings.supervisor_token = "supertoken"
    settings.ha_resync_interval = 0.3
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: ["/dev/fake0"])
    app = create_app(settings, db, bus, manager, ha_transport=ha.transport)
    with TestClient(app) as c:
        wait_for(lambda: c.get("/api/modems").json() and c.get("/api/modems").json()[0]["registration"] == "home")
        yield c


def test_settings_resolution(settings):
    settings.supervisor_token, settings.ha_url, settings.ha_token = "", "", ""
    assert settings.homeassistant() is None
    settings.supervisor_token = "s"
    assert settings.homeassistant() == ("http://supervisor/core/api", "s")
    settings.ha_url, settings.ha_token = "http://ha.local:8123", "t"
    assert settings.homeassistant() == ("http://ha.local:8123", "t")
    settings.ha_integration = False
    assert settings.homeassistant() is None


def test_helpers():
    assert normalize_url("http://ha.local:8123/") == "http://ha.local:8123/api"
    assert normalize_url("http://supervisor/core/api") == "http://supervisor/core/api"
    assert slug("Modem #1 (Büro)") == "modem_1_b_ro"
    ents = modem_entities({"id": "ttyUSB0", "device": "/dev/ttyUSB0", "status": "connecting", "counters": {"sent": 2}})
    assert ents[0][0] == "sensor.sms_monster_modem_ttyusb0" and ents[0][1]["attributes"]["sent_total"] == 2
    assert ents[1][1]["state"] == "unknown"
    eid, body = message_entity("message.received", {"id": 1, "number": "+49", "text": "hi", "received_at": "2024-01-01T10:00:00"})
    assert eid == "sensor.sms_monster_last_received" and body["state"] == "2024-01-01T10:00:00+00:00"
    assert message_entity("message.updated", {}) is None


def test_modem_pushed_and_stale_removed(client, ha):
    wait_for(lambda: ha.states.get(MODEM, {}).get("state") == "ready")
    assert ha.auth == {"Bearer supertoken"}
    m = ha.states[MODEM]
    assert m["attributes"]["operator"] == "Telekom.de" and m["attributes"]["imei"] == "861234567890123"
    assert m["attributes"]["friendly_name"] == "SMS Monster 861234567890123"
    sig = ha.states[MODEM + "_signal"]
    assert sig["attributes"]["unit_of_measurement"] == "%" and isinstance(sig["state"], int)
    wait_for(lambda: "sensor.sms_monster_modem_oldimei" not in ha.states)
    assert "sensor.other" in ha.states
    assert "sensor.sms_monster_modem_fake0" not in ha.states  # pre-identification id was replaced by the IMEI
    assert ha.states["sensor.sms_monster_modems"]["state"] == 1
    assert ha.states["sensor.sms_monster_modems"]["attributes"]["ready"] == 1
    assert client.get("/api/health").json()["home_assistant"] == "http://supervisor/core/api"


def test_modem_rename_updates_entity(client, ha):
    client.patch(f"/api/modems/861234567890123", json={"name": "Office"})
    wait_for(lambda: ha.states.get(MODEM, {}).get("attributes", {}).get("friendly_name") == "SMS Monster Office")


def test_incoming_sms_entity_and_event(client, fake, ha):
    fake.inbox[1] = INCOMING
    fake.push("+CMTI: \"SM\",1")
    wait_for(lambda: "sensor.sms_monster_last_received" in ha.states)
    ent = ha.states["sensor.sms_monster_last_received"]
    assert ent["attributes"]["text"] == "How are you?" and ent["attributes"]["device_class"] == "timestamp"
    assert ent["state"].endswith("+00:00") or "+" in ent["state"] or "-" in ent["state"][10:]
    wait_for(lambda: ha.states.get("sensor.sms_monster_received", {}).get("state") == 1)
    wait_for(lambda: any(e == "smsmonster_message_received" for e, _ in ha.events))
    data = [d for e, d in ha.events if e == "smsmonster_message_received"][0]
    assert data["text"] == "How are you?" and data["direction"] == "in"


def test_outgoing_sms_entity_and_event(client, ha):
    r = client.post("/api/messages", json={"to": "+31641600986", "text": "hello"})
    assert r.status_code == 202
    wait_for(lambda: ha.states.get("sensor.sms_monster_last_sent", {}).get("attributes", {}).get("text") == "hello")
    wait_for(lambda: ha.states.get("sensor.sms_monster_sent", {}).get("state") == 1)
    wait_for(lambda: any(e == "smsmonster_message_sent" for e, _ in ha.events))
    assert ha.states["sensor.sms_monster_queued"]["state"] == 0


def test_modem_removed_deletes_entities(client, ha):
    wait_for(lambda: MODEM in ha.states)
    assert client.post("/api/modems/861234567890123/ignore").status_code == 200
    wait_for(lambda: MODEM not in ha.states and MODEM + "_signal" not in ha.states)
    wait_for(lambda: any(e == "smsmonster_modem_removed" for e, _ in ha.events))
    wait_for(lambda: ha.states.get("sensor.sms_monster_modems", {}).get("state") == 0)


def test_ha_down_does_not_break_gateway(settings, db, bus, fake):
    ha = FakeHA(status=503)
    settings.ha_url, settings.ha_token = "http://ha.local:8123", "tok"
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: ["/dev/fake0"])
    app = create_app(settings, db, bus, manager, ha_transport=ha.transport)
    with TestClient(app) as c:
        wait_for(lambda: c.get("/api/modems").json() and c.get("/api/modems").json()[0]["status"] == "ready")
        r = c.post("/api/messages", json={"to": "+31641600986", "text": "hello"})
        assert r.status_code == 202
        wait_for(lambda: c.get(f"/api/messages/{r.json()['id']}").json()["status"] == "sent")
        assert c.get("/api/health").json()["home_assistant"] == "http://ha.local:8123/api"
