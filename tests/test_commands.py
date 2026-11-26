"""Builders (golden bytes) and reply parsers for every command."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aiochainway.models import (
    AntennaPower,
    AntennaState,
    FrequencyRegion,
    Gen2Settings,
    GpiState,
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
    ReaderError,
    UnexpectedReplyError,
    ValidationError,
)
from aiochainway.protocol.frame import Frame, decode, encode

H = bytes.fromhex

# (request, expected full frame hex) — every frame computed by hand from
# docs/protocol.md: A5 5A LEN CMD DATA XOR 0D 0A.
GOLDEN: list[tuple[Callable[[], c.Request], str]] = [
    (lambda: c.build_get_version(), "A55A0008020A0D0A"),
    (lambda: c.build_get_temperature(), "A55A0008343C0D0A"),
    (lambda: c.build_start_inventory(), "A55A000A820000880D0A"),
    (lambda: c.build_stop_inventory(), "A55A00088C840D0A"),
    (lambda: c.build_read_tag_buffer(), "A55A0008E0E80D0A"),
    (lambda: c.build_get_network_config(), "A55A0009A102AA0D0A"),
    (lambda: c.build_get_udp_destination(), "A55A0009A104AC0D0A"),
    (lambda: c.build_get_work_mode(), "A55A0009A106AE0D0A"),
    (lambda: c.build_set_work_mode(WorkMode.AUTO), "A55A000AA10501AF0D0A"),
    (lambda: c.build_set_work_mode(WorkMode.COMMAND), "A55A000AA10500AE0D0A"),
    (lambda: c.build_get_beep(), "A55A0009A108A00D0A"),
    (lambda: c.build_set_beep(True), "A55A000AA10701AD0D0A"),
    (lambda: c.build_set_beep(False), "A55A000AA10700AC0D0A"),
    (lambda: c.build_get_gpi(), "A55A0009A10AA20D0A"),
    (lambda: c.build_set_gpo(GpoState(relay=True)), "A55A000CA109000001A50D0A"),
    (lambda: c.build_get_trigger_config(), "A55A0009A10CA40D0A"),
    (
        lambda: c.build_set_trigger_config(TriggerConfig(gpi=2, work_time=0x0102, interval=0x0304, report=TriggerReport.UDP)),
        "A55A0010A10B01010203040100BE0D0A",
    ),
    (lambda: c.build_get_antennas(), "A55A00082A220D0A"),
    (lambda: c.build_set_antennas(AntennaState.of(1, 2, 3, 4)), "A55A000B2801000F2D0D0A"),
    (lambda: c.build_set_antennas(AntennaState.of(2)), "A55A000B28010002200D0A"),
    (lambda: c.build_get_power(), "A55A0008121A0D0A"),
    (lambda: c.build_set_power(1, 30), "A55A000E1002010BB80BB81D0D0A"),
    (lambda: c.build_set_power(4, 1), "A55A000E10020400640064180D0A"),
    (lambda: c.build_get_region(), "A55A00082E260D0A"),
    (lambda: c.build_set_region(FrequencyRegion.USA), "A55A000A2C01082F0D0A"),
    (lambda: c.build_get_rf_link(), "A55A000A5400005E0D0A"),
    (lambda: c.build_set_rf_link(RfLink.PR_ASK_MILLER4_250K), "A55A000B52000101590D0A"),
    (lambda: c.build_get_gen2(), "A55A0008222A0D0A"),
    (lambda: c.build_get_inventory_mode(), "A55A000A720000780D0A"),
    (lambda: c.build_set_inventory_mode(InventoryModeConfig(InventoryMode.EPC)), "A55A000C70010000007D0D0A"),
    (lambda: c.build_set_inventory_mode(InventoryModeConfig(InventoryMode.EPC_TID)), "A55A000C70010100007C0D0A"),
    (lambda: c.build_get_antenna_work_time(3), "A55A000A4C0300450D0A"),
    (lambda: c.build_set_antenna_work_time(1, 200), "A55A000B4A0100C8880D0A"),
    (lambda: c.build_factory_reset(), "A55A0008747C0D0A"),
    (
        lambda: c.build_set_network_config(ReaderNetworkConfig("192.168.1.50", "255.255.255.0", "192.168.1.1", 8888)),
        "A55A0017A101C0A8013222B8FFFFFF00C0A80101E10D0A",
    ),
    (
        lambda: c.build_set_udp_destination(UdpDestination("192.168.1.10", 9000)),
        "A55A000FA103C0A8010A2328C50D0A",
    ),
    (lambda: c.build_set_fastid(True), "A55A000A5C0100570D0A"),
    (lambda: c.build_set_tagfocus(True), "A55A000A6001006B0D0A"),
]  # fmt: skip


def _hex(text: str) -> bytes:
    return H(text.replace(" ", ""))


# Built inside each test (not at import): mutmut forks after collection.
@pytest.mark.parametrize(("build", "expected"), GOLDEN)
def test_golden_frames(build: Callable[[], c.Request], expected: str) -> None:
    assert build().encode().hex().upper() == _hex(expected).hex().upper()


@pytest.mark.parametrize(("build", "expected"), GOLDEN)
def test_round_trip(build: Callable[[], c.Request], expected: str) -> None:
    request_ = build()
    frame = decode(request_.encode())
    assert (frame.cmd, frame.data) == (request_.cmd, request_.data)


def test_every_registry_command_has_a_golden_vector() -> None:
    covered = {build().name for build, _ in GOLDEN}
    assert set(c.COMMANDS) <= covered | {"set_power_all", "set_gen2"}


def test_registry_matches_builders() -> None:
    for build, _ in GOLDEN:
        request_ = build()
        info = c.COMMANDS[request_.name]
        assert info.cmd == request_.cmd
        assert info.reply_cmd == request_.reply_cmd
        if info.sub is not None:
            assert request_.data[0] == info.sub
            assert request_.reply_cmd == 0xA2


def test_set_power_all_and_gen2() -> None:
    req = c.build_set_power_all([(1, 30), (2, 20)])
    assert req.data == H("02 01 0BB8 0BB8 02 07D0 07D0".replace(" ", ""))
    assert req.cmd == 0x10
    gen2 = Gen2Settings(select_target=5, select_action=3, select_truncate=1, q=1, start_q=4, min_q=2, max_q=15, query_dr=1, query_m=3, query_trext=1, query_sel=2, query_session=1, query_target=1, link_frequency=6)  # fmt: skip
    req = c.build_set_gen2(gen2)
    assert req.data == bytes((0b10101111, 0x42, 0b11111111, 0b10011110))
    assert c.parse_get_gen2(Frame(0x23, req.data)) == gen2


# Request.matches ---------------------------------------------------------------


def test_matches_requires_cmd_and_sub() -> None:
    get = c.build_get_work_mode()
    assert get.matches(Frame(0xA2, b"\x06\x00"))
    assert not get.matches(Frame(0xA2, b"\x04\x00"))
    assert not get.matches(Frame(0xA2, b""))
    assert not get.matches(Frame(0x03, b"\x06"))
    sett = c.build_set_work_mode(WorkMode.AUTO)
    assert sett.matches(Frame(0xA2, b"\x01"))
    start = c.build_start_inventory()
    assert not start.expects_reply
    assert not start.matches(Frame(0x83, b""))


# Parsers: valid ----------------------------------------------------------------


VALID: list[tuple[Callable[[Frame], Any], Frame, Any]] = [
    (c.parse_get_version, Frame(0x03, b"\x01\x02\x03"), "V1.2.3"),
    (c.parse_get_version, Frame(0x03, b"\x0a\x00\x63\xff"), "V10.0.99"),
    (c.parse_get_temperature, Frame(0x35, H("010A5A")), 26.5),
    (c.parse_get_temperature, Frame(0x35, H("01FF9B")), -1.0),
    (c.parse_get_temperature, Frame(0x35, H("01EFFF")), 614.39),
    (c.parse_get_work_mode, Frame(0xA2, b"\x06\x02"), WorkMode.TRIGGER),
    (c.parse_get_beep, Frame(0xA2, b"\x08\x01"), True),
    (c.parse_get_beep, Frame(0xA2, b"\x08\x00"), False),
    (c.parse_get_gpi, Frame(0xA2, b"\x0a\x00\x01"), GpiState(False, True)),
    (c.parse_get_trigger_config, Frame(0xA2, H("0C01006400020" "1")), TriggerConfig(2, 100, 2, TriggerReport.UDP)),
    (c.parse_get_antennas, Frame(0x2B, b"\x00\x05"), AntennaState.of(1, 3)),
    (c.parse_get_antennas, Frame(0x2B, b"\xff\xf0"), AntennaState(frozenset())),
    (c.parse_get_region, Frame(0x2F, b"\x01\x08"), FrequencyRegion.USA),
    (c.parse_get_region, Frame(0x2F, b"\x01\x99"), 0x99),
    (c.parse_get_rf_link, Frame(0x55, b"\x01\x01\x03"), RfLink.DSB_ASK_FM0_400K),
    (c.parse_get_rf_link, Frame(0x55, b"\x01\x01\x09"), 9),
    (c.parse_get_inventory_mode, Frame(0x73, b"\x01\x02\x04\x08"), InventoryModeConfig(InventoryMode.EPC_TID_USER, 4, 8)),
    (c.parse_get_inventory_mode, Frame(0x73, b"\x01\x01\x04\x08"), InventoryModeConfig(InventoryMode.EPC_TID)),
    (c.parse_get_antenna_work_time, Frame(0x4D, H("010101F4")), 500),
    (c.parse_get_udp_destination, Frame(0xA2, H("04C0A8010A2328")), UdpDestination("192.168.1.10", 9000)),
    (c.parse_get_udp_destination, Frame(0xA2, H("04000000002328")), None),
    (c.parse_get_udp_destination, Frame(0xA2, H("04C0A8010A0000")), None),
    (c.parse_get_network_config, Frame(0xA2, H("02C0A801322 2B8FFFF0000C0A80101".replace(" ", ""))), ReaderNetworkConfig("192.168.1.50", "255.255.0.0", "192.168.1.1", 8888)),
    (c.parse_get_network_config, Frame(0xA2, H("02C0A801322 2B8".replace(" ", ""))), ReaderNetworkConfig("192.168.1.50", "255.255.255.0", "192.168.1.1", 8888)),
    (c.parse_get_power, Frame(0x13, H("00010BB80BB8020 7D007D0".replace(" ", ""))), [AntennaPower(1, 30, 30), AntennaPower(2, 20, 20)]),
    (c.parse_get_power, Frame(0x13, H("00010BB80BB80300000000".replace(" ", ""))), [AntennaPower(1, 30, 30), AntennaPower(3, 30, 30)]),
    (c.parse_get_power, Frame(0x13, H("00000BB80BB80907D007D0".replace(" ", ""))), [AntennaPower(1, 30, 30)]),
    (c.parse_get_power, Frame(0x13, H("00010BB80BB8010 7D007D0".replace(" ", ""))), [AntennaPower(1, 30, 30)]),
    (c.parse_get_power, Frame(0x13, H("00010A280000")), [AntennaPower(1, 26, 26)]),
]  # fmt: skip


@pytest.mark.parametrize(("parser", "frame", "expected"), VALID)
def test_parse_valid(
    parser: Callable[[Frame], Any], frame: Frame, expected: Any
) -> None:
    assert parser(frame) == expected


@pytest.mark.parametrize(("parser", "frame", "expected"), VALID)
def test_parse_tolerates_extra_bytes(
    parser: Callable[[Frame], Any], frame: Frame, expected: Any
) -> None:
    if parser is c.parse_get_power or parser is c.parse_get_network_config:
        pytest.skip("payload length is meaningful for this reply")
    assert parser(Frame(frame.cmd, frame.data + b"\xee\xee")) == expected


@pytest.mark.parametrize(("parser", "frame", "expected"), VALID)
def test_parse_wrong_cmd(
    parser: Callable[[Frame], Any], frame: Frame, expected: Any
) -> None:
    with pytest.raises(UnexpectedReplyError, match="expected reply CMD"):
        parser(Frame((frame.cmd + 2) & 0xFF, frame.data))


MIN_LEN: dict[Callable[[Frame], Any], int] = {
    c.parse_get_version: 3,
    c.parse_get_temperature: 3,
    c.parse_get_work_mode: 2,
    c.parse_get_beep: 2,
    c.parse_get_gpi: 3,
    c.parse_get_trigger_config: 7,
    c.parse_get_antennas: 2,
    c.parse_get_region: 2,
    c.parse_get_rf_link: 3,
    c.parse_get_inventory_mode: 4,
    c.parse_get_antenna_work_time: 4,
    c.parse_get_udp_destination: 7,
    c.parse_get_network_config: 7,
    c.parse_get_power: 4,
}


@pytest.mark.parametrize(("parser", "frame", "expected"), VALID)
def test_parse_short(
    parser: Callable[[Frame], Any], frame: Frame, expected: Any
) -> None:
    for cut in range(MIN_LEN[parser]):
        with pytest.raises(UnexpectedReplyError):
            parser(Frame(frame.cmd, frame.data[:cut]))


SUB_PARSERS = [
    (c.parse_get_work_mode, b"\x06\x00"),
    (c.parse_get_beep, b"\x08\x00"),
    (c.parse_get_gpi, b"\x0a\x00\x00"),
    (c.parse_get_trigger_config, H("0C00006400000 1".replace(" ", ""))),
    (c.parse_get_udp_destination, H("04C0A8010A2328")),
    (c.parse_get_network_config, H("02C0A801322 2B8".replace(" ", ""))),
]


@pytest.mark.parametrize(("parser", "data"), SUB_PARSERS)
def test_parse_wrong_sub(parser: Callable[[Frame], Any], data: bytes) -> None:
    with pytest.raises(UnexpectedReplyError, match="sub-command"):
        parser(Frame(0xA2, bytes(((data[0] + 1) & 0xFF,)) + data[1:]))
    with pytest.raises(UnexpectedReplyError, match="sub-command"):
        parser(Frame(0xA2, b""))


STATUS_PARSERS: list[tuple[Callable[[Frame], Any], int, bytes]] = [
    (c.parse_get_temperature, 0x35, H("000A5A")),
    (c.parse_get_region, 0x2F, b"\x00\x08"),
    (c.parse_get_rf_link, 0x55, b"\x00\x00\x01"),
    (c.parse_get_inventory_mode, 0x73, b"\x00\x00\x00\x00"),
    (c.parse_get_antenna_work_time, 0x4D, H("000100C8")),
    (c.parse_get_power, 0x13, H("01010BB80BB8")),
    (c.parse_stop_inventory, 0x8D, b"\x00"),
    (c.parse_set_work_mode, 0xA2, b"\x00"),
    (c.parse_set_network_config, 0xA2, b"\x00"),
    (c.parse_set_udp_destination, 0xA2, b"\x00"),
    (c.parse_set_beep, 0xA2, b"\x02"),
    (c.parse_set_gpo, 0xA2, b"\x00"),
    (c.parse_set_trigger_config, 0xA2, b"\x00"),
    (c.parse_set_antennas, 0x29, b"\x00"),
    (c.parse_set_power, 0x11, b"\x00"),
    (c.parse_set_power_all, 0x11, b"\x00"),
    (c.parse_set_region, 0x2D, b"\x00"),
    (c.parse_set_rf_link, 0x53, b"\x00"),
    (c.parse_set_gen2, 0x21, b"\x00"),
    (c.parse_set_inventory_mode, 0x71, b"\x00"),
    (c.parse_set_antenna_work_time, 0x4B, b"\x00"),
    (c.parse_factory_reset, 0x75, b"\x00"),
]


@pytest.mark.parametrize(("parser", "cmd", "data"), STATUS_PARSERS)
def test_parse_failure_status(
    parser: Callable[[Frame], Any], cmd: int, data: bytes
) -> None:
    with pytest.raises(ReaderError) as info:
        parser(Frame(cmd, data))
    assert info.value.status == data[0]


@pytest.mark.parametrize(("parser", "cmd", "data"), STATUS_PARSERS)
def test_parse_status_empty_payload(
    parser: Callable[[Frame], Any], cmd: int, data: bytes
) -> None:
    with pytest.raises(UnexpectedReplyError):
        parser(Frame(cmd, b""))


def test_set_parsers_accept_ok() -> None:
    for parser, cmd, _ in STATUS_PARSERS:
        if parser.__name__.startswith("parse_set") or parser in (
            c.parse_stop_inventory,
            c.parse_factory_reset,
        ):
            assert parser(Frame(cmd, b"\x01")) is None
            assert parser(Frame(cmd, b"\x01\x99")) is None


@pytest.mark.parametrize(
    ("parser", "frame"),
    [
        (c.parse_get_work_mode, Frame(0xA2, b"\x06\x07")),
        (c.parse_get_inventory_mode, Frame(0x73, b"\x01\x05\x00\x00")),
        (
            c.parse_get_trigger_config,
            Frame(0xA2, H("0C0500640000 01".replace(" ", ""))),
        ),
        (
            c.parse_get_trigger_config,
            Frame(0xA2, H("0C0000640000 07".replace(" ", ""))),
        ),
        (
            c.parse_get_network_config,
            Frame(0xA2, H("02C0A801322 2B8FFFFFF00 0A000001".replace(" ", ""))),
        ),
        (
            c.parse_get_network_config,
            Frame(0xA2, H("0200000000 22B8".replace(" ", ""))),
        ),
        (c.parse_get_udp_destination, Frame(0xA2, H("04E00000012328"))),
        (c.parse_get_power, Frame(0x13, H("00050BB80BB8"))),
        (c.parse_get_power, Frame(0x13, H("00010FA00FA0"))),
        (c.parse_get_power, Frame(0x13, H("000100000000"))),
    ],
)
def test_parse_rejects_impossible_values(
    parser: Callable[[Frame], Any], frame: Frame
) -> None:
    with pytest.raises(UnexpectedReplyError):
        parser(frame)


# Builder validation ------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: c.build_set_power(0, 20),
        lambda: c.build_set_power(5, 20),
        lambda: c.build_set_power(1, 0),
        lambda: c.build_set_power(1, 31),
        lambda: c.build_set_power(1, True),
        lambda: c.build_set_power(1, 20.5),  # type: ignore[arg-type]  # wrong type on purpose
        lambda: c.build_set_power_all([]),
        lambda: c.build_set_power_all([(1, 20), (1, 25)]),
        lambda: c.build_set_antennas(AntennaState(frozenset())),
        lambda: c.build_set_region(0x55),
        lambda: c.build_set_work_mode(9),  # type: ignore[arg-type]  # wrong type on purpose
        lambda: c.build_set_rf_link(7),  # type: ignore[arg-type]  # wrong type on purpose
        lambda: c.build_set_antenna_work_time(1, 9),
        lambda: c.build_set_antenna_work_time(1, 65536),
        lambda: c.build_set_antenna_work_time(1, True),
        lambda: c.build_set_antenna_work_time(0, 100),
        lambda: c.build_get_antenna_work_time(5),
        lambda: c.build_set_inventory_mode(
            InventoryModeConfig(InventoryMode.EPC_TID_USER, 0, 0)
        ),
    ],
)
def test_builders_validate(call: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        call()


def test_set_inventory_mode_user() -> None:
    req = c.build_set_inventory_mode(
        InventoryModeConfig(InventoryMode.EPC_TID_USER, 2, 6)
    )
    assert req.data == b"\x01\x02\x02\x06"


# Discovery ---------------------------------------------------------------------


def test_parse_discovery() -> None:
    reader = c.parse_discovery(H("A1B2C3D4E5F6C0A863CA22B8") + b"extra")
    assert (reader.mac, reader.ip, reader.port) == (
        "A1:B2:C3:D4:E5:F6",
        "192.168.99.202",
        8888,
    )
    with pytest.raises(UnexpectedReplyError):
        c.parse_discovery(H("A1B2C3D4E5F6C0A863CA22"))


def test_encode_helper_consistency() -> None:
    assert c.build_get_version().encode() == encode(0x02)
