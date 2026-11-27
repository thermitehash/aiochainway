"""Command builders and reply parsers.

Each command has a ``build_*`` function returning a :class:`Request` and, when
the reader answers, a ``parse_*`` function taking the reply :class:`Frame`.

Conventions recovered from the SDK (see ``docs/protocol.md``):

* The reply CMD is the request CMD + 1 (``c.sendAndReceive`` waits for
  ``data[4] + 1``).
* ``0xA1`` "get" replies (``0xA2``) echo the sub-command in ``data[0]``.
  ``0xA1`` "set" replies carry a status byte in ``data[0]`` (``0x01`` = OK).
* Most other replies carry a status byte in ``data[0]``; ``0x01`` = OK, except
  get-power (``0x13``) where ``0x00`` = OK. Version (``0x03``) and Gen2
  (``0x23``) replies have no status byte.

Parsers check CMD, sub-command and length before slicing, and tolerate extra
trailing bytes. They raise :class:`UnexpectedReplyError` for a wrong CMD,
sub-command or short payload and :class:`ReaderError` for a failure status.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
import ipaddress

from ..models import (
    ANTENNA_MAX,
    ANTENNA_MIN,
    ANTENNA_WORK_TIME_MAX_MS,
    ANTENNA_WORK_TIME_MIN_MS,
    POWER_MAX_DBM,
    POWER_MIN_DBM,
    AntennaPower,
    AntennaState,
    DiscoveredReader,
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
    check_antenna,
)
from .errors import ReaderError, UnexpectedReplyError, ValidationError
from .frame import Frame, encode

# Command bytes ---------------------------------------------------------------
CMD_GET_VERSION = 0x02
CMD_SET_POWER = 0x10
CMD_GET_POWER = 0x12
CMD_SET_GEN2 = 0x20
CMD_GET_GEN2 = 0x22
CMD_SET_ANTENNAS = 0x28
CMD_GET_ANTENNAS = 0x2A
CMD_SET_REGION = 0x2C
CMD_GET_REGION = 0x2E
CMD_GET_TEMPERATURE = 0x34
CMD_SET_ANT_WORK_TIME = 0x4A
CMD_GET_ANT_WORK_TIME = 0x4C
CMD_SET_RF_LINK = 0x52
CMD_GET_RF_LINK = 0x54
CMD_SET_FASTID = 0x5C
CMD_SET_TAGFOCUS = 0x60
CMD_SET_INVENTORY_MODE = 0x70
CMD_GET_INVENTORY_MODE = 0x72
CMD_FACTORY_RESET = 0x74
CMD_START_INVENTORY = 0x82
CMD_TAG_REPORT = 0x83
CMD_STOP_INVENTORY = 0x8C
CMD_CONFIG = 0xA1
CMD_CONFIG_REPLY = 0xA2
CMD_READ_TAG_BUFFER = 0xE0

# 0xA1 sub-commands -----------------------------------------------------------
SUB_SET_NETWORK = 0x01
SUB_GET_NETWORK = 0x02
SUB_SET_DESTINATION = 0x03
SUB_GET_DESTINATION = 0x04
SUB_SET_WORK_MODE = 0x05
SUB_GET_WORK_MODE = 0x06
SUB_SET_BEEP = 0x07
SUB_GET_BEEP = 0x08
SUB_SET_GPO = 0x09
SUB_GET_GPI = 0x0A
SUB_SET_TRIGGER = 0x0B
SUB_GET_TRIGGER = 0x0C

#: Sub-commands whose ``0xA2`` reply echoes the sub-command in ``data[0]``.
CONFIG_GET_SUBS = frozenset(
    {
        SUB_GET_NETWORK,
        SUB_GET_DESTINATION,
        SUB_GET_WORK_MODE,
        SUB_GET_BEEP,
        SUB_GET_GPI,
        SUB_GET_TRIGGER,
    }
)

STATUS_OK = 0x01
#: Port the reader announces itself on (``SocketUdpUR4SearchDevice``).
DISCOVERY_PORT = 1111
DEFAULT_TCP_PORT = 8888


class SpecStatus(StrEnum):
    """How well a command's bytes are known."""

    SDK_DERIVED = "sdk-derived"
    INFERRED = "inferred"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class CommandInfo:
    """Registry entry: where a command's layout came from and how it is guarded."""

    name: str
    cmd: int
    sub: int | None
    reply_cmd: int | None
    status: SpecStatus
    sdk_method: str
    summary: str
    #: Network-changing or destructive: needs ``confirm=True``.
    guarded: bool = False
    #: ``False`` when the UR4 path of the SDK never sends it.
    supported: bool = True


