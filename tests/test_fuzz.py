"""Property-based tests (Hypothesis)."""

from __future__ import annotations

from hypothesis import given, strategies as st

from aiochainway.protocol.errors import FrameError
from aiochainway.protocol.frame import (
    MAX_FRAME_LEN,
    Frame,
    FrameDecoder,
    decode,
    encode,
)
from aiochainway.protocol.tags import parse_tag_record

cmds = st.integers(min_value=0, max_value=255)
datas = st.binary(max_size=1000)
frames = st.tuples(cmds, datas).map(lambda t: Frame(*t))


@given(cmds, datas)
def test_encode_decode_round_trip(cmd: int, data: bytes) -> None:
    frame = decode(encode(cmd, data))
    assert (frame.cmd, frame.data) == (cmd, data)


@given(st.binary(max_size=4096), st.lists(st.integers(1, 64), max_size=64))
def test_decoder_never_raises_on_noise(noise: bytes, cuts: list[int]) -> None:
    dec = FrameDecoder()
    pos = 0
    for n in [*cuts, len(noise)]:
        dec.feed(noise[pos : pos + n])
        pos += n
        assert dec.buffered < MAX_FRAME_LEN


def _split(data: bytes, cuts: list[int]) -> list[bytes]:
    out, pos = [], 0
    for n in cuts:
        out.append(data[pos : pos + n])
        pos += n
    out.append(data[pos:])
    return out


# Garbage that cannot contain a header, so every valid frame must be recovered.
garbage = st.binary(max_size=64).map(lambda b: b.replace(b"\xa5", b"\x00"))


@given(
    st.lists(st.tuples(garbage, frames), max_size=12),
    garbage,
    st.lists(st.integers(1, 97), max_size=200),
)
def test_decoder_recovers_frames_between_garbage(
    items: list[tuple[bytes, Frame]], tail: bytes, cuts: list[int]
) -> None:
    stream = b"".join(g + f.encode() for g, f in items) + tail
    dec = FrameDecoder()
    got: list[Frame] = []
    for chunk in _split(stream, cuts):
        got += dec.feed(chunk)
        assert dec.buffered < MAX_FRAME_LEN
    assert got == [f for _, f in items]
    assert dec.bad_frames == 0


@given(
    st.lists(st.tuples(st.binary(max_size=40), frames), max_size=8),
    st.lists(st.integers(1, 50), max_size=100),
)
def test_decoder_with_arbitrary_garbage_finds_frames_in_order(
    items: list[tuple[bytes, Frame]], cuts: list[int]
) -> None:
    # Arbitrary garbage may contain A5 5A and swallow a frame that follows,
    # but the decoder must stay bounded and only return real, valid frames.
    stream = b"".join(g + f.encode() for g, f in items)
    dec = FrameDecoder()
    got: list[Frame] = []
    for chunk in _split(stream, cuts):
        got += dec.feed(chunk)
        assert dec.buffered < MAX_FRAME_LEN
    for frame in got:
        assert decode(frame.encode()) == frame
    # Frames after the last garbage that *could* hide a header are recovered.
    if items and b"\xa5" not in b"".join(g for g, _ in items):
        assert got == [f for _, f in items]


@given(st.binary(max_size=300))
def test_tag_parser_only_frame_error_or_tag(data: bytes) -> None:
    for strict in (True, False):
        try:
            tag = parse_tag_record(data, strict=strict, received_at=0.0)
        except FrameError:
            continue
        assert 1 <= tag.antenna <= 4
        assert len(tag.epc) == ((data[0] >> 3) * 2) * 2
