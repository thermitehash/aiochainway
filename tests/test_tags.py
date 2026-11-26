"""Tag record parsing."""

from __future__ import annotations

import pytest

from aiochainway.protocol.errors import FrameError
from aiochainway.protocol.tags import epc_length, parse_tag_record, rssi_to_dbm

from .fake_reader import tag_record

EPC96 = "E28011700000020A1B2C3D4E"
EPC128 = "300833B2DDD9014000000000ABCD1234"
EPC496 = "AB" * 62
TID = "E2801170200012345678ABCD"
USER = "0011223344556677"


@pytest.mark.parametrize("epc", [EPC96, EPC128, EPC496, ""])
def test_epc_lengths(epc: str) -> None:
    record = tag_record(epc, rssi_dbm=-55.5, antenna=2)
    tag = parse_tag_record(record, received_at=1.0, source="x")
    assert tag.epc == epc
    assert tag.rssi_dbm == -55.5
    assert tag.antenna == 2
    assert tag.tid is None and tag.user is None
    assert tag.pc == (record[0] << 8) | record[1]
    assert tag.received_at == 1.0
    assert tag.source == "x"


def test_epc_length_from_pc() -> None:
    assert epc_length(b"\x30\x00") == 12
    assert epc_length(b"\x40\x00") == 16
    assert epc_length(b"\xf8\x00") == 62
    assert epc_length(b"\x07\xff") == 0


def test_epc_tid() -> None:
    tag = parse_tag_record(tag_record(EPC96, tid=TID, antenna=4))
    assert (tag.epc, tag.tid, tag.user, tag.antenna) == (EPC96, TID, None, 4)


def test_epc_tid_user() -> None:
    tag = parse_tag_record(tag_record(EPC128, tid=TID, user=USER, antenna=3))
    assert (tag.epc, tag.tid, tag.user, tag.antenna) == (EPC128, TID, USER, 3)


def test_received_at_defaults_to_now() -> None:
    tag = parse_tag_record(tag_record(EPC96))
    assert tag.received_at > 1_600_000_000


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0x0000, None),
        (0x0001, None),
        (1999, None),
        (2000, None),
        (0xFFFF, None),  # magnitude 0
        (0xFFFE, -0.1),
        (0xFFFF - 1999, -199.9),  # magnitude 199.9: valid
        (0xFFFF - 2000, None),  # magnitude 200.0: invalid
        (0xFFFF - 600, -60.0),
    ],
)
def test_rssi_edges(raw: int, expected: float | None) -> None:
    assert rssi_to_dbm(raw) == expected
    record = tag_record(EPC96)[:-3] + raw.to_bytes(2, "big") + b"\x01"
    assert parse_tag_record(record).rssi_dbm == expected


@pytest.mark.parametrize("antenna", [0, 5, 0xFF])
def test_bad_antenna_rejected(antenna: int) -> None:
    with pytest.raises(FrameError, match="antenna"):
        parse_tag_record(tag_record(EPC96, antenna=antenna))


@pytest.mark.parametrize(
    "record",
    [
        tag_record(EPC96),
        tag_record(EPC496, tid=TID),
        tag_record(EPC128, tid=TID, user=USER),
    ],
)
def test_truncated_at_every_offset(record: bytes) -> None:
    full = parse_tag_record(record)
    for cut in range(len(record)):
        try:
            tag = parse_tag_record(record[:cut])
        except FrameError:
            continue
        # A shorter record may still be a valid *different* layout (a USER
        # record cut to EPC+TID+USER-1); it must never be the same read.
        assert tag != full
        assert tag.epc == full.epc


def test_strict_rejects_in_between_lengths() -> None:
    record = tag_record(EPC96) + b"\x00\x00"
    with pytest.raises(FrameError, match="fits neither"):
        parse_tag_record(record)
    tag = parse_tag_record(record, strict=False)
    assert tag.epc == EPC96
    assert tag.antenna == 1


def test_short_epc_rejected_not_padded() -> None:
    # PC says 12 EPC bytes but only 10 follow (SDK would pad with zeros).
    record = b"\x30\x00" + bytes(10) + b"\xfd\xa7\x01"
    with pytest.raises(FrameError, match="truncated"):
        parse_tag_record(record)


@pytest.mark.parametrize("record", [b"", b"\x30", b"\x30\x00", b"\x30\x00\xff\xff"])
def test_tiny_records(record: bytes) -> None:
    with pytest.raises(FrameError):
        parse_tag_record(record)
