"""Frozen, validated data models.

Every model checks its fields in ``__post_init__`` and raises
:class:`~aiochainway.protocol.errors.ValidationError` before anything is sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
import ipaddress

from .protocol.errors import ValidationError

#: The UR4 has four antenna ports.
ANTENNA_MIN = 1
ANTENNA_MAX = 4
#: ``i.setPowerSendData`` clamps to 1..30 dBm and the demo offers 1..30.
POWER_MIN_DBM = 1
POWER_MAX_DBM = 30
#: ``RFIDWithUHFNetworkUR4.setAntennaWorkTime`` rejects values outside 10..65535 ms.
ANTENNA_WORK_TIME_MIN_MS = 10
ANTENNA_WORK_TIME_MAX_MS = 0xFFFF


def _check_ipv4(name: str, value: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError) as err:
        raise ValidationError(f"{name} is not an IPv4 address: {value!r}") from err


def _check_port(name: str, value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 0xFFFF
    ):
        raise ValidationError(f"{name} must be an integer 1-65535, got {value!r}")


def check_antenna(value: int) -> int:
    """Validate an antenna number (1-4) and return it."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not ANTENNA_MIN <= value <= ANTENNA_MAX
    ):
        raise ValidationError(
            f"antenna must be {ANTENNA_MIN}-{ANTENNA_MAX}, got {value!r}"
        )
    return value


class TagSource(StrEnum):
    """Where a tag read came from."""

    TCP = "tcp"
    UDP = "udp"


@dataclass(frozen=True, slots=True)
class TagRead:
    """One tag report (reply CMD ``0x83``).

    ``epc``, ``tid`` and ``user`` are upper-case hex strings. ``rssi_dbm`` is
    ``None`` when the reader's value is outside the plausible range.
    ``received_at`` is a UNIX timestamp. ``source`` is ``"tcp"``/``"udp"`` plus
    the reader address, e.g. ``"udp:192.168.99.202:8888"``.
    """

    epc: str
    pc: int
    antenna: int
    rssi_dbm: float | None = None
    tid: str | None = None
    user: str | None = None
    received_at: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        """Validate fields."""
        check_antenna(self.antenna)
        if not 0 <= self.pc <= 0xFFFF:
            raise ValidationError(f"PC out of range: {self.pc!r}")
        for name in ("epc", "tid", "user"):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                bytes.fromhex(value)
            except ValueError as err:
                raise ValidationError(f"{name} is not hex: {value!r}") from err
            if value != value.upper():
                raise ValidationError(f"{name} must be upper-case hex: {value!r}")


@dataclass(frozen=True, slots=True)
class ReaderNetworkConfig:
    """The reader's own static IPv4 settings and TCP command port."""

    ip: str
    mask: str
    gateway: str
    port: int = 8888

    def __post_init__(self) -> None:
        """Validate: IPv4 fields, contiguous mask, gateway on the reader's subnet."""
        ip = _check_ipv4("ip", self.ip)
        _check_ipv4("mask", self.mask)
        gateway = _check_ipv4("gateway", self.gateway)
        _check_port("port", self.port)
        try:
            network = ipaddress.IPv4Network(f"{self.ip}/{self.mask}", strict=False)
        except ValueError as err:
            raise ValidationError(f"mask is not a valid netmask: {self.mask}") from err
        if network.prefixlen == 0 or network.prefixlen > 30:
            raise ValidationError(f"mask /{network.prefixlen} is not usable")
        if ip in (network.network_address, network.broadcast_address):
            raise ValidationError(f"{ip} is the network or broadcast address")
        if gateway not in network:
            raise ValidationError(f"gateway {gateway} is not in {network}")
        if ip.is_multicast or ip.is_loopback or ip.is_unspecified:
            raise ValidationError(f"{ip} cannot be a reader address")


@dataclass(frozen=True, slots=True)
class UdpDestination:
    """Where the reader sends tag frames in auto and trigger mode."""

    ip: str
    port: int

    def __post_init__(self) -> None:
        """Validate the IPv4 address and port."""
        ip = _check_ipv4("ip", self.ip)
        _check_port("port", self.port)
        if ip.is_unspecified or ip.is_multicast:
            raise ValidationError(f"{ip} cannot be a UDP destination")


class WorkMode(IntEnum):
    """Reader work mode (``0xA1`` sub ``05``/``06``)."""

    COMMAND = 0
    AUTO = 1
    TRIGGER = 2


