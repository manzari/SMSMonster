from datetime import timedelta

from smsmonster import pdu

DELIVER_VECTOR = "07911326040000F0040B911346610089F60000208062917314080CC8F71D14969741F977FD07"


def test_decode_known_deliver():
    sms = pdu.decode_pdu(DELIVER_VECTOR)
    assert sms.kind == "deliver"
    assert sms.number == "+31641600986"
    assert sms.text == "How are you?"
    assert sms.timestamp is not None
    assert (sms.timestamp.year, sms.timestamp.month, sms.timestamp.day) == (2002, 8, 26)
    assert sms.concat is None


def test_timestamp_timezone():
    assert pdu.decode_timestamp(bytes.fromhex("42501210000080")).utcoffset() == timedelta(hours=2)
    assert pdu.decode_timestamp(bytes.fromhex("42501210000040")).utcoffset() == timedelta(hours=1)
    assert pdu.decode_timestamp(bytes.fromhex("42501210000048")).utcoffset() == timedelta(hours=-1)


def test_encode_known_submit():
    parts = pdu.encode_submit("+31641600986", "hello")
    assert len(parts) == 1
    assert parts[0].hex == "0011000B911346610089F60000A705E8329BFD06"
    assert parts[0].tpdu_len == 19


def test_roundtrip_gsm7_extension_chars():
    text = "Price: 5€ [ok] {x} ~ \\ | ^"
    parts = pdu.encode_submit("+491701234567", text)
    assert len(parts) == 1
    back = pdu.decode_pdu(parts[0].hex)
    assert back.kind == "submit"
    assert back.number == "+491701234567"
    assert back.text == text


def test_roundtrip_ucs2():
    text = "Grüße aus Köln 😀 — ok"
    parts = pdu.encode_submit("01701234567", text)
    assert len(parts) == 1
    back = pdu.decode_pdu(parts[0].hex)
    assert back.alphabet == "ucs2"
    assert back.text == text
    assert back.number == "01701234567"


def test_multipart_gsm7():
    text = "".join(chr(ord("a") + i % 26) for i in range(400))
    parts = pdu.encode_submit("+491701234567", text, ref=42)
    assert len(parts) == 3
    decoded = [pdu.decode_pdu(p.hex) for p in parts]
    assert all(d.concat and d.concat.ref == 42 and d.concat.total == 3 for d in decoded)
    assert [d.concat.seq for d in decoded] == [1, 2, 3]
    assert "".join(d.text for d in decoded) == text
    assert len(decoded[0].text) == 153


def test_multipart_ucs2_does_not_split_surrogates():
    text = "😀" * 60  # 240 bytes UCS-2
    parts = pdu.encode_submit("+491701234567", text)
    assert len(parts) == 2
    decoded = [pdu.decode_pdu(p.hex) for p in parts]
    assert "".join(d.text for d in decoded) == text
    assert "�" not in decoded[0].text


def test_alphanumeric_sender():
    # sender "Telekom" as alphanumeric address, text "Hi"
    septets = pdu.gsm7_encode("Telekom")
    packed = pdu.pack7(septets)
    addr = bytes([(len(septets) * 7 + 3) // 4, 0xD0]) + packed
    body = pdu.pack7(pdu.gsm7_encode("Hi"))
    tpdu = bytes([0x04]) + addr + bytes([0x00, 0x00]) + bytes.fromhex("42501210000000") + bytes([2]) + body
    sms = pdu.decode_pdu("00" + tpdu.hex())
    assert sms.number == "Telekom"
    assert sms.text == "Hi"


def test_bad_pdu_raises():
    try:
        pdu.decode_pdu("zz")
    except pdu.PduError:
        pass
    else:
        raise AssertionError("expected PduError")
    try:
        pdu.decode_pdu("0004")
    except pdu.PduError:
        pass
    else:
        raise AssertionError("expected PduError")


def test_invalid_number():
    try:
        pdu.encode_submit("abc", "x")
    except pdu.PduError:
        pass
    else:
        raise AssertionError("expected PduError")
