# Using aiochainway from the pantry HACS integration

The pantry integration (Home Assistant, HACS) owns entities, the config flow and
inventory logic. `aiochainway` owns the bytes. The integration lists
`aiochainway==0.1.0` in `manifest.json` `requirements` and never builds frames
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

Use `tests/fake_reader.py` from this repo as a template for a fake reader, or
feed `TagPresence` directly with `TagRead` objects. `TagPresence(clock=…)`
takes an injected clock, so no sleeps are needed.
