"""
Phase 2 — Wire frame encoder / decoder.

Wire format (bytes on USB-CDC or BLE):
  [SOF 0xAA] [LEN] [payload: LEN bytes] [CRC8]

Payload:
  byte 0: MAGIC 0x4E
  byte 1: sequence number (wraps at 255)
  byte 2: segment count N
  byte 3: global brightness (0–255)
  bytes 4 .. 4+N*4-1: N segment records, 4 bytes each:
    byte 0: state  (notify.state.State)
    byte 1: hue    (0–254 HSV hue; 255 = white)
    byte 2: anim   (notify.state.Anim)
    byte 3: span   (LED count; 0 = auto-even)
"""
from __future__ import annotations

from dataclasses import dataclass

from notify.state import Anim, State, STATE_STYLE

FRAME_SOF   = 0xAA
FRAME_MAGIC = 0x4E
MAX_SEGS    = 16
MAX_TITLE   = 24   # per-segment v2 title cap (bytes) — matches the device (nsn_proto.h)

# nsn PROTOCOL v2 (backward-compatible, same MAGIC): after the base N-segment block the
# payload may carry an OPTIONAL per-segment extension so the e-ink can NAME a session —
# [harness:1][titleLen:1][title: titleLen bytes] repeated for the first N segments. A v1
# decoder ignores these trailing bytes (still CRC-covered), so old firmware still works;
# the broker only appends the extension when a segment carries harness/title, so plain v1
# frames stay byte-identical. Emit v2 only to devices that advertised protoVer >= 2.
HARNESS_CODE = {"claude": 1, "codex": 2, "vibe": 3}   # 0 = unknown


@dataclass
class FrameSegment:
    state: State
    hue:   int    # 0–255
    anim:  Anim
    span:  int    # 0 = auto
    harness: int = 0   # v2: 0=unknown, 1=claude, 2=codex, 3=vibe (HARNESS_CODE)
    title: str = ""    # v2: short session title (cwd basename / task); truncated to MAX_TITLE

    @classmethod
    def from_state(cls, state: State, span: int = 0) -> "FrameSegment":
        hue, anim = STATE_STYLE[state]
        return cls(state=state, hue=hue, anim=anim, span=span)


def _crc8(data: bytes) -> int:
    """CRC-8/MAXIM (polynomial 0x31, init 0x00, refin/refout True)."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if (crc & 0x01) else (crc >> 1)
    return crc


def _truncate_utf8(s: str, maxbytes: int) -> bytes:
    """UTF-8-encode `s` and cap at `maxbytes`, backing off to a whole codepoint so the
    wire never carries a half-encoded multibyte char (the device copies raw bytes into a
    char[] with no validation, so a split codepoint renders as a garbage glyph)."""
    b = s.encode("utf-8")[:maxbytes]
    while b:
        try:
            b.decode("utf-8")
            return b
        except UnicodeDecodeError:
            b = b[:-1]
    return b


def encode_frame(segments: list[FrameSegment], brightness: int, seq: int) -> bytes:
    """Return a complete framed packet ready to write to the transport."""
    n = min(len(segments), MAX_SEGS)
    payload = bytes([
        FRAME_MAGIC,
        seq & 0xFF,
        n,
        brightness & 0xFF,
    ])
    segs = segments[:n]
    for seg in segs:
        payload += bytes([
            int(seg.state) & 0xFF,
            seg.hue & 0xFF,
            int(seg.anim) & 0xFF,
            seg.span & 0xFF,
        ])
    # v2 extension: per-segment [harness][titleLen][title], only when a segment carries
    # harness/title (so a plain v1 frame is byte-identical). Titles are UTF-8, capped.
    if any(seg.harness or seg.title for seg in segs):
        # The whole payload must fit the 1-byte LEN field (<=255) — else bytes([...])
        # raises ValueError and the push/TTL task dies (the device encoder guards this
        # with `payloadLen > 255 return 0`; the broker must match). Budget title bytes
        # greedily: fixed v2 overhead is the base block already in `payload` plus 2
        # header bytes per segment; include a title only while it keeps the total <=255,
        # otherwise emit harness with an empty title (the device degrades to harness-only,
        # never crashes). With MAX_SEGS=16 the fixed overhead is 100 bytes, so titles
        # always have >=155 bytes of headroom and this can never overflow.
        budget = 255 - (len(payload) + n * 2)
        for seg in segs:
            t = _truncate_utf8(seg.title, MAX_TITLE)   # codepoint-safe cap
            if len(t) > budget:
                t = b""                                 # drop this title to stay <=255
            budget -= len(t)
            payload += bytes([seg.harness & 0xFF, len(t)]) + t
    crc = _crc8(payload)
    return bytes([FRAME_SOF, len(payload) & 0xFF]) + payload + bytes([crc])


@dataclass
class DecodedFrame:
    seq:        int
    brightness: int
    segments:   list[FrameSegment]


def decode_frame(packet: bytes) -> DecodedFrame | None:
    """Parse a raw packet (including SOF / LEN / CRC wrapper).  Returns None on error."""
    if len(packet) < 6:
        return None
    if packet[0] != FRAME_SOF:
        return None
    length = packet[1]
    if len(packet) < 2 + length + 1:
        return None
    payload = packet[2 : 2 + length]
    crc     = packet[2 + length]
    if _crc8(payload) != crc:
        return None
    if payload[0] != FRAME_MAGIC:
        return None

    n          = payload[2]
    brightness = payload[3]
    segments: list[FrameSegment] = []
    for i in range(n):
        off = 4 + i * 4
        if off + 4 > len(payload):
            break
        segments.append(FrameSegment(
            state=State(payload[off]),
            hue=payload[off + 1],
            anim=Anim(payload[off + 2]),
            span=payload[off + 3],
        ))
    # v2 extension: optional per-segment [harness][titleLen][title] after the base block.
    toff = 4 + n * 4
    for seg in segments:
        if toff + 2 > len(payload):
            break
        harness = payload[toff]
        tl = payload[toff + 1]
        toff += 2
        if toff + tl > len(payload):
            break
        seg.harness = harness
        seg.title = payload[toff : toff + tl].decode("utf-8", "replace")
        toff += tl
    return DecodedFrame(seq=payload[1], brightness=brightness, segments=segments)