@dataclass(frozen=True, slots=True)
class Request:
    """A request ready to send, plus how to recognise its reply."""

    name: str
    cmd: int
    data: bytes = b""
    reply_cmd: int | None = None
    #: When set, the reply's ``data[0]`` must equal this (0xA2 "get" replies).
    reply_sub: int | None = None

    def encode(self) -> bytes:
        """Return the request frame bytes."""
        return encode(self.cmd, self.data)

    @property
    def expects_reply(self) -> bool:
        """Whether the client should wait for a reply."""
        return self.reply_cmd is not None

    @property
    def reply_key(self) -> tuple[int, int | None] | None:
        """Key identifying this request's reply (see :func:`reply_key`)."""
        if self.reply_cmd is None:
            return None
        return (self.reply_cmd, self.reply_sub)

    def matches(self, frame: Frame) -> bool:
        """Return whether ``frame`` is the reply to this request."""
        return self.reply_key is not None and reply_key(frame) == self.reply_key


def reply_key(frame: Frame) -> tuple[int, int | None]:
    """Return the key a reply frame is matched on.

    ``(cmd, sub)`` for ``0xA2`` replies that echo a get sub-command, otherwise
    ``(cmd, None)``. So an ``0xA2`` "set" acknowledgement never matches a get
    reply, and the reverse.
    """
    if frame.cmd == CMD_CONFIG_REPLY and frame.sub in CONFIG_GET_SUBS:
        return (frame.cmd, frame.sub)
    return (frame.cmd, None)


_SD = SpecStatus.SDK_DERIVED
_INF = SpecStatus.INFERRED

COMMANDS: dict[str, CommandInfo] = {
    info.name: info
    for info in (
        CommandInfo("get_version", 0x02, None, 0x03, _SD, "i.getVersionSendData / i.parseVersionData", "Firmware version"),
        CommandInfo("get_temperature", 0x34, None, 0x35, _SD, "i.getTemperatureSendData / i.parseTemperatureData", "Module temperature"),
        CommandInfo("start_inventory", 0x82, None, None, _SD, "i.getStartInventoryTagSendData", "Start continuous inventory"),
        CommandInfo("stop_inventory", 0x8C, None, 0x8D, _SD, "i.getStopInventorySendData / i.parseStopInventoryData", "Stop inventory"),
        CommandInfo("read_tag_buffer", 0xE0, None, 0xE1, _SD, "i.getReadTagSendData", "Read tag buffer (not used by the UR4 SDK path)", supported=False),
        CommandInfo("get_network_config", 0xA1, 0x02, 0xA2, _SD, "j.getReaderIpSendData / j.parseGetReaderIpData", "Reader IP, mask, gateway, port"),
        CommandInfo("set_network_config", 0xA1, 0x01, 0xA2, _SD, "j.setReaderIpSendData / j.parseSetReaderIpData", "Set reader IP, mask, gateway, port", guarded=True),
        CommandInfo("get_udp_destination", 0xA1, 0x04, 0xA2, _SD, "j.getReaderDestIpSendData / j.parseGetReaderDestIpData", "UDP destination for auto/trigger mode"),
        CommandInfo("set_udp_destination", 0xA1, 0x03, 0xA2, _SD, "j.setReaderDestIpSendData / j.parseSetReaderDestIpData", "Set UDP destination", guarded=True),
        CommandInfo("get_work_mode", 0xA1, 0x06, 0xA2, _SD, "j.getWorkModeSendData / j.parseGetWorkModeReceiveData", "Work mode"),
        CommandInfo("set_work_mode", 0xA1, 0x05, 0xA2, _SD, "j.setWorkModeSendData / j.parseSetWorkModeReceiveData", "Set work mode"),
        CommandInfo("get_beep", 0xA1, 0x08, 0xA2, _SD, "j.getReaderBeepStatusSendData / j.parseGetReaderBeepStatusData", "Beep on read"),
        CommandInfo("set_beep", 0xA1, 0x07, 0xA2, _SD, "j.getBeepSendData / j.parseBeepData", "Set beep on read"),
        CommandInfo("set_gpo", 0xA1, 0x09, 0xA2, _SD, "j.setIOControlSendData / j.parseSetIOControlReceiveData", "Set GPO outputs and relay"),
        CommandInfo("get_gpi", 0xA1, 0x0A, 0xA2, _SD, "j.getIOControlSendData / j.parseGetIOControlReceiveData", "GPI input levels"),
        CommandInfo("set_trigger_config", 0xA1, 0x0B, 0xA2, _SD, "j.setTriggerWorkModeParaSendData / j.parseSetTriggerWorkModeParaReceiveData", "Set trigger-mode parameters"),
        CommandInfo("get_trigger_config", 0xA1, 0x0C, 0xA2, _SD, "j.getTriggerWorkModeParaSendData / j.parseGetTriggerWorkModeParaReceiveData", "Trigger-mode parameters"),
        CommandInfo("get_antennas", 0x2A, None, 0x2B, _SD, "j.getAntSendData / j.parseGetAntData", "Enabled antennas"),
        CommandInfo("set_antennas", 0x28, None, 0x29, _SD, "j.setAntSendData / j.parseSetAntData", "Enable antennas"),
        CommandInfo("set_antennas_no_save", 0x28, None, 0x29, _INF, "j.setAntSendData with flag 00 (the SDK only ever sends 01)", "Enable antennas without saving"),
        CommandInfo("get_power", 0x12, None, 0x13, _SD, "i.getPowerSendData / i.parseGetPowerAllData", "Per-antenna power"),
        CommandInfo("set_power", 0x10, None, 0x11, _SD, "i.setPowerSendData(int,int) / i.parseSetPowerData", "Set one antenna's power"),
        CommandInfo("set_power_all", 0x10, None, 0x11, _SD, "i.setPowerAllSendData / i.parseSetPowerAllDataData", "Set several antennas' power"),
        CommandInfo("get_region", 0x2E, None, 0x2F, _SD, "i.getFrequencyModeSendData / i.parseGetFrequencyModeData", "Frequency region"),
        CommandInfo("set_region", 0x2C, None, 0x2D, _SD, "i.setFrequencyModeSendData / i.parseSetFrequencyModeData", "Set frequency region"),
        CommandInfo("get_rf_link", 0x54, None, 0x55, _SD, "i.getRFLinkSendData / i.parseGetRFLinkData", "RF link profile"),
        CommandInfo("set_rf_link", 0x52, None, 0x53, _SD, "i.setRFLinkSendData / i.parseSetRFLinkData", "Set RF link profile"),
        CommandInfo("get_gen2", 0x22, None, 0x23, _SD, "i.getGen2SendData / i.parseGetGen2Data", "Gen2 settings"),
        CommandInfo("set_gen2", 0x20, None, 0x21, _SD, "i.setGen2SendData / i.parseSetGen2Data", "Set Gen2 settings"),
        CommandInfo("get_inventory_mode", 0x72, None, 0x73, _SD, "i.getEPCTIDModeSendData / i.parseGetEPCTIDModeData", "EPC / TID / USER report mode"),
        CommandInfo("set_inventory_mode", 0x70, None, 0x71, _SD, "i.setEPC*ModeSendData / i.parseSetEPCAndTIDUserModeData", "Set EPC / TID / USER report mode"),
        CommandInfo("get_antenna_work_time", 0x4C, None, 0x4D, _INF, "j.getAntWorkTimeSendData / j.parseGetAntWorkTimeReceiveData", "Antenna dwell time (reply byte 1 unknown)"),
        CommandInfo("set_antenna_work_time", 0x4A, None, 0x4B, _SD, "j.setAntWorkTimeSendData / j.parseSetAntWorkTimeReceiveData", "Set antenna dwell time"),
        CommandInfo("factory_reset", 0x74, None, 0x75, _SD, "j.getSetSoftResetSendData / j.parseSetSoftResetData (resetUHFSoft: 'Restore factory settings')", "Restore factory settings", guarded=True),
        CommandInfo("set_fastid", 0x5C, None, 0x5D, _SD, "i.setFastIdSendData (UR4 class returns false without sending)", "FastID", supported=False),
        CommandInfo("set_tagfocus", 0x60, None, 0x61, _SD, "i.setTagfocusSendData (UR4 class returns false without sending)", "TagFocus", supported=False),
    )
}  # fmt: skip


