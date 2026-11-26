"""Frame envelope: golden vectors, decode errors, streaming decoder."""

from __future__ import annotations

import pytest

from aiochainway.protocol.errors import ChecksumError, FrameError, ValidationError
from aiochainway.protocol.frame import (
    MAX_DATA_LEN,
    MAX_FRAME_LEN,
    Frame,
    FrameDecoder,
    checksum,
    decode,
    encode,
)

STOP = bytes.fromhex("A55A00088C840D0A")
GET_WORK_MODE = bytes.fromhex("A55A0009A106AE0D0A")


def test_golden_stop_inventory() -> None:
    assert encode(0x8C) == STOP


def test_golden_get_work_mode() -> None:
    assert encode(0xA1, b"\x06") == GET_WORK_MODE


def test_checksum_covers_len_through_data() -> None:
    assert checksum(b"\x00\x09\xa1\x06") == 0xAE
    assert checksum(b"") == 0


def test_decode_golden() -> None:
    assert decode(STOP) == Frame(0x8C, b"")
    assert decode(GET_WORK_MODE) == Frame(0xA1, b"\x06")


def test_frame_helpers() -> None:
    frame = Frame(0xA2, b"\x06\x01")
    assert frame.sub == 0x06
    assert Frame(0x8C).sub is None
    assert frame.encode() == encode(0xA2, b"\x06\x01")
    assert str(frame) == "CMD=0xA2 DATA=06 01"
    assert str(Frame(0x8C)) == "CMD=0x8C DATA=-"


def test_length_is_big_endian() -> None:
    raw = encode(0x83, bytes(300))
    assert raw[2:4] == (308).to_bytes(2, "big")
    assert decode(raw).data == bytes(300)


@pytest.mark.parametrize("cmd", [-1, 256])
def test_encode_rejects_bad_cmd(cmd: int) -> None:
    with pytest.raises(ValidationError):
        encode(cmd)


def test_encode_max_data() -> None:
    assert len(encode(0x01, bytes(MAX_DATA_LEN))) == MAX_FRAME_LEN
    with pytest.raises(ValidationError):
        encode(0x01, bytes(MAX_DATA_LEN + 1))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"\xa5\x5a\x00\x08\x8c\x84\x0d", "too short"),
        (b"\xa5\x5b\x00\x08\x8c\x84\x0d\x0a", "header"),
        (b"\xa5\x5a\x00\x07\x8c\x84\x0d\x0a", "out of range"),
        (b"\xa5\x5a\x00\x09\x8c\x84\x0d\x0a", "says 9"),
        (b"\xa5\x5a\x00\x08\x8c\x84\x0d\x0b", "trailer"),
    ],
)
def test_decode_frame_errors(raw: bytes, message: str) -> None:
    with pytest.raises(FrameError, match=message):
        decode(raw)


def test_decode_rejects_oversize_len() -> None:
    raw = (
        b"\xa5\x5a" + (MAX_FRAME_LEN + 1).to_bytes(2, "big") + bytes(MAX_FRAME_LEN - 3)
    )
    with pytest.raises(FrameError, match="out of range"):
        decode(raw)


def test_decode_checksum_error() -> None:
    bad = STOP[:5] + b"\x00" + STOP[6:]
    with pytest.raises(ChecksumError) as info:
        decode(bad)
    assert info.value.expected == 0x84
    assert info.value.actual == 0x00
    assert isinstance(info.value, FrameError)


# Streaming decoder -------------------------------------------------------------


def test_decoder_byte_by_byte() -> None:
    dec = FrameDecoder()
    out: list[Frame] = []
    for b in GET_WORK_MODE + STOP:
        out += dec.feed(bytes((b,)))
    assert out == [Frame(0xA1, b"\x06"), Frame(0x8C)]
    assert dec.buffered == 0
    assert dec.frames == 2


def test_decoder_skips_garbage_and_counts() -> None:
    dec = FrameDecoder()
    out = dec.feed(b"\x00\x01\x02" + STOP + b"\xff" + GET_WORK_MODE)
    assert out == [Frame(0x8C), Frame(0xA1, b"\x06")]
    assert dec.discarded_bytes == 4
    assert dec.bad_frames == 0


def test_decoder_bad_xor_then_good_frame() -> None:
    dec = FrameDecoder()
    bad = STOP[:5] + b"\x00" + STOP[6:]
    assert dec.feed(bad + STOP) == [Frame(0x8C)]
    assert dec.bad_frames == 1


def test_decoder_bad_trailer() -> None:
    dec = FrameDecoder()
    bad = STOP[:-1] + b"\x00"
    assert dec.feed(bad + GET_WORK_MODE) == [Frame(0xA1, b"\x06")]
    assert dec.bad_frames == 1


def test_decoder_finds_frame_inside_rejected_candidate() -> None:
    # A bogus header claims 20 bytes; a real frame starts inside that span.
    bogus = b"\xa5\x5a\x00\x14"
    dec = FrameDecoder()
    out = dec.feed(bogus + STOP + bytes(20))
    assert out == [Frame(0x8C)]
    assert dec.bad_frames == 1


@pytest.mark.parametrize("length", [0, 7, MAX_FRAME_LEN + 1, 0xFFFF])
def test_decoder_rejects_bad_len(length: int) -> None:
    dec = FrameDecoder()
    out = dec.feed(b"\xa5\x5a" + length.to_bytes(2, "big") + STOP)
    assert out == [Frame(0x8C)]
    assert dec.bad_frames == 1


def test_decoder_custom_max_frame_len() -> None:
    dec = FrameDecoder(max_frame_len=16)
    big = encode(0x83, bytes(20))
    assert dec.feed(big + STOP) == [Frame(0x8C)]
    assert dec.bad_frames == 1


def test_decoder_keeps_trailing_header_byte() -> None:
    dec = FrameDecoder()
    assert dec.feed(b"\x00\x00\xa5") == []
    assert dec.buffered == 1
    assert dec.discarded_bytes == 2
    assert dec.feed(STOP[1:]) == [Frame(0x8C)]


def test_decoder_partial_waits() -> None:
    dec = FrameDecoder()
    assert dec.feed(GET_WORK_MODE[:3]) == []
    assert dec.buffered == 3
    assert dec.feed(GET_WORK_MODE[3:6]) == []
    assert dec.buffered == 6
    assert dec.feed(GET_WORK_MODE[6:]) == [Frame(0xA1, b"\x06")]


def test_decoder_buffer_never_exceeds_max_frame() -> None:
    dec = FrameDecoder()
    # A header promising a maximum-size frame, then lots of noise without A5.
    dec.feed(b"\xa5\x5a" + MAX_FRAME_LEN.to_bytes(2, "big"))
    for _ in range(100):
        dec.feed(b"\x00" * 1000)
        assert dec.buffered < MAX_FRAME_LEN
    assert dec.bad_frames >= 1


def test_decoder_reset() -> None:
    dec = FrameDecoder()
    dec.feed(GET_WORK_MODE[:4])
    dec.reset()
    assert dec.buffered == 0
    assert dec.discarded_bytes == 4
    assert dec.feed(GET_WORK_MODE[4:]) == []


@pytest.mark.parametrize(
    ("max_frame", "max_buffer"), [(7, 100), (2049, 4096), (100, 50)]
)
def test_decoder_bad_limits(max_frame: int, max_buffer: int) -> None:
    with pytest.raises(ValidationError):
        FrameDecoder(max_frame_len=max_frame, max_buffer=max_buffer)
