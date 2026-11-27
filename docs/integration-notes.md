# Using aiochainway from the pantry HACS integration

The pantry integration (Home Assistant, HACS) owns entities, the config flow and
inventory logic. `aiochainway` owns the bytes. The integration lists
`aiochainway==0.3.0` in `manifest.json` `requirements` and never builds frames
itself.

## Division of work

| Concern | Lives in |
|---|---|
| TCP command protocol, UDP frames, tag records, presence tracking | `aiochainway` |
| Config flow, options flow, entities, services, repairs | pantry integration |
| EPC → product mapping, quantities, "added"/"used up" logic | pantry integration |
| Linking to the countertop M5 U107 station and the Kroger Restock barcode flow | pantry integration |

## Config flow

1. Ask for the reader host (default `192.168.99.202`) and TCP port (8888).
2. Test the connection:

   ```python
   from aiochainway import UR4Client, ChainwayError, ReaderTimeout, ConnectionLostError

   try:
       async with UR4Client(host, port, timeout=3) as reader:
           version = await reader.get_version()
           network = await reader.get_network_config()
   except ReaderTimeout, ConnectionLostError:
       errors["base"] = "cannot_connect"
   except ChainwayError:
       errors["base"] = "unknown"
   ```

   Use `network.ip` plus the firmware version as the unique ID until a
   serial-number command is known (no such command is in the SDK). The MAC
   from `discover()` is a better unique ID once discovery is verified.
3. Ask for the UDP port HA listens on (default 8888), region (default USA),
   antennas and power.
4. Provision once, with the HA host as the destination. Do this in the flow so
   the user sees it, and never from a background task:

   ```python
   from aiochainway import provision, FrequencyRegion, WorkMode

   result = await provision(
       host,
       port=port,
       udp_dest=(ha_host_ip, udp_port),
       region=FrequencyRegion.USA,
       antennas=[1, 2, 3, 4],
       power=25,
       work_mode=WorkMode.AUTO,
       confirm=True,  # the user pressed Submit on a step that said so
   )
   ```

   Do not change the reader's IP from Home Assistant. Do that once with the CLI
   (`docs/bringup-macos.md`).
5. Store host, port, UDP port and the provisioning choices in the entry.

## Runtime: one listener per entry

```python
from aiochainway import UR4UdpListener, TagPresence

presence = TagPresence(max_epcs=5000)  # injected clock in tests


def on_tag(tag):  # called in the event loop; keep it fast
    presence.feed(tag)


listener = await UR4UdpListener.start(
    ("0.0.0.0", entry.data["udp_port"]),
    callback=on_tag,
    allowed_sources=[entry.data["host"]],  # UDP is unauthenticated
)
entry.async_on_unload(listener.stop)
```

* A `DataUpdateCoordinator` (e.g. every 5 s) reads `presence.present(ttl=…)` and
  `presence.departed(ttl=…)`, maps EPCs to products, and updates entities.
  Pick the TTL per shelf. Reads in auto mode are bursty, so start around 30 s.
* `listener.stats` (datagrams, tags, bad_frames, dropped_sources) makes good
  diagnostic sensors. A rising `dropped_sources` means something else is sending
  to the port; a rising `bad_frames` points at a firmware or layout mismatch.
* If two entries use the same UDP port, run one listener and dispatch by
  `tag.source` (`udp:<ip>:<port>`). One listener per port.
* Readers in auto mode keep sending; nothing needs to stay connected over TCP.
  Use a short-lived `UR4Client` only for diagnostics or reconfiguration (an
  options-flow "re-provision" button).
* For a "scan now" button in command mode, use
  `async for tag in reader.inventory(duration=3)`. The library always sends stop.

## Door-driven sweeps in command mode (Pantry Hub v0.3.0)

Instead of auto mode, the integration can run a timed sweep over TCP when a
door closes, and treat the result as the cabinet's contents:

```python
from aiochainway import SweepEndReason, UR4Client

client = UR4Client(
    host,
    keepalive_interval_s=300,  # or None, and connect per sweep (see below)
    auto_reconnect=True,
    stop_on_connect=True,  # clears a reader left running by a crashed HA
    on_disconnect=lambda err: ...,  # mark entities unavailable
    on_reconnect=lambda: ...,  # mark available again
)
await client.connect(retries=3)


async def on_door_closed() -> None:
    async with client.inventory(duration=45) as sweep:
        async for tag in sweep:
            ...  # optional live updates
    result = sweep.result
    if result is None or not result.completed:
        # stopped / connection_lost / error: keep the previous contents
        return
    apply_contents(result.unique_epcs, result.per_antenna_counts)
```

* **Only a `completed` sweep (`end_reason == "duration_elapsed"`) is a full
  read of the cabinet.** Anything else is partial; don't mark items as gone
  because of it.
