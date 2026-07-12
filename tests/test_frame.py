"""Tests for the frame encoder / decoder (no hardware required)."""
from notify.broker.frame import FrameSegment, decode_frame, encode_frame
from notify.state import Anim, State


def _roundtrip(segments, brightness=30, seq=1):
    packet  = encode_frame(segments, brightness, seq)
    decoded = decode_frame(packet)
    assert decoded is not None, "decode_frame returned None"
    return decoded


def test_empty_frame_roundtrip():
    d = _roundtrip([])
    assert d.segments == []
    assert d.brightness == 30
    assert d.seq == 1


def test_single_segment_roundtrip():
    seg = FrameSegment(state=State.Running, hue=170, anim=Anim.Comet, span=0)
    d   = _roundtrip([seg])
    assert len(d.segments) == 1
    s = d.segments[0]
    assert s.state == State.Running
    assert s.hue   == 170
    assert s.anim  == Anim.Comet
    assert s.span  == 0


def test_from_state_uses_style_table():
    seg = FrameSegment.from_state(State.AwaitingApproval)
    assert seg.anim == Anim.Blink   # per STATE_STYLE
    assert seg.hue  == 32           # amber


def test_max_segments_roundtrip():
    segs = [FrameSegment.from_state(State.Running)] * 16
    d    = _roundtrip(segs)
    assert len(d.segments) == 16


def test_crc_corruption_detected():
    packet = bytearray(encode_frame([FrameSegment.from_state(State.Done)], 30, 0))
    packet[-1] ^= 0xFF   # corrupt CRC byte
    assert decode_frame(bytes(packet)) is None


def test_seq_wraps():
    d = _roundtrip([], seq=255)
    assert d.seq == 255
    d = _roundtrip([], seq=256)
    assert d.seq == 0   # encoded as & 0xFF


def test_brightness_roundtrip():
    d = _roundtrip([], brightness=128)
    assert d.brightness == 128


def test_v2_payload_capped_at_255_no_valueerror():
    # 16 sessions with long titles would blow past a 255-byte payload -> the old code
    # raised ValueError at bytes([FRAME_SOF, len(payload)]). encode_frame must budget
    # titles to keep the payload <= 255 and never raise.
    from notify.broker.frame import MAX_SEGS
    segs = []
    for i in range(MAX_SEGS):
        s = FrameSegment.from_state(State.Running)
        s.harness = 2
        s.title = "verylongreponame_" + str(i) * 8  # ~24 bytes each
        segs.append(s)
    packet = encode_frame(segs, 100, 0)          # must not raise
    assert len(packet) - 3 <= 255                # payload (minus SOF+LEN+CRC) fits the LEN byte
    d = decode_frame(packet)
    assert d is not None and len(d.segments) == MAX_SEGS


def test_v2_title_utf8_truncation_is_codepoint_safe():
    # A multibyte codepoint straddling the MAX_TITLE byte boundary must not be split.
    s = FrameSegment.from_state(State.Running)
    s.harness = 1
    s.title = "a" * 23 + "é"     # 'é' = 2 bytes; byte 24 would cut it in half
    packet = encode_frame([s], 100, 0)
    d = decode_frame(packet)
    # The trailing partial 'é' is dropped, leaving valid UTF-8 (23 'a's), never a lone byte.
    assert d.segments[0].title == "a" * 23


def test_v1_frame_still_byte_identical_after_guard():
    # No harness/title -> no v2 extension emitted; a plain frame stays byte-locked.
    segs = [FrameSegment(state=State.Running, hue=170, anim=Anim.Comet, span=0)]
    packet = encode_frame(segs, 100, 7)
    d = decode_frame(packet)
    assert d.segments[0].harness == 0 and d.segments[0].title == ""
