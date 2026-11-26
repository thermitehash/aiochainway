"""Guarded reader provisioning.

:func:`provision` reads the current settings, then applies only what differs,
in a safe order:

1. stop any inventory
2. region
3. antennas
4. power
5. UDP destination
6. work mode
7. IP / mask / gateway / port **last**, because the reader drops the TCP
   connection when its address changes.

Network-changing steps (destination, IP) need ``confirm=True``. ``dry_run=True``
builds the same plan and returns the frames without connecting.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import logging

from .client import UR4Client, check_guard
from .models import (
    AntennaPower,
    AntennaState,
    FrequencyRegion,
    ReaderNetworkConfig,
    UdpDestination,
    WorkMode,
    check_antenna,
)
from .protocol import commands as c
from .protocol.errors import ChainwayError, ValidationError

_LOGGER = logging.getLogger("aiochainway")

ClientFactory = Callable[..., UR4Client]


@dataclass(frozen=True, slots=True)
class ProvisionStep:
    """One step of a provisioning plan."""

    name: str
    description: str
    request: c.Request
    #: ``"planned"``, ``"sent"``, ``"skipped"`` (already set) or ``"failed"``.
    outcome: str = "planned"
    detail: str = ""

    @property
    def frame_hex(self) -> str:
        """The request frame as spaced hex."""
        return self.request.encode().hex(" ").upper()


@dataclass(slots=True)
class ProvisionResult:
    """What :func:`provision` did."""

    steps: list[ProvisionStep] = field(default_factory=list)
    dry_run: bool = False
    network_changed: bool = False
    message: str = ""

    @property
    def sent(self) -> list[ProvisionStep]:
        """Steps actually sent to the reader."""
        return [s for s in self.steps if s.outcome == "sent"]


@dataclass(frozen=True, slots=True)
class ProvisionTarget:
    """Desired settings; ``None`` fields are left alone."""

    network: ReaderNetworkConfig | None = None
    udp_dest: UdpDestination | None = None
    region: FrequencyRegion | None = None
    antennas: AntennaState | None = None
    power: Mapping[int, int] | None = None
    work_mode: WorkMode | None = None


def make_target(
    *,
    new_ip: str | None = None,
    mask: str | None = None,
    gateway: str | None = None,
    tcp_port: int = c.DEFAULT_TCP_PORT,
    udp_dest: UdpDestination | tuple[str, int] | None = None,
    region: FrequencyRegion | None = None,
    antennas: Iterable[int] | None = None,
    power: int | Mapping[int, int] | None = None,
    work_mode: WorkMode | None = None,
) -> ProvisionTarget:
    """Validate loose arguments into a :class:`ProvisionTarget`.

    ``power`` as an ``int`` applies to every antenna in ``antennas`` (or 1-4).
    """
    network = None
    if new_ip is not None or mask is not None or gateway is not None:
        if new_ip is None or mask is None or gateway is None:
            raise ValidationError("new_ip, mask and gateway must be given together")
        network = ReaderNetworkConfig(
            ip=new_ip, mask=mask, gateway=gateway, port=tcp_port
        )
    dest = UdpDestination(*udp_dest) if isinstance(udp_dest, tuple) else udp_dest
    ant_state = AntennaState(frozenset(antennas)) if antennas is not None else None
    if ant_state is not None and not ant_state.enabled:
        raise ValidationError("at least one antenna must be enabled")
    power_map: dict[int, int] | None = None
    if isinstance(power, int):
        targets = sorted(ant_state.enabled) if ant_state else [1, 2, 3, 4]
        power_map = dict.fromkeys(targets, power)
    elif power is not None:
        power_map = {check_antenna(a): p for a, p in power.items()}
    if power_map is not None:
        for ant, dbm in power_map.items():
            AntennaPower(antenna=ant, power=dbm)  # validates the range
    try:
        region_enum = FrequencyRegion(region) if region is not None else None
        mode_enum = WorkMode(work_mode) if work_mode is not None else None
    except ValueError as err:
        raise ValidationError(str(err)) from err
    return ProvisionTarget(
        network=network,
        udp_dest=dest,
        region=region_enum,
        antennas=ant_state,
        power=power_map,
        work_mode=mode_enum,
    )


def plan(target: ProvisionTarget) -> list[ProvisionStep]:
    """Return the ordered steps for ``target`` (pure; nothing is sent)."""
    steps: list[ProvisionStep] = [
        ProvisionStep(
            "stop_inventory", "Stop any running inventory", c.build_stop_inventory()
        )
    ]
    if target.region is not None:
        steps.append(
            ProvisionStep(
                "set_region",
                f"Region {target.region.name}",
                c.build_set_region(target.region),
            )
        )
    if target.antennas is not None:
        ants = ",".join(str(a) for a in sorted(target.antennas.enabled))
        steps.append(
            ProvisionStep(
                "set_antennas",
                f"Enable antennas {ants}",
                c.build_set_antennas(target.antennas),
            )
        )
    if target.power:
        desc = ", ".join(f"ant{a}={p} dBm" for a, p in sorted(target.power.items()))
        steps.append(
            ProvisionStep(
                "set_power_all",
                f"Power {desc}",
                c.build_set_power_all(sorted(target.power.items())),
            )
        )
    if target.udp_dest is not None:
        d = target.udp_dest
        steps.append(
            ProvisionStep(
                "set_udp_destination",
                f"UDP destination {d.ip}:{d.port}",
                c.build_set_udp_destination(d),
            )
        )
    if target.work_mode is not None:
        steps.append(
            ProvisionStep(
                "set_work_mode",
                f"Work mode {target.work_mode.name}",
                c.build_set_work_mode(target.work_mode),
            )
        )
    if target.network is not None:
        n = target.network
        steps.append(
            ProvisionStep(
                "set_network_config",
                f"Reader address {n.ip}/{n.mask} gw {n.gateway} port {n.port}",
                c.build_set_network_config(n),
            )
        )
    return steps


def _check_guards(
    steps: list[ProvisionStep], *, confirm: bool, allow_unverified: bool
) -> None:
    for step in steps:
        if c.COMMANDS[step.name].guarded:
            check_guard(step.name, confirm=confirm, allow_unverified=allow_unverified)


async def _already_set(
    client: UR4Client, step: ProvisionStep, target: ProvisionTarget
) -> bool:
    """Return whether the reader already has the value ``step`` would set."""
    try:
        match step.name:
            case "set_region":
                return await client.get_region() == target.region
            case "set_antennas":
                return await client.get_antennas() == target.antennas
            case "set_power_all":
                current = {p.antenna: p.power for p in await client.get_power_all()}
                assert target.power is not None
                return all(current.get(a) == p for a, p in target.power.items())
            case "set_udp_destination":
                return await client.get_udp_destination() == target.udp_dest
            case "set_work_mode":
                return await client.get_work_mode() == target.work_mode
            case "set_network_config":
                return await client.get_network_config() == target.network
    except ChainwayError as err:
        _LOGGER.debug("Could not read current value for %s: %s", step.name, err)
    return False


async def provision(
    host: str,
    *,
    port: int = c.DEFAULT_TCP_PORT,
    new_ip: str | None = None,
    mask: str | None = None,
    gateway: str | None = None,
    new_port: int = c.DEFAULT_TCP_PORT,
    udp_dest: UdpDestination | tuple[str, int] | None = None,
    region: FrequencyRegion | None = None,
    antennas: Iterable[int] | None = None,
    power: int | Mapping[int, int] | None = None,
    work_mode: WorkMode | None = WorkMode.AUTO,
    confirm: bool = False,
    allow_unverified: bool = False,
    dry_run: bool = False,
    timeout: float = 3.0,
    client_factory: ClientFactory = UR4Client,
) -> ProvisionResult:
    """Provision a reader for auto mode in a guarded, ordered sequence.

    Returns every step with its outcome. Stops at the first failing step and
    re-raises its error after recording it (the partial result is attached to
    the exception as ``err.provision_result``).

    Raises:
        UnsafeOperationError: a network-changing step without ``confirm=True``
            (not raised in ``dry_run``).
        ValidationError: bad arguments (nothing is sent).

    """
    target = make_target(
        new_ip=new_ip,
        mask=mask,
        gateway=gateway,
        tcp_port=new_port,
        udp_dest=udp_dest,
        region=region,
        antennas=antennas,
        power=power,
        work_mode=work_mode,
    )
    steps = plan(target)
    result = ProvisionResult(dry_run=dry_run)
    if dry_run:
        result.steps = steps
        result.message = "Dry run: nothing was sent."
        return result
    _check_guards(steps, confirm=confirm, allow_unverified=allow_unverified)
    async with client_factory(host, port, timeout) as client:
        for step in steps:
            if step.name == "stop_inventory":
                await client.stop_inventory()
                result.steps.append(_with(step, "sent"))
                continue
            if await _already_set(client, step, target):
                result.steps.append(_with(step, "skipped", "already set"))
                continue
            try:
                await _apply(client, step, target, confirm, allow_unverified)
            except ChainwayError as err:
                result.steps.append(_with(step, "failed", str(err)))
                err.provision_result = result  # type: ignore[attr-defined]  # attach context for callers
                raise
            result.steps.append(_with(step, "sent"))
            if step.name == "set_network_config":
                result.network_changed = True
    if result.network_changed and target.network is not None:
        result.message = (
            f"The reader acknowledged the new address {target.network.ip}:"
            f"{target.network.port}. It may need a power cycle. Reconnect there "
            "and run `aiochainway info` to verify."
        )
    else:
        result.message = "Done."
    return result


def _with(step: ProvisionStep, outcome: str, detail: str = "") -> ProvisionStep:
    return ProvisionStep(step.name, step.description, step.request, outcome, detail)


async def _apply(
    client: UR4Client,
    step: ProvisionStep,
    target: ProvisionTarget,
    confirm: bool,
    allow_unverified: bool,
) -> None:
    match step.name:
        case "set_region":
            assert target.region is not None
            await client.set_region(target.region)
        case "set_antennas":
            assert target.antennas is not None
            await client.set_antennas(target.antennas)
        case "set_power_all":
            assert target.power is not None
            await client.set_power_all(target.power)
        case "set_udp_destination":
            assert target.udp_dest is not None
            await client.set_udp_destination(
                target.udp_dest, confirm=confirm, allow_unverified=allow_unverified
            )
        case "set_work_mode":
            assert target.work_mode is not None
            await client.set_work_mode(target.work_mode)
        case "set_network_config":
            assert target.network is not None
            await client.set_network_config(
                target.network, confirm=confirm, allow_unverified=allow_unverified
            )
        case _:  # pragma: no cover - plan() only produces the names above
            raise ValidationError(f"unknown step {step.name}")