class FrequencyRegion(IntEnum):
    """Region / frequency-band codes (CMD ``0x2C``/``0x2E``).

    Codes come from the SDK javadoc for ``setFrequencyMode`` and the demo's
    region list. ``0x34`` is "New Zealand" in the javadoc and "Taiwan" in the demo.
    """

    CHINA_1 = 0x01
    CHINA_2 = 0x02
    EUROPE = 0x04
    USA = 0x08
    KOREA = 0x16
    JAPAN = 0x32
    SOUTH_AFRICA = 0x33
    TAIWAN_OR_NEW_ZEALAND = 0x34
    PERU = 0x36
    RUSSIA = 0x37
    MOROCCO = 0x80


class RfLink(IntEnum):
    """RF link profile (CMD ``0x52``/``0x54``), per the SDK javadoc."""

    DSB_ASK_FM0_40K = 0
    PR_ASK_MILLER4_250K = 1
    PR_ASK_MILLER4_300K = 2
    DSB_ASK_FM0_400K = 3


class InventoryMode(IntEnum):
    """Which memory banks a tag report carries (CMD ``0x70``/``0x72``)."""

    EPC = 0
    EPC_TID = 1
    EPC_TID_USER = 2


class TriggerReport(IntEnum):
    """Where trigger-mode reads go (C# demo ``SetWorkModePara``)."""

    SERIAL = 0
    UDP = 1


@dataclass(frozen=True, slots=True)
class AntennaState:
    """Which antenna ports are enabled."""

    enabled: frozenset[int]

    def __post_init__(self) -> None:
        """Validate antenna numbers."""
        for ant in self.enabled:
            check_antenna(ant)

    @classmethod
    def of(cls, *antennas: int) -> AntennaState:
        """Build from antenna numbers, e.g. ``AntennaState.of(1, 2)``."""
        return cls(frozenset(antennas))

    def is_enabled(self, antenna: int) -> bool:
        """Return whether ``antenna`` is enabled."""
        return check_antenna(antenna) in self.enabled


