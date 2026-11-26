"""Boundary and regression tests that pin exact protocol behaviour.

Most were added after mutation testing showed a behaviour was not asserted.
"""

from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from aiochainway.models import (
    AntennaPower,
    FrequencyRegion,
    Gen2Settings,
    GpoState,
    InventoryMode,
    InventoryModeConfig,
    ReaderNetworkConfig,
    RfLink,
    TriggerConfig,
    TriggerReport,
    UdpDestination,
    WorkMode,
)
from aiochainway.protocol import commands as c
from aiochainway.protocol.errors import (
    ChecksumError,
    FrameError,
    ReaderError,
    ReaderTimeout,
    UnexpectedReplyError,
    ValidationError,
)
from aiochainway.protocol.frame import (
    MAX_DATA_LEN,
    MIN_FRAME_LEN,
    Frame,
    FrameDecoder,
    decode,
    encode,
)
from aiochainway.protocol.tags import parse_tag_record

from .fake_reader import tag_record

H = bytes.fromhex
STOP = H("A55A00088C840D0A")

GETS = [
    c.build_get_network_config,
    c.build_get_udp_destination,
    c.build_get_work_mode,
    c.build_get_beep,
    c.build_get_gpi,
    c.build_get_trigger_config,
]


@pytest.mark.parametrize("build", GETS)
def test_config_gets_match_on_sub_command(build: object) -> None:
    req = build()  # type: ignore[operator]  # parametrized callables
    assert req.reply_cmd == 0xA2
    assert req.reply_sub == req.data[0]
    assert req.matches(Frame(0xA2, bytes((req.data[0], 0))))
    assert not req.matches(Frame(0xA2, b"\x01"))


def test_config_sets_match_on_status() -> None:
    sets = [
        c.build_set_network_config(
            ReaderNetworkConfig("10.0.0.5", "255.255.255.0", "10.0.0.1")
        ),
        c.build_set_udp_destination(UdpDestination("10.0.0.9", 1)),
        c.build_set_work_mode(WorkMode.AUTO),
        c.build_set_beep(True),
        c.build_set_gpo(GpoState()),
        c.build_set_trigger_config(TriggerConfig()),
    ]
    for req in sets:
        assert req.reply_cmd == 0xA2 and req.reply_sub is None
        assert req.matches(Frame(0xA2, b"\x01"))
    assert c.build_start_inventory().reply_cmd is None


def test_set_power_all_and_gen2_requests() -> None:
    req = c.build_set_power_all([(4, 5)])
    assert (req.name, req.cmd, req.reply_cmd) == ("set_power_all", 0x10, 0x11)
    req = c.build_set_gen2(Gen2Settings())
    assert (req.name, req.cmd, req.reply_cmd) == ("set_gen2", 0x20, 0x21)
    with pytest.raises(ValidationError, match="no antenna powers given"):
        c.build_set_power_all([])
    with pytest.raises(ValidationError, match="more than once"):
        c.build_set_power_all([(2, 5), (2, 6)])


gen2_settings = st.builds(
    Gen2Settings,
    **{
        name: st.integers(0, (1 << width) - 1)
        for name, width in Gen2Settings.WIDTHS.items()
    },
)


@given(gen2_settings)
def test_gen2_pack_parse_round_trip(settings: Gen2Settings) -> None:
    req = c.build_set_gen2(settings)
    assert c.parse_get_gen2(Frame(0x23, req.data)) == settings


@pytest.mark.parametrize("name", list(Gen2Settings.WIDTHS))
def test_gen2_each_field_isolated(name: str) -> None:
    width = Gen2Settings.WIDTHS[name]
    zero = Gen2Settings(**dict.fromkeys(Gen2Settings.WIDTHS, 0))
    one = Gen2Settings(
        **{**dict.fromkeys(Gen2Settings.WIDTHS, 0), name: (1 << width) - 1}
    )
    packed = c.build_set_gen2(one).data
    assert c.build_set_gen2(zero).data == b"\x00\x00\x00\x00"
    assert c.parse_get_gen2(Frame(0x23, packed)) == one
    assert bin(int.from_bytes(packed, "big")).count("1") == width


def test_temperature_negative_boundary() -> None:
    assert c.parse_get_temperature(Frame(0x35, H("01F000"))) == -(
        (0xFFFF - 0xF000) / 100
    )
    assert c.parse_get_temperature(Frame(0x35, H("01EF00"))) == 0xEF00 / 100


def test_network_config_fields_and_long_reply() -> None:
    data = H("02C0A801321F90FFFFFF00C0A801FEAABBCC")
    cfg = c.parse_get_network_config(Frame(0xA2, data))
    assert cfg == ReaderNetworkConfig(
        "192.168.1.50", "255.255.255.0", "192.168.1.254", 8080
    )


