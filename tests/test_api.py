import json

import pytest
from starlette.testclient import TestClient

from conftest import wait_for
from fake_sim800 import FakeSim800, opener
from smsmonster.api import create_app
from smsmonster.manager import ModemManager

INCOMING = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"


@pytest.fixture
def fake():
    return FakeSim800()


@pytest.fixture
def client(settings, db, bus, fake):
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: ["/dev/fake0"])
    app = create_app(settings, db, bus, manager)
    with TestClient(app) as c:
        wait_for(lambda: c.get("/api/modems").json() and c.get("/api/modems").json()[0]["status"] == "ready")
        wait_for(lambda: c.get("/api/modems").json()[0]["registration"] == "home")
        yield c


def test_health_and_modems(client):
    h = client.get("/api/health").json()
    assert h["status"] == "ok" and h["modems"] == 1 and h["ready"] == 1 and h["auth"] is False
    modems = client.get("/api/modems").json()
    assert modems[0]["imei"] == "861234567890123"
    assert modems[0]["operator"] == "Telekom.de"
    one = client.get("/api/modems/861234567890123").json()
    assert one["id"] == "861234567890123"
    assert client.get("/api/modems/nope").status_code == 404


def test_patch_modem(client, db):
    r = client.patch("/api/modems/861234567890123", json={"name": "Office", "phone_number": "+4915112345678"})
    assert r.status_code == 200 and r.json()["name"] == "Office"
    assert db.get_modem("861234567890123")["phone_number"] == "+4915112345678"


def test_send_and_list(client, fake, db):
    r = client.post("/api/messages", json={"to": "+31641600986", "text": "hello"})
    assert r.status_code == 202
    mid = r.json()["id"]
    wait_for(lambda: client.get(f"/api/messages/{mid}").json()["status"] == "sent")
    assert fake.sent[0].endswith("E8329BFD06")
    assert client.get("/api/messages?direction=out").json()[0]["id"] == mid
    assert client.get("/api/messages?search=hell").json()[0]["id"] == mid
    assert client.get("/api/messages?direction=in").json() == []
    assert client.get("/api/stats").json()["sent"] == 1
    assert client.delete(f"/api/messages/{mid}").status_code == 204
    assert client.get(f"/api/messages/{mid}").status_code == 404


def test_send_validation(client):
    assert client.post("/api/messages", json={"text": "x"}).status_code == 400
    assert client.post("/api/messages", json={"to": "+1", "text": ""}).status_code == 400
    assert client.post("/api/messages", json={"to": "+1", "text": "x", "modem_id": "nope"}).status_code == 404
    assert client.post("/api/messages", content=b"not json").status_code == 400


def test_receive_and_sse(client, fake):
    with client.stream("GET", "/api/events") as stream:
        lines = stream.iter_lines()
        assert next(l for l in lines if l.startswith("event:")) == "event: hello"
        fake.deliver_sms(INCOMING)
        while True:
            line = next(lines)
            if line == "event: message.received":
                data = json.loads(next(lines)[len("data: "):])
                break
    assert data["text"] == "How are you?"
    rows = client.get("/api/messages?direction=in").json()
    assert rows[0]["number"] == "+31641600986"


def test_at_and_ussd_endpoints(client):
    r = client.post("/api/modems/861234567890123/at", json={"command": "AT+CSQ"})
    assert r.json()["response"] == ["+CSQ: 20,0"]
    assert client.post("/api/modems/861234567890123/at", json={"command": "rm -rf"}).status_code == 400
    r = client.post("/api/modems/861234567890123/ussd", json={"code": "*100#"})
    assert r.json()["text"] == "Your balance is 5.00 EUR"


def test_retry_failed(client, db):
    row = db.insert_message(direction="out", modem_id="861234567890123", number="+31641600986", text="x", status="failed", attempts=3)
    r = client.post(f"/api/messages/{row['id']}/retry")
    assert r.status_code == 200
    wait_for(lambda: client.get(f"/api/messages/{row['id']}").json()["status"] == "sent")


def test_webhooks_crud(client):
    r = client.post("/api/webhooks", json={"url": "http://example.invalid/hook", "events": ["message.received"], "secret": "s3"})
    assert r.status_code == 201
    wid = r.json()["id"]
    assert client.post("/api/webhooks", json={"url": "ftp://x", "events": ["message.received"]}).status_code == 400
    assert client.post("/api/webhooks", json={"url": "http://x", "events": ["nope"]}).status_code == 400
    listed = client.get("/api/webhooks").json()
    assert listed["webhooks"][0]["id"] == wid and "message.sent" in listed["events"]
    r = client.patch(f"/api/webhooks/{wid}", json={"enabled": False, "events": ["*"]})
    assert r.json()["enabled"] is False and r.json()["events"] == ["*"]
    assert client.delete(f"/api/webhooks/{wid}").status_code == 204
    assert client.delete(f"/api/webhooks/{wid}").status_code == 404


def test_auth(settings, db, bus, fake):
    settings.api_token = "secret-token"
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: [])
    app = create_app(settings, db, bus, manager)
    with TestClient(app) as c:
        assert c.get("/api/health").json() == {"status": "ok", "version": c.get("/api/health").json()["version"], "auth": True}
        assert c.get("/api/modems").status_code == 401
        assert c.get("/api/modems", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/api/health", headers={"Authorization": "Bearer secret-token"}).json()["modems"] == 0
        assert c.get("/api/health", headers={"X-API-Key": "secret-token"}).status_code == 200
        assert c.get("/api/health?token=secret-token").status_code == 200
        assert c.get("/").status_code == 200  # UI itself is public, it asks for the token
        assert c.post("/api/messages", json={"to": "+1", "text": "x"}, headers={"X-API-Key": "secret-token"}).status_code == 503


def test_ingress_bypass(settings, db, bus, fake):
    settings.api_token = "secret-token"
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: [])
    app = create_app(settings, db, bus, manager)
    with TestClient(app, client=("172.30.32.2", 1234)) as c:
        assert c.get("/api/modems").status_code == 401
        assert c.get("/api/modems", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}).status_code == 200
    app = create_app(settings, db, bus, ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: []))
    with TestClient(app, client=("10.0.0.5", 1234)) as c:
        assert c.get("/api/modems", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"}).status_code == 401


def test_discover_keeps_url_devices(tmp_path):
    from smsmonster.manager import discover_devices
    assert discover_devices(["socket://host:5555", str(tmp_path / "missing")]) == ["socket://host:5555"]
