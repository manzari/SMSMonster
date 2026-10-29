"""SMS PDU codec (GSM 03.40): SMS-DELIVER decode, SMS-SUBMIT encode, GSM 7-bit / UCS-2, concatenation."""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = {0x0A: "\f", 0x14: "^", 0x28: "{", 0x29: "}", 0x2F: "\\", 0x3C: "[", 0x3D: "~", 0x3E: "]", 0x40: "|", 0x65: "€"}
_BASIC_MAP = {c: i for i, c in enumerate(GSM7_BASIC)}
_EXT_MAP = {c: i for i, c in GSM7_EXT.items()}
ESC = 0x1B


class PduError(ValueError):
    pass


@dataclass
class Concat:
    ref: int
    total: int
    seq: int


@dataclass
class DecodedSms:
    kind: str  # deliver | submit | status_report | unknown
    number: str = ""
    text: str = ""
    timestamp: datetime | None = None
    concat: Concat | None = None
    alphabet: str = "gsm7"
    raw: str = ""
    extra: dict = field(default_factory=dict)


# ---------- 7-bit helpers ----------

def gsm7_encode(text: str) -> list[int] | None:
    out: list[int] = []
    for ch in text:
        if ch in _BASIC_MAP:
            out.append(_BASIC_MAP[ch])
        elif ch in _EXT_MAP:
            out.extend((ESC, _EXT_MAP[ch]))
        else:
            return None
    return out


def gsm7_decode(septets: list[int]) -> str:
    out = []
    esc = False
    for s in septets:
        if esc:
            out.append(GSM7_EXT.get(s, " "))
            esc = False
        elif s == ESC:
            esc = True
        else:
            out.append(GSM7_BASIC[s & 0x7F])
    return "".join(out)