# Helpers -----------------------------------------------------------------------


def _expect(
    name: str, frame: Frame, reply_cmd: int, *, sub: int | None = None, min_len: int = 0
) -> bytes:
    """Check CMD, sub-command and length; return the payload."""
    if frame.cmd != reply_cmd:
        raise UnexpectedReplyError(
            f"{name}: expected reply CMD 0x{reply_cmd:02X}, got 0x{frame.cmd:02X}"
        )
    data = frame.data
    if sub is not None and (not data or data[0] != sub):
        got = f"0x{data[0]:02X}" if data else "nothing"
        raise UnexpectedReplyError(
            f"{name}: expected sub-command 0x{sub:02X}, got {got}"
        )
    if len(data) < min_len:
        raise UnexpectedReplyError(
            f"{name}: reply payload is {len(data)} bytes, need at least {min_len}"
        )
    return data


def _expect_status(
    name: str, frame: Frame, reply_cmd: int, *, ok: int = STATUS_OK, min_len: int = 1
) -> bytes:
    """Like :func:`_expect` and also require ``data[0] == ok``."""
    data = _expect(name, frame, reply_cmd, min_len=1)
    if data[0] != ok:
        raise ReaderError(name, data[0])
    if len(data) < min_len:
        raise UnexpectedReplyError(
            f"{name}: reply payload is {len(data)} bytes, need at least {min_len}"
        )
    return data


def _ipv4_bytes(value: str) -> bytes:
    return ipaddress.IPv4Address(value).packed


def _ipv4_str(data: bytes) -> str:
    return str(ipaddress.IPv4Address(bytes(data)))