@dataclass(frozen=True, slots=True)
class AntennaPower:
    """Output power of one antenna, in dBm (read and write power)."""

    antenna: int
    power: float
    write_power: float | None = None

    def __post_init__(self) -> None:
        """Validate antenna number and the 1-30 dBm range."""
        check_antenna(self.antenna)
        for name in ("power", "write_power"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not POWER_MIN_DBM <= value <= POWER_MAX_DBM:
                raise ValidationError(
                    f"{name} must be {POWER_MIN_DBM}-{POWER_MAX_DBM} dBm, got {value!r}"
                )


@dataclass(frozen=True, slots=True)
class GpiState:
    """GPI input levels (``True`` = high)."""

    input1: bool
    input2: bool


@dataclass(frozen=True, slots=True)
class GpoState:
    """GPO output levels and relay (``True`` = high / relay closed)."""

    output1: bool = False
    output2: bool = False
    relay: bool = False


@dataclass(frozen=True, slots=True)
class TriggerConfig:
    """Trigger-mode parameters (``0xA1`` sub ``0B``/``0C``).

    ``gpi`` is the input that starts a read (1 or 2). ``work_time`` and
    ``interval`` are in units of 10 ms according to Chainway's C# demo.
    """

    gpi: int = 1
    work_time: int = 100
    interval: int = 0
    report: TriggerReport = TriggerReport.UDP

    def __post_init__(self) -> None:
        """Validate ranges."""
        if self.gpi not in (1, 2):
            raise ValidationError(f"gpi must be 1 or 2, got {self.gpi!r}")
        for name in ("work_time", "interval"):
            value = getattr(self, name)
            if isinstance(value, bool) or not 0 <= value <= 0xFFFF:
                raise ValidationError(f"{name} must be 0-65535, got {value!r}")
        if not isinstance(self.report, TriggerReport):
            raise ValidationError(f"report must be a TriggerReport: {self.report!r}")


@dataclass(frozen=True, slots=True)
class InventoryModeConfig:
    """Memory banks reported per tag, with the USER window for EPC+TID+USER."""

    mode: InventoryMode = InventoryMode.EPC
    user_offset: int = 0
    user_length: int = 0

    def __post_init__(self) -> None:
        """Validate the USER window fits in a byte each."""
        for name in ("user_offset", "user_length"):
            value = getattr(self, name)
            if isinstance(value, bool) or not 0 <= value <= 0xFF:
                raise ValidationError(f"{name} must be 0-255, got {value!r}")


@dataclass(frozen=True, slots=True)
class Gen2Settings:
    """EPC Gen2 air-protocol settings (CMD ``0x20``/``0x22``), raw field values."""

    select_target: int = 0
    select_action: int = 0
    select_truncate: int = 0
    q: int = 1
    start_q: int = 4
    min_q: int = 0
    max_q: int = 15
    query_dr: int = 0
    query_m: int = 0
    query_trext: int = 1
    query_sel: int = 0
    query_session: int = 0
    query_target: int = 0
    link_frequency: int = 0

    #: Bit width of each field in the packed 4-byte payload.
    WIDTHS = {
        "select_target": 3,
        "select_action": 3,
        "select_truncate": 1,
        "q": 1,
        "start_q": 4,
        "min_q": 4,
        "max_q": 4,
        "query_dr": 1,
        "query_m": 2,
        "query_trext": 1,
        "query_sel": 2,
        "query_session": 2,
        "query_target": 1,
        "link_frequency": 3,
    }

    def __post_init__(self) -> None:
        """Validate each field fits its bit width."""
        for name, width in self.WIDTHS.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not 0 <= value < (1 << width):
                raise ValidationError(
                    f"{name} must fit in {width} bit(s), got {value!r}"
                )


@dataclass(frozen=True, slots=True)
class ReaderInfo:
    """Firmware version and module temperature."""

    version: str
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredReader:
    """A reader announcing itself on UDP port 1111."""

    mac: str
    ip: str
    port: int


@dataclass(frozen=True, slots=True)
class NetworkChangeResult:
    """Result of ``set_network_config``: the reader accepted it, not yet verified."""

    requested: ReaderNetworkConfig
    accepted: bool
    message: str = field(
        default=(
            "The reader acknowledged the new settings. It may need a power cycle. "
            "Reconnect at the new address and read the settings back to verify."
        )
    )


class SweepEndReason(StrEnum):
    """Why an inventory sweep ended (:attr:`SweepResult.end_reason`).

    The rule is about the reader, not the consumer: a sweep is
    ``DURATION_ELAPSED`` when *stop* was sent after the requested duration had
    run out and nothing went wrong, whether or not the consumer read every
    tag (all reports are counted in :class:`SweepResult` either way). Inside
    ``async with``, ``break`` ends the loop, not the sweep; the sweep ends when
    the block is left.
    """

    #: Stop was sent after the requested duration ran out. The only
    #: "completed" outcome.
    DURATION_ELAPSED = "duration_elapsed"
    #: Stop was sent before the requested duration ran out (the block was left
    #: early, ``aclose()`` was called, the task was cancelled, or an abandoned
    #: stream was collected), or no duration was given.
    STOPPED = "stopped"
    #: The TCP connection dropped or the client was closed during the sweep.
    CONNECTION_LOST = "connection_lost"
    #: The sweep failed to start, or the consumer's code raised inside it.
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SweepResult:
    """Summary of one inventory sweep, available once the stream has ended.

    ``started_at``/``ended_at`` are UNIX timestamps (``started_at`` is ``None``
    if start was never sent). ``reports`` counts every tag report received,
    including repeats and reports dropped because the consumer was too slow
    (``dropped``). ``per_antenna_counts`` maps antenna → reports.
    """

    end_reason: SweepEndReason
    started_at: float | None
    ended_at: float
    reports: int = 0
    unique_epcs: frozenset[str] = frozenset()
    per_antenna_counts: dict[int, int] = field(default_factory=dict)
    requested_duration: float | None = None
    dropped: int = 0
    #: The stop frame was written to the socket.
    stop_sent: bool = False
    #: The reader acknowledged stop (0x8D) within the client's ``stop_drain``.
    stop_acknowledged: bool = False
    #: ``repr`` of the exception for :attr:`SweepEndReason.ERROR`.
    error: str | None = None

    @property
    def completed(self) -> bool:
        """Whether the sweep ran for its full requested duration."""
        return self.end_reason is SweepEndReason.DURATION_ELAPSED

    @property
    def unique_count(self) -> int:
        """Number of distinct EPCs seen."""
        return len(self.unique_epcs)

    @property
    def elapsed(self) -> float | None:
        """Seconds from start to end, or ``None`` if it never started."""
        if self.started_at is None:
            return None
        return self.ended_at - self.started_at