def pack7(septets: list[int], padding_bits: int = 0) -> bytes:
    acc = 0
    for i, s in enumerate(septets):
        acc |= (s & 0x7F) << (7 * i + padding_bits)
    nbits = 7 * len(septets) + padding_bits
    return acc.to_bytes((nbits + 7) // 8, "little") if nbits else b""


def unpack7(data: bytes, count: int, padding_bits: int = 0) -> list[int]:
    acc = int.from_bytes(data, "little") >> padding_bits
    return [(acc >> (7 * i)) & 0x7F for i in range(count)]


# ---------- address / timestamp ----------

def _swap_nibbles_str(raw: bytes) -> str:
    return "".join(f"{b & 0x0F:X}{b >> 4:X}" for b in raw)


def encode_address(number: str) -> bytes:
    number = number.strip().replace(" ", "").replace("-", "")
    toa = 0x81
    if number.startswith("+"):
        toa = 0x91
        number = number[1:]
    if not (number.isascii() and number.isdigit() and len(number) <= 20):
        raise PduError(f"invalid number: {number[:32]!r}")
    digits = number + ("F" if len(number) % 2 else "")
    raw = bytes(int(digits[i + 1] + digits[i], 16) for i in range(0, len(digits), 2))
    return bytes([len(number), toa]) + raw


def decode_address(data: bytes, pos: int) -> tuple[str, int]:
    ln = data[pos]
    toa = data[pos + 1]
    nbytes = (ln + 1) // 2
    raw = data[pos + 2 : pos + 2 + nbytes]
    if toa & 0x70 == 0x50:  # alphanumeric
        text = gsm7_decode(unpack7(raw, ln * 4 // 7))
        return text, pos + 2 + nbytes
    digits = _swap_nibbles_str(raw)[:ln]
    if toa & 0x70 == 0x10:
        digits = "+" + digits
    return digits, pos + 2 + nbytes


def decode_timestamp(raw: bytes) -> datetime | None:
    try:
        vals = [(b & 0x0F) * 10 + (b >> 4) for b in raw[:6]]
        tzb = raw[6]
        quarters = (tzb & 0x07) * 10 + (tzb >> 4)
        sign = -1 if tzb & 0x08 else 1
        tz = timezone(timedelta(minutes=15 * quarters * sign))
        return datetime(2000 + vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], tzinfo=tz)
    except (ValueError, IndexError):
        return None


# ---------- decode ----------

def _alphabet_from_dcs(dcs: int) -> str:
    if dcs & 0xC0 == 0x00 or dcs & 0xF0 in (0xC0, 0xD0, 0xE0):
        if dcs & 0xF0 == 0xE0:
            return "ucs2"
        if dcs & 0xF0 in (0xC0, 0xD0):
            return "gsm7"
        return ("gsm7", "8bit", "ucs2", "gsm7")[(dcs >> 2) & 3]
    if dcs & 0xF0 == 0xF0:
        return "8bit" if dcs & 0x04 else "gsm7"
    return "gsm7"


def _parse_udh(udh: bytes) -> Concat | None:
    i = 0
    concat = None
    while i + 1 < len(udh):
        iei, iel = udh[i], udh[i + 1]
        ie = udh[i + 2 : i + 2 + iel]
        if iei == 0x00 and iel == 3:
            concat = Concat(ie[0], ie[1], ie[2])
        elif iei == 0x08 and iel == 4:
            concat = Concat((ie[0] << 8) | ie[1], ie[2], ie[3])
        i += 2 + iel
    return concat


def decode_user_data(ud: bytes, udl: int, dcs: int, has_udh: bool) -> tuple[str, str, Concat | None]:
    alphabet = _alphabet_from_dcs(dcs)
    concat = None
    udhl = 0
    if has_udh and ud:
        udhl = ud[0]
        concat = _parse_udh(ud[1 : 1 + udhl])
        body = ud[1 + udhl :]
    else:
        body = ud
    if alphabet == "gsm7":
        hdr_septets = math.ceil((udhl + 1) * 8 / 7) if has_udh else 0
        padding = hdr_septets * 7 - (udhl + 1) * 8 if has_udh else 0
        text = gsm7_decode(unpack7(body, max(udl - hdr_septets, 0), padding))
    elif alphabet == "ucs2":
        n = udl - (udhl + 1 if has_udh else 0)
        text = body[:n].decode("utf-16-be", errors="replace")
    else:
        n = udl - (udhl + 1 if has_udh else 0)
        text = body[:n].decode("latin-1", errors="replace")
    return text, alphabet, concat


def decode_pdu(hexstr: str) -> DecodedSms:
    hexstr = hexstr.strip()
    try:
        data = bytes.fromhex(hexstr)
    except ValueError as e:
        raise PduError(f"bad hex: {e}") from e
    if len(data) < 2:
        raise PduError("pdu too short")
    pos = 1 + data[0]
    try:
        fo = data[pos]
        pos += 1
        mti = fo & 0x03
        has_udh = bool(fo & 0x40)
        res = DecodedSms(kind="unknown", raw=hexstr)
        if mti == 0:
            res.kind = "deliver"
            res.number, pos = decode_address(data, pos)
            dcs = data[pos + 1]
            pos += 2
            res.timestamp = decode_timestamp(data[pos : pos + 7])
            pos += 7
        elif mti == 1:
            res.kind = "submit"
            pos += 1  # MR
            res.number, pos = decode_address(data, pos)
            dcs = data[pos + 1]
            pos += 2
            vpf = (fo >> 3) & 0x03
            pos += {0: 0, 2: 1, 1: 7, 3: 7}[vpf]
        elif mti == 2:
            res.kind = "status_report"
            res.extra["mr"] = data[pos]
            pos += 1
            res.number, pos = decode_address(data, pos)
            res.timestamp = decode_timestamp(data[pos : pos + 7])
            res.extra["discharge"] = decode_timestamp(data[pos + 7 : pos + 14])
            res.extra["status"] = data[pos + 14]
            return res
        else:
            return res
        udl = data[pos]
        pos += 1
        res.text, res.alphabet, res.concat = decode_user_data(data[pos:], udl, dcs, has_udh)
        return res
    except IndexError as e:
        raise PduError("truncated pdu") from e


# ---------- encode ----------

def _split_septets(septets: list[int], size: int) -> list[list[int]]:
    chunks, cur = [], []
    i = 0
    while i < len(septets):
        unit = septets[i : i + 2] if septets[i] == ESC else septets[i : i + 1]
        if len(cur) + len(unit) > size:
            chunks.append(cur)
            cur = []
        cur.extend(unit)
        i += len(unit)
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def _split_ucs2(data: bytes, size: int) -> list[bytes]:
    chunks = []
    i = 0
    while i < len(data):
        end = min(i + size, len(data))
        if end < len(data) and 0xD8 <= data[end - 2] <= 0xDB:
            end -= 2
        chunks.append(data[i:end])
        i = end
    return chunks or [b""]


@dataclass
class EncodedPdu:
    hex: str
    tpdu_len: int
    seq: int
    total: int


def encode_submit(number: str, text: str, ref: int | None = None, request_status: bool = False) -> list[EncodedPdu]:
    addr = encode_address(number)
    septets = gsm7_encode(text)
    parts: list[tuple[bytes, int, int]] = []  # (user data without udh, udl contribution, dcs)
    if septets is not None:
        dcs = 0x00
        if len(septets) <= 160:
            chunks = [septets]
        else:
            chunks = _split_septets(septets, 153)
    else:
        dcs = 0x08
        try:
            data = text.encode("utf-16-be")
        except UnicodeEncodeError as e:
            raise PduError("text contains invalid characters") from e
        chunks = [data] if len(data) <= 140 else _split_ucs2(data, 134)
    total = len(chunks)
    multipart = total > 1
    if ref is None:
        ref = random.randint(0, 255)
    out = []
    for idx, chunk in enumerate(chunks, start=1):
        fo = 0x11 | (0x40 if multipart else 0) | (0x20 if request_status else 0)
        udh = bytes([0x05, 0x00, 0x03, ref & 0xFF, total, idx]) if multipart else b""
        if dcs == 0x00:
            padding = (7 - (len(udh) * 8) % 7) % 7 if multipart else 0
            body = pack7(chunk, padding)
            udl = len(chunk) + (math.ceil(len(udh) * 8 / 7) if multipart else 0)
        else:
            body = chunk
            udl = len(udh) + len(chunk)
        pdu = bytes([0x00, fo, 0x00]) + addr + bytes([0x00, dcs, 0xA7, udl]) + udh + body
        out.append(EncodedPdu(pdu.hex().upper(), len(pdu) - 1, idx, total))
    return out