* `result.stop_acknowledged` is false if the reader didn't confirm stop in
  time. The sweep is still valid; log it, and consider `stop_on_connect` or an
  explicit `await client.stop_inventory()` before the next sweep.
* Leaving the `async with` block always sends stop and waits for the ack
  (bounded by `stop_drain`, default = `timeout`). If the door opens again
  mid-sweep, just leave the block: the result says `stopped`.
* Only one sweep at a time per client: a second `inventory()` raises
  `UnsafeOperationError`. Debounce door events.
* `result.unique_epcs` is a set of EPC hex strings; `reports` counts every
  read (repeats included); `per_antenna_counts` tells which shelf saw how
  much.
* **Long idle periods:** whether the UR4 drops idle TCP sessions is not known
  yet (hardware-validation §E.2). Either keep the connection with
  `keepalive_interval_s` (a ping that times out drops the link so
  `auto_reconnect` replaces it), or connect per sweep:
  `async with UR4Client(host, stop_on_connect=True) as c: async with c.inventory(45) as s: ...`.
* `await client.ping()` returns the round-trip in seconds; good for a
  diagnostic sensor.

Test all of this with `FakeUR4` scenarios (see "Testing the integration").

## Errors to handle

| Exception | Meaning | Suggested HA handling |
|---|---|---|
| `ReaderTimeout`, `ConnectionLostError` | reader unreachable or not answering | `ConfigEntryNotReady` at setup; mark diagnostic entities unavailable |
| `ReaderError` | reader answered with a failure status | log, raise a repair issue for provisioning |
| `UnexpectedReplyError`, `FrameError` | bytes don't match the spec | log with `--debug`-style hex; ask the user to open an issue |
| `UnsafeOperationError` | a guarded call without `confirm=True` | programming error |
| `UnsupportedCommandError` | FastID / TagFocus / tag buffer on UR4 | hide the option |
| `ValidationError` (a `ValueError`) | bad user input | form error |

## EPC → product mapping (integration side)

* Key the mapping on the EPC hex string (`TagRead.epc`), upper case.
* Unknown EPC seen → create a pending "unassigned tag" that the user can link
  from the countertop M5 U107 station (scan the product barcode, then hold the
  tag near the reader), or from the Kroger Restock barcode flow (a scanned UPC
  that already maps to a product).
* `TagSighting.antennas` tells which shelf zone (antenna port) saw the tag.
  Map ports to zones in the options flow.

## Testing the integration

Use the fake reader that ships in the library, from the same pinned version
as the client. It speaks the protocol that version implements, and this
repo's CI checks that it answers every supported command with a reply the
client's own parsers accept:

```python
from aiochainway import UR4Client, UR4UdpListener, TagPresence
from aiochainway.testing import FakeUR4, tag_frames, tag_record


async def test_config_flow_connects() -> None:
    async with FakeUR4() as fake:  # 127.0.0.1, free port
        async with UR4Client(fake.host, fake.port) as reader:
            assert await reader.get_version() == "V1.2.3"


async def test_reader_unreachable_shows_error() -> None:
    async with FakeUR4() as fake:
        fake.faults.drop = {"get_version"}  # reply never comes → ReaderTimeout
        ...
```

* `fake.state` holds what the fake reports (version, network, power, region…);
  writes from the client update it.
* `fake.faults` injects delays, dropped or duplicated replies, corrupted XOR,
  byte-by-byte writes, unsolicited tag frames, failure statuses and
  disconnects.
* `fake.tags` / `tag_record(...)` set the tags an inventory returns.
* `tag_frames(...)` builds the bytes of one auto-mode UDP datagram; send it
  to your `UR4UdpListener` with a plain UDP socket.
* `fake.received` / `fake.names()` show what the integration sent, e.g. to
  assert that provisioning set the UDP destination.
* `fake.scenario = [ScriptedTag(...), ...]` scripts sweeps: tags present
  from/until a time on the fake's scenario clock (`fake.elapsed()`), a
  per-tag `read_probability` for edge-of-field flicker (seeded:
  `FakeUR4(seed=…)`), and per-tag `antennas` (only enabled ports report).
* Sweep faults: `faults.disconnect_after_s` (drop mid-sweep),
  `faults.stop_ack_delay` / `faults.no_stop_ack`, and `faults.idle_timeout_s`
  (the fake closes idle connections, to test keepalive and reconnect).

```python
from aiochainway.testing import FakeUR4, ScriptedTag


async def test_item_removed_between_sweeps() -> None:
    async with FakeUR4(seed=1) as fake:
        fake.scenario = [
            ScriptedTag("E2801170000002AA", antennas=(1,)),
            ScriptedTag("E2801170000002BB", present_until=1.0),  # taken out
            ScriptedTag("E2801170000002CC", read_probability=0.4),  # flickers
        ]
        ...
```

For entity logic, feed `TagPresence` directly with `TagRead` objects;
`TagPresence(clock=…)` takes an injected clock, so no sleeps are needed.
