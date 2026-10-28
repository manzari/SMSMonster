import time

from conftest import wait_for
from fake_sim800 import FakeSim800, opener
from smsmonster import pdu
from smsmonster.modem import ModemWorker

INCOMING = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"


def start_worker(fake, settings, db, bus):
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    wait_for(lambda: w.ready)
    return w


def test_init_and_status(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        st = w.state
        assert st.id == st.imei == "861234567890123"
        assert st.model == "SIMCOM_SIM800C"
        assert st.firmware == "1418B05SIM800C24"
        assert st.iccid == "89490200001234567890"
        assert st.phone_number == "+491701234567"
        wait_for(lambda: w.state.registration == "home")
        assert st.operator == "Telekom.de"
        assert st.rssi == 20 and st.signal_dbm == -73
        assert "AT+CMGF=0" in fake.commands
        assert any(c.startswith("AT+CNMI=2,1") for c in fake.commands)
        assert db.get_modem(st.imei)["model"] == "SIMCOM_SIM800C"
    finally:
        w.stop()
        w.join(3)
        assert fake.closed


def test_baudrate_probing(settings, db, bus):
    settings.baudrates = [115200, 9600]
    fake = FakeSim800(baudrate=9600)
    w = start_worker(fake, settings, db, bus)
    try:
        assert w.state.baudrate == 9600
        assert fake.opened == 2
    finally:
        w.stop()
        w.join(3)


def test_receive_sms_via_cmti(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        idx = fake.deliver_sms(INCOMING)
        msg = wait_for(lambda: (db.list_messages(direction="in") or [None])[0])
        assert msg["number"] == "+31641600986"
        assert msg["text"] == "How are you?"
        assert msg["status"] == "received"
        assert msg["modem_id"] == "861234567890123"
        assert msg["received_at"].startswith("2002-08-26T19:37:41")
        wait_for(lambda: idx in fake.deleted)
        assert w.state.counters["received"] == 1
    finally:
        w.stop()
        w.join(3)


def test_sweep_picks_up_stored_messages(settings, db, bus):
    fake = FakeSim800()
    fake.inbox[3] = INCOMING  # stored before we connected, no CMTI
    w = start_worker(fake, settings, db, bus)
    try:
        msg = wait_for(lambda: (db.list_messages(direction="in") or [None])[0])
        assert msg["text"] == "How are you?"
        wait_for(lambda: 3 in fake.deleted)
    finally:
        w.stop()
        w.join(3)


def test_receive_multipart(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        text = "A" * 200
        parts = pdu.encode_submit("+491701234567", text, ref=7)
        # convert submit PDUs into deliver PDUs: swap header/sender, keep UD
        for p in parts:
            raw = bytes.fromhex(p.hex)
            ud = raw[1 + 1 + 1 + 2 + 6 + 3:]  # after smsc, fo, mr, addr(2+6), pid, dcs, vp
            tpdu = bytes([0x44]) + pdu.encode_address("+491701234567") + bytes([0x00, 0x00]) + bytes.fromhex("42501210000000") + ud
            fake.deliver_sms("00" + tpdu.hex())
            time.sleep(0.1)
        msg = wait_for(lambda: (db.list_messages(direction="in") or [None])[0])
        assert msg["text"] == text
        assert msg["parts_total"] == 2 and msg["parts_received"] == 2
        assert len(db.list_messages(direction="in")) == 1
    finally:
        w.stop()
        w.join(3)


def test_incomplete_multipart_is_flushed_after_timeout(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        tpdu = bytes([0x44]) + pdu.encode_address("+491701234567") + bytes([0x00, 0x08]) + bytes.fromhex("42501210000000")
        udh = bytes([0x05, 0x00, 0x03, 9, 2, 1])
        body = "part one".encode("utf-16-be")
        tpdu += bytes([len(udh) + len(body)]) + udh + body
        fake.deliver_sms("00" + tpdu.hex())
        msg = wait_for(lambda: (db.list_messages(direction="in") or [None])[0], timeout=6)
        assert msg["text"] == "part one"
        assert msg["parts_total"] == 2 and msg["parts_received"] == 1
    finally:
        w.stop()
        w.join(3)


def test_send_sms(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        db.insert_message(direction="out", modem_id=None, number="+31641600986", text="hello", status="queued")
        msg = wait_for(lambda: (lambda m: m if m and m["status"] == "sent" else None)(db.list_messages(direction="out")[0]))
        assert fake.sent == ["0011000B911346610089F60000A705E8329BFD06"]
        assert msg["modem_id"] == "861234567890123"
        assert msg["reference"] == 1
        assert msg["sent_at"]
        assert w.state.counters["sent"] == 1
    finally:
        w.stop()
        w.join(3)


def test_send_multipart_unicode(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        text = "Grüße 😀 " * 20
        db.insert_message(direction="out", modem_id=None, number="+31641600986", text=text, status="queued")
        wait_for(lambda: db.list_messages(direction="out")[0]["status"] == "sent")
        assert len(fake.sent) == 3
        assert "".join(pdu.decode_pdu(p).text for p in fake.sent) == text
    finally:
        w.stop()
        w.join(3)


def test_send_failure_retries_then_fails(settings, db, bus):
    settings.send_retries = 2
    fake = FakeSim800(cms_error=331)
    w = start_worker(fake, settings, db, bus)
    try:
        w._next_send = 0
        db.insert_message(direction="out", modem_id=None, number="+31641600986", text="hello", status="queued")
        wait_for(lambda: db.list_messages(direction="out")[0]["attempts"] == 1)
        first = db.list_messages(direction="out")[0]
        assert first["status"] == "queued" and "331" in first["error"]
        w._next_send = 0  # skip the retry backoff
        msg = wait_for(lambda: (lambda m: m if m["status"] == "failed" else None)(db.list_messages(direction="out")[0]))
        assert msg["attempts"] == 2
        assert "no network service" in msg["error"]
        assert w.state.counters["failed"] == 1
    finally:
        w.stop()
        w.join(3)


def test_sim_pin_entered(settings, db, bus):
    settings.sim_pin = "1234"
    fake = FakeSim800(sim="SIM PIN", pin="1234")
    w = start_worker(fake, settings, db, bus)
    try:
        assert 'AT+CPIN="1234"' in fake.commands
    finally:
        w.stop()
        w.join(3)


def test_sim_pin_missing(settings, db, bus):
    fake = FakeSim800(sim="SIM PIN", pin="1234")
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    try:
        wait_for(lambda: w.state.status == "sim_pin_required")
        assert "SIM PIN" in w.state.last_error
    finally:
        w.stop()
        w.join(3)


def test_no_sim(settings, db, bus):
    fake = FakeSim800(sim="NONE")
    w = ModemWorker("/dev/fake0", settings, db, bus, open_port=opener(fake))
    w.start()
    try:
        wait_for(lambda: w.state.status == "no_sim")
    finally:
        w.stop()
        w.join(3)


def test_incoming_call_logged_and_hung_up(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        fake.ring("+491709999999")
        msg = wait_for(lambda: (db.list_messages(kind="call") or [None])[0])
        assert msg["number"] == "+491709999999" and msg["direction"] == "in"
        wait_for(lambda: "ATH" in fake.commands)
    finally:
        w.stop()
        w.join(3)


def test_jobs_at_and_ussd(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        assert w.submit(lambda m: m.job_at("AT+CSQ")).result(5) == ["+CSQ: 20,0"]
        assert w.submit(lambda m: m.job_at("AT+BOGUS")).result(5) == ["ERROR"]
        res = w.submit(lambda m: m.job_ussd("*100#")).result(5)
        assert res["text"] == "Your balance is 5.00 EUR" and res["status"] == 0
        fake.ussd_reply = '+CUSD: 0,"00480069",72'
        assert w.submit(lambda m: m.job_ussd("*100#")).result(5)["text"] == "Hi"
    finally:
        w.stop()
        w.join(3)


def test_reconnect_after_power_down(settings, db, bus):
    fake = FakeSim800()
    w = start_worker(fake, settings, db, bus)
    try:
        fake.push("NORMAL POWER DOWN")
        wait_for(lambda: w.state.status == "disconnected")
        wait_for(lambda: w.ready, timeout=10)
        assert fake.opened >= 2
    finally:
        w.stop()
        w.join(3)