def test_trigger_fields() -> None:
    cfg = c.parse_get_trigger_config(Frame(0xA2, H("0C011234005600")))
    assert cfg == TriggerConfig(
        gpi=2, work_time=0x1234, interval=0x56, report=TriggerReport.SERIAL
    )


def test_get_power_edge_cases() -> None:
    four = c.parse_get_power(Frame(0x13, H("00040BB807D0")))
    assert four == [AntennaPower(4, 30, 20)]
    ordered = c.parse_get_power(Frame(0x13, H("00050BB80BB80207D007D0")))
    assert ordered == [AntennaPower(2, 20, 20)]
    partial = c.parse_get_power(Frame(0x13, H("00010BB80BB802AABBCC")))
    assert partial == [AntennaPower(1, 30, 30)]
    with pytest.raises(UnexpectedReplyError, match="no antenna records"):
        c.parse_get_power(Frame(0x13, H("00060BB80BB8")))
    with pytest.raises(UnexpectedReplyError, match="get_power"):
        c.parse_get_power(Frame(0x13, H("0001FFFF0BB8")))


def test_antenna_work_time_bounds() -> None:
    assert c.build_set_antenna_work_time(1, 10).data == H("01000A")
    assert c.build_set_antenna_work_time(4, 65535).data == H("04FFFF")
    with pytest.raises(ValidationError, match="10-65535 ms"):
        c.build_set_antenna_work_time(1, 100.0)  # type: ignore[arg-type]  # wrong type on purpose


def test_inventory_mode_messages() -> None:
    with pytest.raises(UnexpectedReplyError, match="unknown mode 5"):
        c.parse_get_inventory_mode(Frame(0x73, b"\x01\x05\x07\x00"))
    with pytest.raises(ValidationError, match="EPC_TID_USER needs"):
        c.build_set_inventory_mode(InventoryModeConfig(InventoryMode.EPC_TID_USER))


def test_power_message() -> None:
    with pytest.raises(
        ValidationError, match="power must be an integer 1-30 dBm, got 31"
    ):
        c.build_set_power(1, 31)


def test_discovery_exactly_twelve_bytes() -> None:
    reader = c.parse_discovery(H("0011223344550A0000051F90"))
    assert (reader.mac, reader.ip, reader.port) == (
        "00:11:22:33:44:55",
        "10.0.0.5",
        8080,
    )
    with pytest.raises(UnexpectedReplyError, match="11 bytes, need 12"):
        c.parse_discovery(bytes(11))


def test_expect_messages() -> None:
    with pytest.raises(
        UnexpectedReplyError, match="expected sub-command 0x06, got 0x07"
    ):
        c.parse_get_work_mode(Frame(0xA2, b"\x07\x00"))
    with pytest.raises(UnexpectedReplyError, match="got nothing"):
        c.parse_get_work_mode(Frame(0xA2, b""))
    with pytest.raises(
        UnexpectedReplyError,
        match=r"get_work_mode: reply payload is 1 bytes, need at least 2",
    ):
        c.parse_get_work_mode(Frame(0xA2, b"\x06"))
    with pytest.raises(
        UnexpectedReplyError,
        match=r"get_region: reply payload is 1 bytes, need at least 2",
    ):
        c.parse_get_region(Frame(0x2F, b"\x01"))
    with pytest.raises(UnexpectedReplyError, match="expected reply CMD 0x03, got 0x05"):
        c.parse_get_version(Frame(0x05, b"\x01\x02\x03"))
    for parse, name in (
        (c.parse_get_udp_destination, "get_udp_destination"),
        (c.parse_get_trigger_config, "get_trigger_config"),
        (c.parse_get_network_config, "get_network_config"),
        (c.parse_get_gen2, "get_gen2"),
    ):
        with pytest.raises(UnexpectedReplyError, match=f"^{name}:"):
            parse(Frame(0x00, b""))


def test_ack_parsers_carry_command_name() -> None:
    with pytest.raises(
        ReaderError, match=r"^set_beep: reader reported failure"
    ) as info:
        c.parse_set_beep(Frame(0xA2, b"\x00"))
    assert info.value.command == "set_beep"
    with pytest.raises(
        UnexpectedReplyError, match=r"^set_antennas: expected reply CMD 0x29"
    ):
        c.parse_set_antennas(Frame(0xA2, b"\x01"))


