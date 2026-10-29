"""Hardening tests: auth boundary, API input validation, and a hostile modem."""
import asyncio
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from conftest import wait_for
from fake_sim800 import FakeSim800, opener
from smsmonster import main as main_mod
from smsmonster import pdu
from smsmonster.api import create_app
from smsmonster.manager import ModemManager
from smsmonster.modem import MAX_LINE_BYTES, MAX_PENDING_READS, ModemWorker, _log_safe
from smsmonster.webhooks import MAX_PENDING, WebhookDispatcher

INCOMING = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"
TOKEN = "s3cret-token"


def make_client(settings, db, bus, fake, **kw):
    manager = ModemManager(settings, db, bus, open_port=opener(fake), discover=lambda: ["/dev/fake0"])
    return TestClient(create_app(settings, db, bus, manager), **kw)


def start_worker(fake, settings, db, bus):
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    wait_for(lambda: w.ready)
    return w


# ---------------- auth boundary ----------------

def test_token_required_and_forwarded_headers_do_not_bypass(settings, db, bus):
    settings.api_token = TOKEN
    settings.trust_ingress = True
    with make_client(settings, db, bus, FakeSim800()) as c:
        assert c.get("/api/messages").status_code == 401
        assert c.get("/api/health").status_code == 200
        spoof = {"X-Forwarded-For": "172.30.32.2", "X-Ingress-Path": "/x", "X-Real-IP": "172.30.32.2"}
        assert c.get("/api/messages", headers=spoof).status_code == 401
        assert c.get("/api/messages", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
        assert c.get("/api/messages", headers={"X-API-Key": TOKEN}).status_code == 200
        assert c.get("/api/messages", headers={"Authorization": "Bearer sécrét".encode("latin-1")}).status_code == 401
        assert c.get("/api/messages", headers={"Authorization": "Bearer " + TOKEN[:-1]}).status_code == 401


def test_ingress_peer_is_trusted_only_when_enabled(settings, db, bus):
    settings.api_token = TOKEN
    settings.trust_ingress = True
    with make_client(settings, db, bus, FakeSim800(), client=("172.30.32.2", 4321)) as c:
        assert c.get("/api/messages", headers={"X-Ingress-Path": "/x"}).status_code == 200
        assert c.get("/api/messages").status_code == 401
    settings.trust_ingress = False
    with make_client(settings, db, bus, FakeSim800(), client=("172.30.32.2", 4321)) as c:
        assert c.get("/api/messages", headers={"X-Ingress-Path": "/x"}).status_code == 401


def test_query_token_only_for_event_stream(settings, db, bus):
    settings.api_token = TOKEN
    with make_client(settings, db, bus, FakeSim800()) as c:
        assert c.get(f"/api/messages?token={TOKEN}").status_code == 401
        with c.stream("GET", f"/api/events?token={TOKEN}") as s:
            assert s.status_code == 200
        with c.stream("GET", "/api/events?token=wrong") as s:
            assert s.status_code == 401


def test_server_does_not_trust_proxy_headers(monkeypatch):
    seen = {}
    monkeypatch.setattr(main_mod.uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(main_mod, "build_app", lambda: type("A", (), {"state": type("S", (), {"settings": main_mod.Settings()})()})())
    main_mod.main()
    assert seen.get("proxy_headers") is False and "forwarded_allow_ips" not in seen


def test_security_headers(settings, db, bus):
    with make_client(settings, db, bus, FakeSim800()) as c:
        r = c.get("/api/health")
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
        assert "default-src 'self'" in c.get("/").headers["content-security-policy"]


# ---------------- API input validation ----------------

@pytest.fixture
def client(settings, db, bus):
    fake = FakeSim800()
    with make_client(settings, db, bus, fake) as c:
        wait_for(lambda: c.get("/api/modems").json() and c.get("/api/modems").json()[0]["status"] == "ready")
        c.fake = fake
        yield c


def test_json_endpoints_need_json_content_type(client):
    body = json.dumps({"to": "+31641600986", "text": "csrf"})
    assert client.post("/api/messages", content=body, headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/messages", content=body).status_code == 415
    assert client.post("/api/messages", content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/messages", content=b"[]", headers={"Content-Type": "application/json"}).status_code == 400
    assert client.get("/api/messages?direction=out").json() == []


def test_body_size_limit(client):
    big = json.dumps({"to": "+1", "text": "x", "pad": "a" * 70000})
    assert client.post("/api/messages", content=big, headers={"Content-Type": "application/json"}).status_code == 413


def test_send_rejects_bad_numbers_and_text(client):
    for to in ["abc", "²", "+", "", "1" * 21, "+49 170 <script>", "٣٣٣"]:
        assert client.post("/api/messages", json={"to": to, "text": "x"}).status_code == 400, to
    assert client.post("/api/messages", content=b'{"to": "+1", "text": "\\ud800"}', headers={"Content-Type": "application/json"}).status_code == 400
    assert client.post("/api/messages", json={"to": "+49 170-1234567", "text": "ok"}).status_code == 202
    assert client.get("/api/messages?direction=out").json()[0]["number"] == "+491701234567"


def test_at_command_and_timeout_validation(client):
    mid = "861234567890123"
    for bad in [{"command": "AT\rATD123;"}, {"command": "ATE0", "timeout": "nan"}, {"command": "ATE0", "timeout": "x"}, {"command": "A" * 600}]:
        assert client.post(f"/api/modems/{mid}/at", json=bad).status_code == 400, bad
    t0 = time.monotonic()
    r = client.post(f"/api/modems/{mid}/at", json={"command": "AT", "timeout": 1e12})
    assert r.status_code == 200 and r.json()["response"] == [] and time.monotonic() - t0 < 10


def test_ussd_code_validation(client):
    mid = "861234567890123"
    assert client.post(f"/api/modems/{mid}/ussd", json={"code": '*100#",15;ATD+49900'}).status_code == 400
    assert client.post(f"/api/modems/{mid}/ussd", json={"code": "*100#"}).status_code == 200


def test_webhook_url_validated_on_patch(client):
    hid = client.post("/api/webhooks", json={"url": "http://example.invalid/h"}).json()["id"]
    assert client.patch(f"/api/webhooks/{hid}", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.patch(f"/api/webhooks/{hid}", json={"url": "https://example.invalid/h2"}).status_code == 200


# ---------------- hostile modem ----------------

def test_overlong_line_is_bounded_and_recovered(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        opened = fake.opened
        with fake._lock:
            fake._rx += b"A" * (MAX_LINE_BYTES * 3)
        wait_for(lambda: fake.opened > opened, timeout=10)
        assert len(w._buf) <= MAX_LINE_BYTES + 4096
        wait_for(lambda: w.ready, timeout=20)
    finally:
        w.stop()
        w.join(3)


def test_oversized_response_disconnects_instead_of_hanging(settings, db, bus):
    class Chatty(FakeSim800):
        def _handle(self, cmd):
            if cmd.upper() == "AT+CSQ":
                self._out(*["+CSQ: 1,0"] * 6000, "OK")
            else:
                super()._handle(cmd)

    fake = Chatty()
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    try:
        wait_for(lambda: "oversized" in (w.state.last_error or ""), timeout=10)
        wait_for(lambda: fake.opened >= 2, timeout=15)
    finally:
        w.stop()
        w.join(3)


def test_cmti_flood_does_not_starve_jobs(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        stop = threading.Event()

        def flood():
            i = 0
            while not stop.is_set():
                i += 1
                fake.push(f'+CMTI: "SM",{i}')
                time.sleep(0.001)

        t = threading.Thread(target=flood, daemon=True)
        t.start()
        time.sleep(0.5)
        fut = w.submit(lambda m: m.job_at("AT"))
        assert fut.result(timeout=5) == []
        assert len(w._pending_read) <= MAX_PENDING_READS
        stop.set()
        t.join(1)
        w.stop()
        w.join(3)
        assert not w.is_alive()
    finally:
        w.stop()


def test_incoming_rate_limit_leaves_excess_on_sim(settings, db, bus):
    settings.max_incoming_per_min = 5
    settings.sweep_interval = 100
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        for _ in range(12):
            fake.deliver_sms(INCOMING)
        wait_for(lambda: len(db.list_messages(direction="in")) == 5)
        time.sleep(0.5)
        assert len(db.list_messages(direction="in")) == 5
        assert len(fake.inbox) == 7
        assert "throttling" in w.state.last_error
        fake.ring("+4915112345678")
        time.sleep(0.3)
        assert db.list_messages(kind="call") == []
        assert "ATH" in fake.commands
    finally:
        w.stop()
        w.join(3)


def test_bogus_imei_is_rejected(settings, db, bus):
    fake = FakeSim800(imei="86²34567890123")
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    try:
        wait_for(lambda: "IMEI" in (w.state.last_error or ""))
        time.sleep(0.3)
        assert not w.ready and w.state.id == "fake0"
        assert db.list_modems() == []
    finally:
        w.stop()
        w.join(3)


def test_modem_strings_are_sanitized(settings, db, bus):
    class Nasty(FakeSim800):
        def _handle(self, cmd):
            if cmd.upper() == "AT+CGMM":
                self._out("SIM\x1b[31m800" + "X" * 200, "OK")
            elif cmd.upper() == "AT+COPS?":
                self._out('+COPS: 0,0,"Tele\x07kom"', "OK")
            else:
                super()._handle(cmd)

    w = start_worker(Nasty(), settings, db, bus)
    try:
        assert w.state.model == "SIM[31m800" + "X" * 54
        wait_for(lambda: w.state.operator == "Telekom")
    finally:
        w.stop()
        w.join(3)


def test_unsendable_message_fails_without_worker_crash(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        db.insert_message(direction="out", modem_id=None, number="²", text="x", status="queued")
        msg = wait_for(lambda: (lambda m: m if m["status"] == "failed" else None)(db.list_messages(direction="out")[0]))
        assert msg["attempts"] == 1 and "invalid number" in msg["error"]
        assert w.ready and fake.opened == 1 and fake.sent == []
    finally:
        w.stop()
        w.join(3)


def test_pdu_encoder_rejects_unicode_digits_and_long_numbers():
    for n in ["٣٣٣", "²", "1" * 21, "+" + "1" * 21]:
        with pytest.raises(pdu.PduError):
            pdu.encode_submit(n, "x")
    assert pdu.encode_submit("+" + "1" * 20, "x")
    with pytest.raises(pdu.PduError):
        pdu.encode_submit("+1", "\ud800")


def test_log_safe_strips_control_sequences():
    out = _log_safe("a\nb\x1b[31mred\x00")
    assert "\n" not in out and "\x1b" not in out and out == "a\\x0ab\\x1b[31mred\\x00"


async def test_webhook_backlog_is_bounded(db):
    db.add_webhook("http://example.invalid/h", ["*"], None)
    d = WebhookDispatcher(db)
    gate = asyncio.Event()

    async def slow(hook, event, data):
        await gate.wait()

    d._deliver = slow
    for _ in range(MAX_PENDING + 50):
        d("message.received", {"n": 1})
    await asyncio.sleep(0)
    assert d._pending == MAX_PENDING
    gate.set()
    await asyncio.sleep(0.05)
    assert d._pending == 0
    await d.close()
