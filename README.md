# aiochainway

Async Python library for **Chainway UR4** fixed UHF RFID readers. TCP command
and UDP auto-mode inventory, no dependencies.

> **Unofficial.** Not affiliated with or endorsed by Chainway. This is a
> clean-room implementation: the protocol was worked out from the public method
> signatures and bytecode of Chainway's Java SDK and is written up in our own
> words in [`docs/protocol.md`](docs/protocol.md). No Chainway code or files
> are included.

* asyncio, Python 3.14+, zero runtime dependencies, fully typed (`py.typed`)
* `UR4Client`: TCP command channel, one request in flight, timeouts everywhere,
  reconnect with backoff, optional keepalive and auto-reconnect, sweeps that
  **always** send stop and report a `SweepResult` (completed or why not)
* `UR4UdpListener`: auto/trigger-mode tag frames over UDP, multiple frames per
  datagram, source filtering, a callback that can't crash the listener
* `TagPresence`: pure presence tracking (first/last seen, RSSI, antennas, TTL)
* `provision()`: guarded, ordered setup for auto mode (IP change last)
* `aiochainway` CLI that runs on macOS and Linux
* `aiochainway.testing.FakeUR4`: a fake reader for your own tests

## Install

```sh
pip install aiochainway
```

## Quick start

### Command mode over TCP

```python
import asyncio
from aiochainway import UR4Client


async def main() -> None:
    async with UR4Client("192.168.99.202") as reader:
        print(await reader.get_version())
        print(await reader.get_power_all())
        async with reader.inventory(duration=5) as sweep:
            async for tag in sweep:
                print(tag.epc, tag.rssi_dbm, tag.antenna)
        print(sweep.result.end_reason, sweep.result.unique_count)


asyncio.run(main())
```

### Auto mode over UDP

```python
from aiochainway import UR4UdpListener, TagPresence

presence = TagPresence()
listener = await UR4UdpListener.start(
    ("0.0.0.0", 8888), callback=presence.feed, allowed_sources=["192.168.99.202"]
)
...
print([s.epc for s in presence.present(ttl=30)])
await listener.stop()
```

or iterate: `async for tag in listener: ...`

### Provision a reader for auto mode

```python
from aiochainway import provision, FrequencyRegion

result = await provision(
    "192.168.99.202",
    udp_dest=("192.168.99.100", 8888),
    region=FrequencyRegion.USA,
    antennas=[1, 2, 3, 4],
    power=25,
    confirm=True,  # required for network-changing steps
)
for step in result.steps:
    print(step.outcome, step.description)
```

`dry_run=True` returns the frames without connecting.

## CLI

Global options: `--host` (or `AIOCHAINWAY_HOST`), `--port`, `--timeout`,
`--json`, `--debug` (hex dump of every frame in both directions).

| Command | What it does |
|---|---|
| `version` | firmware version |
| `info` | version, temperature, mode, network, destination, antennas, power, region, beep |
| `mode [--set command\|auto\|trigger]` | work mode |
| `inventory [--seconds N] [--unique] [--stop-first]` | read tags over TCP; prints a sweep summary |
| `sweep [--seconds 45] [--repeat 10] [--pause 5]` | repeated sweeps with unique-EPC variance |
| `ping [--count N]` | round-trip latency |
| `idle [--minutes 60]` | does the reader drop an idle TCP session? |
| `listen [--port P] [--source IP] [--seconds N]` | receive auto-mode tags over UDP |
| `antennas [--set 1,2]` | enabled antennas |
| `power [--set DBM] [--antenna N]` | antenna power, 1–30 dBm |
| `region [--set usa]` | frequency region |
| `gpi` / `gpo --relay 1` | GPIO |
| `beep [--set on\|off]`, `trigger [--set …]` | beep, trigger-mode parameters |
| `network [--set --ip … --gateway … --confirm]` | reader IP settings |
| `destination [--set --ip … --dest-port … --confirm]` | UDP destination |
| `provision … [--dry-run] [--confirm]` | ordered setup for auto mode |
| `factory-reset --confirm` | restore factory settings |
| `discover [--seconds N]` | listen for reader announcements on UDP 1111 |
| `decode <hex>` / `encode <cmd> [data]` | offline frame tools |

Exit codes: `0` ok, `2` usage, `3` timeout or unreachable, `4` protocol error or
reader-reported failure, `5` unsafe operation refused (missing `--confirm`).

On macOS, the first `listen` may trigger a firewall prompt. Click **Allow**, or
UDP frames are silently dropped.

## Testing code that uses aiochainway

`aiochainway.testing` ships an in-process fake reader with fault injection,
in sync with the protocol of the version you pin:

```python
from aiochainway import UR4Client
from aiochainway.testing import FakeUR4, tag_record

async with FakeUR4() as fake:
    fake.tags = [tag_record("E2801170000002AA", antenna=2)]
    fake.faults.delay = 0.2
    async with UR4Client(fake.host, fake.port) as reader:
        print([t.epc async for t in reader.inventory(duration=1)])
```

## Network setup

See [`docs/bringup-macos.md`](docs/bringup-macos.md). The reader ships at
`192.168.99.202:8888` with a static address (no DHCP).

**Security:** the UR4's TCP port has no authentication and its UDP tag frames
can be spoofed by anyone on the network. Keep the reader on an isolated IoT
VLAN, firewall it from the internet, and pass `allowed_sources` to the listener.

## Spec status

| Area | Status |
|---|---|
| Frame envelope, XOR, length limits | sdk-derived |
| Version, temperature, inventory start/stop, tag records | sdk-derived |
| Network, UDP destination, work mode, beep, GPIO, trigger (`0xA1`) | sdk-derived (trigger time units inferred) |
| Antennas, power, region, RF link, Gen2, inventory mode, antenna dwell set | sdk-derived |
| Antenna dwell get | inferred (one reply byte unknown) |
| Factory reset (`0x74`) | sdk-derived |
| FastID, TagFocus, tag buffer (`0xE0`) | not supported on the UR4 by the SDK |
| Anything | **verified**: none yet; see [`docs/hardware-validation.md`](docs/hardware-validation.md) |

## Reporting bugs

Please include the `--debug` output, which shows every frame in hex:

```sh
aiochainway --debug --host <reader-ip> <command> 2> debug.txt
```

plus the firmware version (`aiochainway version`) and, if you can, a Wireshark
capture filtered with `ip.addr == <reader-ip>`. Remove anything you consider
private (IP addresses, EPCs) before posting.

## Development

```sh
python3.14 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
ruff format --check . && ruff check . && mypy --strict
pytest --cov=aiochainway
mutmut run && python script/mutation_score.py 85
```

## License

MIT. Chainway is a trademark of its owner; used here only to describe compatibility.