def test_error_messages() -> None:
    err = ReaderError("cmd", None)
    assert str(err) == "cmd: reader reported failure" and err.status is None
    err = ReaderError("cmd", 0x0A, "detail")
    assert str(err) == "cmd: reader reported failure (status 0x0A): detail"
    assert err.command == "cmd"
    t = ReaderTimeout("get_x", 2.5)
    assert (
        str(t) == "get_x: no reply within 2.5 s"
        and t.timeout == 2.5
        and t.command == "get_x"
    )
    ck = ChecksumError(0x12, 0x34)
    assert str(ck) == "XOR checksum mismatch: frame has 0x34, computed 0x12"


# Frames --------------------------------------------------------------------------


def test_decode_max_frame_and_messages() -> None:
    raw = encode(0x83, bytes(MAX_DATA_LEN))
    assert decode(raw).data == bytes(MAX_DATA_LEN)
    with pytest.raises(FrameError, match=r"bad header A55B$"):
        decode(b"\xa5\x5b" + STOP[2:])
    with pytest.raises(FrameError, match="bad trailer 0D0B"):
        decode(STOP[:-1] + b"\x0b")
    with pytest.raises(
        ValidationError, match=r"data is 2041 bytes; the maximum is 2040"
    ):
        encode(1, bytes(MAX_DATA_LEN + 1))


def test_decoder_limits_inclusive() -> None:
    FrameDecoder(max_frame_len=MIN_FRAME_LEN, max_buffer=MIN_FRAME_LEN)
    dec = FrameDecoder()
    raw = encode(0x83, bytes(MAX_DATA_LEN))
    assert dec.feed(raw) == [Frame(0x83, bytes(MAX_DATA_LEN))]
    small = FrameDecoder(max_frame_len=9)
    assert small.feed(encode(0xA1, b"\x06")) == [Frame(0xA1, b"\x06")]


def test_decoder_long_frame_length_high_byte() -> None:
    dec = FrameDecoder()
    data = bytes(range(256)) + bytes(44)
    assert dec.feed(encode(0x83, data)) == [Frame(0x83, data)]


def test_decoder_bad_len_detected_with_just_the_header() -> None:
    dec = FrameDecoder()
    assert dec.feed(b"\xa5\x5a\x00\x00") == []
    assert dec.bad_frames == 1
    dec2 = FrameDecoder()
    dec2.feed(b"\xa5\x5a\x00")
    assert dec2.bad_frames == 0 and dec2.buffered == 3


def test_decoder_counts_every_bad_frame_and_keeps_position() -> None:
    bad_xor = STOP[:5] + b"\x00" + STOP[6:]
    dec = FrameDecoder()
    get_mode = encode(0xA1, b"\x06")
    out = dec.feed(STOP + get_mode + bad_xor + bad_xor + b"\xa5\x5a\x00\x01" + STOP)
    assert out == [Frame(0x8C), Frame(0xA1, b"\x06"), Frame(0x8C)]
    assert dec.bad_frames == 3


def test_decoder_reset_accumulates() -> None:
    dec = FrameDecoder()
    dec.feed(b"\xa5\x5a\x00")
    dec.reset()
    dec.feed(b"\xa5\x5a")
    dec.reset()
    assert dec.discarded_bytes == 5


# Tags ----------------------------------------------------------------------------


def test_user_bank_is_upper_hex() -> None:
    tag = parse_tag_record(tag_record("AABB", tid="ab" * 12, user="aabbccdd"))
    assert tag.user == "AABBCCDD" and tag.tid == "AB" * 12


@pytest.mark.parametrize("extra", range(1, 12))
def test_strict_rejects_every_in_between_length(extra: int) -> None:
    record = tag_record("AABB") + bytes(extra)
    with pytest.raises(
        FrameError, match="fits neither EPC \\(7\\) nor EPC\\+TID \\(>= 19\\)"
    ):
        parse_tag_record(record)


def test_truncated_message() -> None:
    with pytest.raises(FrameError, match="PC 0x3000 needs 17 bytes, got 16"):
        parse_tag_record(b"\x30\x00" + bytes(14))


def test_enum_parsers_return_enum_members() -> None:
    region = c.parse_get_region(Frame(0x2F, b"\x01\x08"))
    assert type(region) is FrequencyRegion and region is FrequencyRegion.USA
    link = c.parse_get_rf_link(Frame(0x55, b"\x01\x01\x02"))
    assert type(link) is RfLink and link is RfLink.PR_ASK_MILLER4_300K
    assert type(c.parse_get_region(Frame(0x2F, b"\x01\x99"))) is int


@pytest.mark.parametrize("cut", range(4))
def test_gen2_short_reply(cut: int) -> None:
    with pytest.raises(UnexpectedReplyError, match="get_gen2"):
        c.parse_get_gen2(Frame(0x23, bytes(cut)))