def _u16(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def _config(name: str, sub: int, payload: bytes = b"", *, get: bool) -> Request:
    return Request(
        name,
        CMD_CONFIG,
        bytes((sub,)) + payload,
        CMD_CONFIG_REPLY,
        sub if get else None,
    )


def _ack(name: str, frame: Frame, reply_cmd: int) -> None:
    """Check a status-only acknowledgement (``data[0] == 0x01``)."""
    _expect_status(name, frame, reply_cmd)


# Version / temperature -------------------------------------------------------


def build_get_version() -> Request:
    """Build *get firmware version* (``0x02``)."""
    return Request("get_version", CMD_GET_VERSION, b"", 0x03)


def parse_get_version(frame: Frame) -> str:
    """Parse ``0x03``: three version bytes, rendered ``V<a>.<b>.<c>``."""
    data = _expect("get_version", frame, 0x03, min_len=3)
    return f"V{data[0]}.{data[1]}.{data[2]}"


def build_get_temperature() -> Request:
    """Build *get module temperature* (``0x34``)."""
    return Request("get_temperature", CMD_GET_TEMPERATURE, b"", 0x35)


def parse_get_temperature(frame: Frame) -> float:
    """Parse ``0x35``: status, then temperature x100 (u16, high byte >= 0xF0 = negative).

    The negative branch mirrors the SDK's ``-((0xFFFF - raw) / 100)``.
    """
    data = _expect_status("get_temperature", frame, 0x35, min_len=3)
    raw = _u16(data, 1)
    if data[1] >= 0xF0:
        return -((0xFFFF - raw) / 100)
    return raw / 100


# Inventory -------------------------------------------------------------------


def build_start_inventory() -> Request:
    """Build *start continuous inventory* (``0x82 00 00``). No reply is awaited."""
    return Request("start_inventory", CMD_START_INVENTORY, b"\x00\x00", None)


def build_stop_inventory() -> Request:
    """Build *stop inventory* (``A5 5A 00 08 8C 84 0D 0A``)."""
    return Request("stop_inventory", CMD_STOP_INVENTORY, b"", 0x8D)


def parse_stop_inventory(frame: Frame) -> None:
    """Check the ``stop_inventory`` acknowledgement (status 0x01)."""
    _ack("stop_inventory", frame, 0x8D)


def build_read_tag_buffer() -> Request:
    """Build *read tag buffer* (``0xE0``). Not used by the SDK's UR4 path."""
    return Request("read_tag_buffer", CMD_READ_TAG_BUFFER, b"", 0xE1)


# Network ---------------------------------------------------------------------


def build_get_network_config() -> Request:
    """Build *get reader IP/port* (``A1 02``)."""
    return _config("get_network_config", SUB_GET_NETWORK, get=True)


def parse_get_network_config(frame: Frame) -> ReaderNetworkConfig:
    """Parse ``A2 02 ip(4) port(2) [mask(4) gateway(4)]``.

    Without mask/gateway (payload < 15 bytes) the SDK assumes
    ``255.255.255.0`` and ``a.b.c.1``; so does this parser.
    """
    data = _expect(
        "get_network_config", frame, CMD_CONFIG_REPLY, sub=SUB_GET_NETWORK, min_len=7
    )
    ip = _ipv4_str(data[1:5])
    port = _u16(data, 5)
    if len(data) >= 15:
        mask = _ipv4_str(data[7:11])
        gateway = _ipv4_str(data[11:15])
    else:
        mask = "255.255.255.0"
        gateway = f"{data[1]}.{data[2]}.{data[3]}.1"
    try:
        return ReaderNetworkConfig(ip=ip, mask=mask, gateway=gateway, port=port)
    except ValidationError as err:
        raise UnexpectedReplyError(f"get_network_config: {err}") from err


def build_set_network_config(config: ReaderNetworkConfig) -> Request:
    """Build *set reader IP/port* (``A1 01 ip(4) port(2) mask(4) gateway(4)``)."""
    payload = (
        _ipv4_bytes(config.ip)
        + config.port.to_bytes(2, "big")
        + _ipv4_bytes(config.mask)
        + _ipv4_bytes(config.gateway)
    )
    return _config("set_network_config", SUB_SET_NETWORK, payload, get=False)


def parse_set_network_config(frame: Frame) -> None:
    """Check the ``set_network_config`` acknowledgement (status 0x01)."""
    _ack("set_network_config", frame, CMD_CONFIG_REPLY)


def build_get_udp_destination() -> Request:
    """Build *get UDP destination* (``A1 04``)."""
    return _config("get_udp_destination", SUB_GET_DESTINATION, get=True)


def parse_get_udp_destination(frame: Frame) -> UdpDestination | None:
    """Parse ``A2 04 ip(4) port(2)``; ``None`` if unset (0.0.0.0 or port 0)."""
    data = _expect(
        "get_udp_destination",
        frame,
        CMD_CONFIG_REPLY,
        sub=SUB_GET_DESTINATION,
        min_len=7,
    )
    ip = _ipv4_str(data[1:5])
    port = _u16(data, 5)
    if ip == "0.0.0.0" or port == 0:
        return None
    try:
        return UdpDestination(ip=ip, port=port)
    except ValidationError as err:
        raise UnexpectedReplyError(f"get_udp_destination: {err}") from err


def build_set_udp_destination(dest: UdpDestination) -> Request:
    """Build *set UDP destination* (``A1 03 ip(4) port(2)``)."""
    payload = _ipv4_bytes(dest.ip) + dest.port.to_bytes(2, "big")
    return _config("set_udp_destination", SUB_SET_DESTINATION, payload, get=False)


def parse_set_udp_destination(frame: Frame) -> None:
    """Check the ``set_udp_destination`` acknowledgement (status 0x01)."""
    _ack("set_udp_destination", frame, CMD_CONFIG_REPLY)


# Work mode, beep, GPIO, trigger ----------------------------------------------


def build_get_work_mode() -> Request:
    """Build *get work mode* (``A1 06``)."""
    return _config("get_work_mode", SUB_GET_WORK_MODE, get=True)


def parse_get_work_mode(frame: Frame) -> WorkMode:
    """Parse ``A2 06 mode``."""
    data = _expect(
        "get_work_mode", frame, CMD_CONFIG_REPLY, sub=SUB_GET_WORK_MODE, min_len=2
    )
    try:
        return WorkMode(data[1])
    except ValueError as err:
        raise UnexpectedReplyError(f"get_work_mode: unknown mode {data[1]}") from err


def build_set_work_mode(mode: WorkMode) -> Request:
    """Build *set work mode* (``A1 05 mode``)."""
    try:
        value = WorkMode(mode)
    except ValueError as err:
        raise ValidationError(f"unknown work mode {mode!r}") from err
    return _config("set_work_mode", SUB_SET_WORK_MODE, bytes((value,)), get=False)


def parse_set_work_mode(frame: Frame) -> None:
    """Check the ``set_work_mode`` acknowledgement (status 0x01)."""
    _ack("set_work_mode", frame, CMD_CONFIG_REPLY)


def build_get_beep() -> Request:
    """Build *get beep state* (``A1 08``)."""
    return _config("get_beep", SUB_GET_BEEP, get=True)


def parse_get_beep(frame: Frame) -> bool:
    """Parse ``A2 08 state``; non-zero = beep on."""
    data = _expect("get_beep", frame, CMD_CONFIG_REPLY, sub=SUB_GET_BEEP, min_len=2)
    return data[1] != 0


def build_set_beep(enabled: bool) -> Request:
    """Build *set beep* (``A1 07 00|01``)."""
    return _config("set_beep", SUB_SET_BEEP, b"\x01" if enabled else b"\x00", get=False)


def parse_set_beep(frame: Frame) -> None:
    """Check the ``set_beep`` acknowledgement (status 0x01)."""
    _ack("set_beep", frame, CMD_CONFIG_REPLY)


def build_get_gpi() -> Request:
    """Build *get GPI levels* (``A1 0A``)."""
    return _config("get_gpi", SUB_GET_GPI, get=True)


def parse_get_gpi(frame: Frame) -> GpiState:
    """Parse ``A2 0A gpi1 gpi2`` (1 = high)."""
    data = _expect("get_gpi", frame, CMD_CONFIG_REPLY, sub=SUB_GET_GPI, min_len=3)
    return GpiState(input1=data[1] != 0, input2=data[2] != 0)


def build_set_gpo(state: GpoState) -> Request:
    """Build *set GPO* (``A1 09 output1 output2 relay``), each 0 or 1."""
    payload = bytes((int(state.output1), int(state.output2), int(state.relay)))
    return _config("set_gpo", SUB_SET_GPO, payload, get=False)


def parse_set_gpo(frame: Frame) -> None:
    """Check the ``set_gpo`` acknowledgement (status 0x01)."""
    _ack("set_gpo", frame, CMD_CONFIG_REPLY)


def build_get_trigger_config() -> Request:
    """Build *get trigger parameters* (``A1 0C``)."""
    return _config("get_trigger_config", SUB_GET_TRIGGER, get=True)


def parse_get_trigger_config(frame: Frame) -> TriggerConfig:
    """Parse ``A2 0C gpi work_time(2) interval(2) report``."""
    data = _expect(
        "get_trigger_config", frame, CMD_CONFIG_REPLY, sub=SUB_GET_TRIGGER, min_len=7
    )
    try:
        return TriggerConfig(
            gpi=data[1] + 1,
            work_time=_u16(data, 2),
            interval=_u16(data, 4),
            report=TriggerReport(data[6]),
        )
    except (ValueError, ValidationError) as err:
        raise UnexpectedReplyError(f"get_trigger_config: {err}") from err


def build_set_trigger_config(config: TriggerConfig) -> Request:
    """Build *set trigger parameters* (``A1 0B gpi work(2) interval(2) report 00``).

    The trailing ``00`` matches the SDK, which allocates 8 bytes and fills 7.
    """
    payload = (
        bytes((config.gpi - 1,))
        + config.work_time.to_bytes(2, "big")
        + config.interval.to_bytes(2, "big")
        + bytes((int(config.report), 0))
    )
    return _config("set_trigger_config", SUB_SET_TRIGGER, payload, get=False)


def parse_set_trigger_config(frame: Frame) -> None:
    """Check the ``set_trigger_config`` acknowledgement (status 0x01)."""
    _ack("set_trigger_config", frame, CMD_CONFIG_REPLY)


# Antennas, power, region -----------------------------------------------------


def _antenna_mask(antennas: Iterable[int]) -> bytes:
    mask = 0
    for ant in antennas:
        mask |= 1 << (check_antenna(ant) - 1)
    # Wire order: [ANT9-16, ANT1-8].
    return bytes(((mask >> 8) & 0xFF, mask & 0xFF))


def build_get_antennas() -> Request:
    """Build *get enabled antennas* (``0x2A``)."""
    return Request("get_antennas", CMD_GET_ANTENNAS, b"", 0x2B)


def parse_get_antennas(frame: Frame) -> AntennaState:
    """Parse ``0x2B hi lo``: bit n-1 of ``lo`` = antenna n."""
    data = _expect("get_antennas", frame, 0x2B, min_len=2)
    lo = data[1]
    return AntennaState(
        frozenset(
            ant for ant in range(ANTENNA_MIN, ANTENNA_MAX + 1) if lo & (1 << (ant - 1))
        )
    )


#: Save flags for *set antennas*. The SDK always sends ``0x01``; ``0x00``
#: ("don't save") is inferred and has not been seen in the SDK.
ANT_SAVE = 0x01
ANT_NO_SAVE = 0x00


def build_set_antennas(state: AntennaState, *, save: bool = True) -> Request:
    """Build *set antennas* (``0x28 flag hi lo``).

    ``flag`` is ``01`` (save; what the SDK always sends) or, with
    ``save=False``, ``00`` (inferred: never seen in the SDK).
    """
    if not state.enabled:
        raise ValidationError("at least one antenna must stay enabled")
    flag = ANT_SAVE if save else ANT_NO_SAVE
    return Request(
        "set_antennas" if save else "set_antennas_no_save",
        CMD_SET_ANTENNAS,
        bytes((flag,)) + _antenna_mask(state.enabled),
        0x29,
    )


def parse_set_antennas(frame: Frame) -> None:
    """Check the ``set_antennas`` acknowledgement (status 0x01)."""
    _ack("set_antennas", frame, 0x29)


def build_set_antennas_no_save(state: AntennaState) -> Request:
    """Build *set antennas* with the inferred "don't save" flag (``0x28 00 hi lo``)."""
    return build_set_antennas(state, save=False)


def parse_set_antennas_no_save(frame: Frame) -> None:
    """Check the ``set_antennas_no_save`` acknowledgement (status 0x01)."""
    _ack("set_antennas_no_save", frame, 0x29)


def _check_power(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not POWER_MIN_DBM <= value <= POWER_MAX_DBM
    ):
        raise ValidationError(
            f"power must be an integer {POWER_MIN_DBM}-{POWER_MAX_DBM} dBm, got {value!r}"
        )
    return value


def _power_record(antenna: int, dbm: int) -> bytes:
    centi = _check_power(dbm) * 100
    return bytes((check_antenna(antenna),)) + centi.to_bytes(2, "big") * 2


def build_get_power() -> Request:
    """Build *get power* (``0x12``)."""
    return Request("get_power", CMD_GET_POWER, b"", 0x13)


def parse_get_power(frame: Frame) -> list[AntennaPower]:
    """Parse ``0x13 00 (ant read(2) write(2))*``; status ``00`` = OK.

    As in the SDK: antenna 0 means antenna 1, a power of 0 means "same as
    antenna 1". Records for antennas above 4 are ignored (the UR4 has 4 ports).
    """
    data = _expect_status("get_power", frame, 0x13, ok=0x00, min_len=4)
    count = (len(data) - 1) // 5
    default: float | None = None
    raw: list[tuple[int, float, float]] = []
    for index in range(count):
        offset = 1 + index * 5
        ant = data[offset] or 1
        read = _u16(data, offset + 1) / 100
        write = _u16(data, offset + 3) / 100
        if ant == 1:
            default = read
        raw.append((ant, read, write))
    out: list[AntennaPower] = []
    seen: set[int] = set()
    for ant, read_raw, write_raw in raw:
        if ant > ANTENNA_MAX or ant in seen:
            continue
        seen.add(ant)
        read = read_raw or (default or 0.0)
        write = write_raw or read
        try:
            out.append(AntennaPower(antenna=ant, power=read, write_power=write))
        except ValidationError as err:
            raise UnexpectedReplyError(f"get_power: {err}") from err
    if not out:
        raise UnexpectedReplyError("get_power: no antenna records in reply")
    return sorted(out, key=lambda p: p.antenna)


def build_set_power(antenna: int, dbm: int) -> Request:
    """Build *set power* for one antenna (``0x10 02 ant read(2) write(2)``, dBm x100)."""
    return Request(
        "set_power", CMD_SET_POWER, b"\x02" + _power_record(antenna, dbm), 0x11
    )


def parse_set_power(frame: Frame) -> None:
    """Check the ``set_power`` acknowledgement (status 0x01)."""
    _ack("set_power", frame, 0x11)


def build_set_power_all(powers: Iterable[tuple[int, int]]) -> Request:
    """Build *set power* for several antennas (``0x10 02 (ant read(2) write(2))*``)."""
    items = list(powers)
    if not items:
        raise ValidationError("no antenna powers given")
    if len({ant for ant, _ in items}) != len(items):
        raise ValidationError("an antenna appears more than once")
    payload = b"".join(_power_record(ant, dbm) for ant, dbm in items)
    return Request("set_power_all", CMD_SET_POWER, b"\x02" + payload, 0x11)


def parse_set_power_all(frame: Frame) -> None:
    """Check the ``set_power_all`` acknowledgement (status 0x01)."""
    _ack("set_power_all", frame, 0x11)


def build_get_region() -> Request:
    """Build *get frequency region* (``0x2E``)."""
    return Request("get_region", CMD_GET_REGION, b"", 0x2F)


def parse_get_region(frame: Frame) -> FrequencyRegion | int:
    """Parse ``0x2F 01 code``; unknown codes are returned as ``int``."""
    data = _expect_status("get_region", frame, 0x2F, min_len=2)
    try:
        return FrequencyRegion(data[1])
    except ValueError:
        return data[1]


def build_set_region(region: FrequencyRegion | int) -> Request:
    """Build *set frequency region* (``0x2C 01 code``); ``01`` is the save flag."""
    try:
        code = FrequencyRegion(region)
    except ValueError as err:
        raise ValidationError(f"unknown region code {region!r}") from err
    return Request("set_region", CMD_SET_REGION, bytes((0x01, code)), 0x2D)


def parse_set_region(frame: Frame) -> None:
    """Check the ``set_region`` acknowledgement (status 0x01)."""
    _ack("set_region", frame, 0x2D)


# RF link, Gen2, inventory mode, antenna dwell --------------------------------


def build_get_rf_link() -> Request:
    """Build *get RF link* (``0x54 00 00``)."""
    return Request("get_rf_link", CMD_GET_RF_LINK, b"\x00\x00", 0x55)


def parse_get_rf_link(frame: Frame) -> RfLink | int:
    """Parse ``0x55 01 ?? link``; unknown values are returned as ``int``."""
    data = _expect_status("get_rf_link", frame, 0x55, min_len=3)
    try:
        return RfLink(data[2])
    except ValueError:
        return data[2]


def build_set_rf_link(link: RfLink) -> Request:
    """Build *set RF link* (``0x52 00 01 link``)."""
    try:
        value = RfLink(link)
    except ValueError as err:
        raise ValidationError(f"unknown RF link {link!r}") from err
    return Request("set_rf_link", CMD_SET_RF_LINK, bytes((0x00, 0x01, value)), 0x53)


def parse_set_rf_link(frame: Frame) -> None:
    """Check the ``set_rf_link`` acknowledgement (status 0x01)."""
    _ack("set_rf_link", frame, 0x53)


def build_get_gen2() -> Request:
    """Build *get Gen2 settings* (``0x22``)."""
    return Request("get_gen2", CMD_GET_GEN2, b"", 0x23)


def _pack_gen2(s: Gen2Settings) -> bytes:
    return bytes(
        (
            (s.select_target << 5)
            | (s.select_action << 2)
            | (s.select_truncate << 1)
            | s.q,
            (s.start_q << 4) | s.min_q,
            (s.max_q << 4) | (s.query_dr << 3) | (s.query_m << 1) | s.query_trext,
            (s.query_sel << 6)
            | (s.query_session << 4)
            | (s.query_target << 3)
            | s.link_frequency,
        )
    )


def parse_get_gen2(frame: Frame) -> Gen2Settings:
    """Parse ``0x23 b0 b1 b2 b3`` (bit-packed, no status byte)."""
    d = _expect("get_gen2", frame, 0x23, min_len=4)
    return Gen2Settings(
        select_target=(d[0] >> 5) & 0x07,
        select_action=(d[0] >> 2) & 0x07,
        select_truncate=(d[0] >> 1) & 0x01,
        q=d[0] & 0x01,
        start_q=(d[1] >> 4) & 0x0F,
        min_q=d[1] & 0x0F,
        max_q=(d[2] >> 4) & 0x0F,
        query_dr=(d[2] >> 3) & 0x01,
        query_m=(d[2] >> 1) & 0x03,
        query_trext=d[2] & 0x01,
        query_sel=(d[3] >> 6) & 0x03,
        query_session=(d[3] >> 4) & 0x03,
        query_target=(d[3] >> 3) & 0x01,
        link_frequency=d[3] & 0x07,
    )


def build_set_gen2(settings: Gen2Settings) -> Request:
    """Build *set Gen2 settings* (``0x20 b0 b1 b2 b3``)."""
    return Request("set_gen2", CMD_SET_GEN2, _pack_gen2(settings), 0x21)


def parse_set_gen2(frame: Frame) -> None:
    """Check the ``set_gen2`` acknowledgement (status 0x01)."""
    _ack("set_gen2", frame, 0x21)


def build_get_inventory_mode() -> Request:
    """Build *get inventory mode* (``0x72 00 00``)."""
    return Request("get_inventory_mode", CMD_GET_INVENTORY_MODE, b"\x00\x00", 0x73)


def parse_get_inventory_mode(frame: Frame) -> InventoryModeConfig:
    """Parse ``0x73 01 mode user_offset user_length``."""
    data = _expect_status("get_inventory_mode", frame, 0x73, min_len=4)
    try:
        mode = InventoryMode(data[1])
    except ValueError as err:
        raise UnexpectedReplyError(
            f"get_inventory_mode: unknown mode {data[1]}"
        ) from err
    if mode is InventoryMode.EPC_TID_USER:
        return InventoryModeConfig(mode, data[2], data[3])
    return InventoryModeConfig(mode)


def build_set_inventory_mode(config: InventoryModeConfig) -> Request:
    """Build *set inventory mode* (``0x70 01 mode user_offset user_length``)."""
    if config.mode is InventoryMode.EPC_TID_USER:
        if config.user_length == 0:
            raise ValidationError("EPC_TID_USER needs user_length > 0")
        tail = bytes((config.user_offset, config.user_length))
    else:
        tail = b"\x00\x00"
    return Request(
        "set_inventory_mode",
        CMD_SET_INVENTORY_MODE,
        bytes((0x01, int(config.mode))) + tail,
        0x71,
    )


def parse_set_inventory_mode(frame: Frame) -> None:
    """Check the ``set_inventory_mode`` acknowledgement (status 0x01)."""
    _ack("set_inventory_mode", frame, 0x71)


def build_get_antenna_work_time(antenna: int) -> Request:
    """Build *get antenna dwell time* (``0x4C ant 00``)."""
    return Request(
        "get_antenna_work_time",
        CMD_GET_ANT_WORK_TIME,
        bytes((check_antenna(antenna), 0)),
        0x4D,
    )


def parse_get_antenna_work_time(frame: Frame) -> int:
    """Parse ``0x4D 01 ?? time(2)``: dwell time in ms.

    The SDK reads bytes 2-3 after checking only for 2 bytes; this parser needs 4.
    """
    data = _expect_status("get_antenna_work_time", frame, 0x4D, min_len=4)
    return _u16(data, 2)


def build_set_antenna_work_time(antenna: int, ms: int) -> Request:
    """Build *set antenna dwell time* (``0x4A ant time(2)``), 10-65535 ms."""
    if (
        isinstance(ms, bool)
        or not isinstance(ms, int)
        or not ANTENNA_WORK_TIME_MIN_MS <= ms <= ANTENNA_WORK_TIME_MAX_MS
    ):
        raise ValidationError(
            f"work time must be {ANTENNA_WORK_TIME_MIN_MS}-{ANTENNA_WORK_TIME_MAX_MS} ms"
        )
    payload = bytes((check_antenna(antenna),)) + ms.to_bytes(2, "big")
    return Request("set_antenna_work_time", CMD_SET_ANT_WORK_TIME, payload, 0x4B)


def parse_set_antenna_work_time(frame: Frame) -> None:
    """Check the ``set_antenna_work_time`` acknowledgement (status 0x01)."""
    _ack("set_antenna_work_time", frame, 0x4B)


# Destructive / unsupported -----------------------------------------------------


def build_factory_reset() -> Request:
    """Build *restore factory settings* (``0x74``, SDK ``resetUHFSoft``)."""
    return Request("factory_reset", CMD_FACTORY_RESET, b"", 0x75)


def parse_factory_reset(frame: Frame) -> None:
    """Check the ``factory_reset`` acknowledgement (status 0x01)."""
    _ack("factory_reset", frame, 0x75)


def build_set_fastid(enabled: bool) -> Request:
    """Build *FastID* (``0x5C 00|01 00``). The SDK's UR4 class never sends it."""
    return Request("set_fastid", CMD_SET_FASTID, bytes((int(enabled), 0)), 0x5D)


def build_set_tagfocus(enabled: bool) -> Request:
    """Build *TagFocus* (``0x60 00|01 00``). The SDK's UR4 class never sends it."""
    return Request("set_tagfocus", CMD_SET_TAGFOCUS, bytes((int(enabled), 0)), 0x61)


# Discovery -------------------------------------------------------------------


def parse_discovery(datagram: bytes) -> DiscoveredReader:
    """Parse a UDP/1111 announcement: ``MAC(6) IP(4) PORT(2)``.

    Raises:
        UnexpectedReplyError: shorter than 12 bytes.

    """
    if len(datagram) < 12:
        raise UnexpectedReplyError(
            f"discovery datagram is {len(datagram)} bytes, need 12"
        )
    mac = ":".join(f"{b:02X}" for b in datagram[:6])
    ip = _ipv4_str(datagram[6:10])
    port = _u16(datagram, 10)
    return DiscoveredReader(mac=mac, ip=ip, port=port)
