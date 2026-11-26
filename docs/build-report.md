# aiochainway build report
- Version: 0.1.0
- Date: 2026-11-26
- PR: https://github.com/thermitehash/aiochainway/pull/1
- Commit: 8752bc6 (code); this report is committed on top of it
- CI: CI_STATUS_PLACEHOLDER

## Summary
This builds the whole `aiochainway` v0.1.0 library in one PR, with no hardware attached:

- the protocol layer (frames, every UR4 command, tag records);
- a TCP client and a UDP listener;
- `TagPresence`, guarded provisioning and device discovery;
- a macOS-friendly CLI, docs, and CI/release workflows.

Every command layout was resolved with `javap` from `ReaderAPI20240822.jar`, the SDK javadoc, and the Java and C# demos. All of it is `sdk-derived` except two rows marked `inferred`, and nothing is `verified`. Testing used an in-repo fake reader with fault injection, golden vectors, Hypothesis fuzzing, mutation testing, and an independent hostile review.

**Not done:**
- No hardware validation.
- The PyPI publish is wired but off (`PUBLISH_PYPI` is unset).
- No Projects-tool copy of this report: no such tool was bound to this session.

## Command status
| Command | CMD/sub | Builder | Reply parser | Status (sdk-derived / inferred) | SDK method |
|---|---|---|---|---|---|
| Firmware version | 0x02 → 0x03 | yes | yes | sdk-derived | i.getVersionSendData / parseVersionData |
| Temperature | 0x34 → 0x35 | yes | yes | sdk-derived | i.getTemperatureSendData / parseTemperatureData |
| Start inventory | 0x82 `00 00` (no reply awaited) | yes | n/a | sdk-derived | i.getStartInventoryTagSendData |
| Stop inventory | 0x8C → 0x8D | yes | yes | sdk-derived | i.getStopInventorySendData / parseStopInventoryData |
| Tag report | 0x83 | n/a | yes | sdk-derived | h.a(byte[],boolean), c.f.b |
| Read tag buffer | 0xE0 → 0xE1 | yes | no (unsupported on UR4) | sdk-derived builder | i.getReadTagSendData |
| Get network config | 0xA1/02 → 0xA2 | yes | yes | sdk-derived | j.getReaderIpSendData / parseGetReaderIpData |
| Set network config | 0xA1/01 → 0xA2 | yes | yes | sdk-derived (guarded) | j.setReaderIpSendData / parseSetReaderIpData |
| Get UDP destination | 0xA1/04 → 0xA2 | yes | yes | sdk-derived | j.getReaderDestIpSendData / parseGetReaderDestIpData |
| Set UDP destination | 0xA1/03 → 0xA2 | yes | yes | sdk-derived (guarded) | j.setReaderDestIpSendData / parseSetReaderDestIpData |
| Get work mode | 0xA1/06 → 0xA2 | yes | yes | sdk-derived | j.getWorkModeSendData / parseGetWorkModeReceiveData |
| Set work mode | 0xA1/05 → 0xA2 | yes | yes | sdk-derived | j.setWorkModeSendData / parseSetWorkModeReceiveData |
| Get beep | 0xA1/08 → 0xA2 | yes | yes | sdk-derived | j.getReaderBeepStatusSendData / parseGetReaderBeepStatusData |
| Set beep | 0xA1/07 → 0xA2 | yes | yes | sdk-derived | j.getBeepSendData / parseBeepData |
| Set GPO | 0xA1/09 → 0xA2 | yes | yes | sdk-derived | j.setIOControlSendData / parseSetIOControlReceiveData |
| Get GPI | 0xA1/0A → 0xA2 | yes | yes | sdk-derived | j.getIOControlSendData / parseGetIOControlReceiveData |
| Set trigger params | 0xA1/0B → 0xA2 | yes | yes | sdk-derived bytes; 10 ms units inferred (C# demo) | j.setTriggerWorkModeParaSendData |
| Get trigger params | 0xA1/0C → 0xA2 | yes | yes | sdk-derived | j.getTriggerWorkModeParaSendData / parseGet… |
| Get antennas | 0x2A → 0x2B | yes | yes | sdk-derived | j.getAntSendData / parseGetAntData |
| Set antennas | 0x28 → 0x29 | yes | yes | sdk-derived | j.setAntSendData / parseSetAntData |
| Get power (all) | 0x12 → 0x13 | yes | yes | sdk-derived | i.getPowerSendData / parseGetPowerAllData |
| Set power (one) | 0x10 → 0x11 | yes | yes | sdk-derived | i.setPowerSendData(int,int) / parseSetPowerData |
| Set power (several) | 0x10 → 0x11 | yes | yes | sdk-derived | i.setPowerAllSendData / parseSetPowerAllDataData |
| Get region | 0x2E → 0x2F | yes | yes | sdk-derived | i.getFrequencyModeSendData / parseGetFrequencyModeData |
| Set region | 0x2C → 0x2D | yes | yes | sdk-derived | i.setFrequencyModeSendData / parseSetFrequencyModeData |
| Get RF link | 0x54 → 0x55 | yes | yes | sdk-derived | i.getRFLinkSendData / parseGetRFLinkData |
| Set RF link | 0x52 → 0x53 | yes | yes | sdk-derived | i.setRFLinkSendData / parseSetRFLinkData |
| Get Gen2 | 0x22 → 0x23 | yes | yes | sdk-derived | i.getGen2SendData / parseGetGen2Data |
| Set Gen2 | 0x20 → 0x21 | yes | yes | sdk-derived | i.setGen2SendData / parseSetGen2Data |
| Get inventory mode (EPC/TID/USER) | 0x72 → 0x73 | yes | yes | sdk-derived | i.getEPCTIDModeSendData / parseGetEPCTIDModeData |
| Set inventory mode | 0x70 → 0x71 | yes | yes | sdk-derived | i.setEPC*ModeSendData / parseSetEPCAndTIDUserModeData |
| Get antenna work time | 0x4C → 0x4D | yes | yes | inferred (reply byte 1 unknown) | j.getAntWorkTimeSendData / parseGetAntWorkTimeReceiveData |
| Set antenna work time | 0x4A → 0x4B | yes | yes | sdk-derived | j.setAntWorkTimeSendData / parseSetAntWorkTimeReceiveData |
| Factory reset | 0x74 → 0x75 | yes | yes | sdk-derived (guarded) | j.getSetSoftResetSendData / parseSetSoftResetData (resetUHFSoft) |
| FastID | 0x5C → 0x5D | yes | n/a (unsupported on UR4) | sdk-derived builder | i.setFastIdSendData |
| TagFocus | 0x60 → 0x61 | yes | n/a (unsupported on UR4) | sdk-derived builder | i.setTagfocusSendData |
| Device discovery | UDP 1111, passive | n/a | yes | sdk-derived | SocketUdpUR4SearchDevice$ThreadUdpReceive.run |

## Spec changes vs. the starting spec
1. **Reply CMD = request CMD + 1** for every command (e.g. 0x02 → 0x03, 0xA1 → 0xA2, 0x8C → 0x8D). The starting spec implied replies reuse the request CMD.
2. **0xA1 replies:** a "get" echoes the sub-command in `data[0]`; a "set" reply is a status byte (`01` = OK) and not an echo.
3. **Status conventions vary by command:**
   - Most replies: `data[0] == 01` is success.
   - Get power (0x13): `00` is success.
   - Version, Gen2 and get-antennas replies have no status byte.
   - There are no error frames anywhere.
4. **Max frame LEN is 2048, not 1024.** That is the SDK's splitter limit. 1024 is only the UDP datagram buffer, and frames may span datagrams.
5. **Work modes:** 0 command, 1 auto, **2 trigger**.
6. **Factory reset exists:** 0x74 (`resetUHFSoft`, documented as "Restore factory settings"). There is no separate soft-reset command.
7. **FastID and TagFocus are unsupported on the UR4:** the SDK's UR4 class returns false without sending anything. **Read tag buffer (0xE0)** is not used on the UR4 network path, so `inventory()` is the only way to read tags.
8. **Tag record:**
   - TID/USER presence is decided by record length alone.
   - The SDK never validates the antenna byte; we reject anything outside 1–4.
   - The SDK zero-pads short EPCs and ignores trailing bytes; we reject both by default (`strict=True`).
9. **Set network config** is `01 ip4 port2 mask4 gw4` (15 bytes). **Set destination** is `03 ip4 port2`.
10. **Antenna mask:** `28 01 hi lo`, where `01` is a save flag and `lo` bit n-1 = ANT n. The get reply `2B hi lo` has no status byte.
11. **Power:**
    - `10 02 ant read×100 write×100`, range **1–30 dBm** (the SDK clamps).
    - The get reply is 5-byte records per antenna.
12. **Region codes:** USA is **0x08**, the full table is in protocol.md, and 0x34 is ambiguous.
13. **On connect, the SDK always sends stop inventory** first. aiochainway does not do this automatically; `stop_inventory()` is exposed instead (see Open questions).
14. **Device discovery exists:** the reader announces `MAC IP PORT` on UDP 1111 and the host just listens. This was added as `discover()` / `aiochainway discover`.
15. **GPO** is `09 out1 out2 relay`, and the trigger params are GPI, work time, interval (10 ms units) and report mode (serial/UDP).

## Open questions
1. Does start inventory (0x82) get a reply? Needs a capture.
2. Does the reader accept TCP commands in auto mode, and does it also push tags over TCP? Needs a capture (hardware-validation §C/§D).
3. Does the reader need a power cycle after an IP change? Needs a capture/observation.
4. What does factory reset (0x74) reset? Network settings? Destination? Needs an observation after a reset.
5. What are the reply bytes we don't understand: 0x4D byte 1, and 0x55 byte 1? Needs a capture or the Chainway doc.
6. Is region 0x34 New Zealand or Taiwan? Needs the Chainway doc.
7. Are the trigger-mode work/interval units really 10 ms? Needs a capture with a known setting, or the Chainway doc.
8. How often does the reader broadcast on UDP 1111? Needs a capture.
9. UDP framing: one frame per datagram or several? What source port? Needs a capture.
10. Do settings persist across a power cycle? Needs an observation.
11. Should the client send stop inventory on connect like the SDK? It isn't the default here, because it could interrupt auto mode. Needs answer #2.
12. Is there a serial-number or MAC read command for an HA unique ID? None exists in the SDK. Needs the Chainway doc. Discovery gives the MAC.
13. Is there DHCP? None in the SDK. Waiting on the Chainway support email from 2026-11-26.
14. What is the USER window unit in inventory mode (words or bytes)? Needs the Chainway doc.

## Hardware impact
- **DHCP: no.** Addressing is static IPv4 only in the SDK. Plan a static IP or DHCP reservation outside the pool on the IoT VLAN.
- **Ports:**
  - TCP 8888 for commands; the reader's port is configurable.
  - The UDP destination port is configurable (8888 by default here).
  - UDP 1111 is used for discovery announcements.
  - Firewall rules should allow reader → HA on the UDP port, and HA → reader on TCP 8888.
- **Antennas:** 4 ports, enable mask per port; the SDK models up to 16.
- **Power range:** 1–30 dBm per antenna. Separate read and write power exist on the wire, but the SDK always sets them equal.
- **Antenna dwell time:** 10–65535 ms per antenna.
- **Region:** USA = 0x08 (902–928 MHz), matching the US variant.
- **GPIO for a door trigger:**
  - Inputs: 2 GPIs (`get_gpi`).
  - Outputs: 2 GPOs plus 1 relay.
  - Trigger mode (work mode 2) starts reads from GPI1 or GPI2 for a set work time (10 ms units) and reports over UDP.
  - A door reed switch on GPI1 + trigger mode can replace continuous auto mode.
  - The input electrical levels and wiring aren't in the SDK; check the UR4 manual.
- **PoE:** nothing in the SDK. Confirm 802.3af/at on first boot (hardware-validation §E.8).

## Test results
- Tests: 570/577 (570 passed, 7 skipped by design, 0 failed); 5 consecutive clean full runs.
- Coverage: protocol/ 100%, presence 100%, overall 98%.
- Mutation score (protocol/): 92.3% (1566 killed + 4 timeouts of 1701). The 131 survivors:
  - **97:** changes to error-message text only (casing, "XX" wrapping, message replaced with `None`).
  - **15:** log-message text.
  - **6:** `to_bytes(2,)` without `"big"`; equivalent, because Python 3.11+ defaults to big-endian.
  - **3:** antenna-mask high byte; equivalent, because ports 1–4 never set it.
  - **2:** `pos += 2` after a rejected header; equivalent, because the byte after `A5` (`5A`) can't start a header.
  - **1:** the unreachable `max_buffer` guard.
  - **1:** `round(…, 2)` vs `round(…, 1)` on RSSI; values are exact tenths.
  - **6 more equivalent changes:**
    - `get=None` vs `get=False`, and `default ""` vs `None` (both falsy pairs);
    - a `min_len` default that every caller overrides;
    - two `get_power` min-length variants that fail the same way;
    - an upper-cased name in `parse_set_gen2`'s error message.
- Fuzzing: Hypothesis CI profile, 6 properties × 2000 examples (12,000) plus the dev profile in normal runs. Failures found: 0 by fuzzing.

## Bugs found and fixed
| # | Found by (test / fuzz / mutation / self-review) | Module | Bug | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | test | client.py | `break` out of `async for tag in inventory()` never sent stop: the client held a strong ref to the stream, so it was never finalised | client keeps a weakref; the stream's `__del__` queues stop synchronously | test_client::test_inventory_break_sends_stop |
| 2 | test | client.py | rejecting a 2nd concurrent inventory sent stop for the 1st | register before marking the stream started | test_client::test_second_concurrent_inventory_refused |
| 3 | test | client.py | `InventoryStream.__del__` raised AttributeError when the constructor rejected the duration | initialise state first | test_client::test_inventory_invalid_duration |
| 4 | test | listener.py | bad tag record (FrameError isn't ValueError) escaped into asyncio's datagram callback; bad_frames not counted | catch FrameError; `datagram_received` is a never-raise boundary | test_listener::test_bad_frames_counted |
| 5 | test | client.py | failing an un-awaited stop-ack future would log "exception never retrieved" | cancel instead of set_exception | test_client::test_close_during_inventory |
| 6 | mutation | tests (harness) | golden vectors built at import were blind to builder mutants (mutmut forks after import) | build requests lazily in tests | test_commands::test_golden_frames |
| 7 | mutation | commands.py / tests | `0xA2` get requests' `reply_sub` matching, temperature 0xF0 boundary, Gen2 bit fields, get-power edge cases, USER upper-casing, decoder LEN high byte and boundaries, and enum-vs-int region/link were not asserted | added assertions; `parse_set_*` made explicit functions | test_protocol_edges (58 tests) |
| 8 | self-review | client.py | late reply to a **cancelled** request was handed to the next same-kind request | stale-reply records on cancel/timeout; the next same-kind request waits for it | test_review_regressions::test_cancelled_request_late_reply_goes_nowhere |
| 9 | self-review | client.py | the timeout quarantine `min(timeout,1s)` was too short; slow replies were misassigned | stale window = 2×timeout, released early when the late reply lands | ::test_slow_late_reply_after_timeout_goes_nowhere, ::test_stale_reply_that_never_comes_only_delays |
| 10 | self-review | commands.py | a `0xA2` set ack could accept a get reply (`reply_sub=None` matched any A2) | canonical `reply_key()`: get-sub frames never match set acks | ::test_set_ack_never_matches_get_reply |
| 11 | self-review | client.py | `close()` during an inventory never sent stop | finish the stream (sync stop write) before closing; wake the consumer | ::test_close_during_inventory_sends_stop |
| 12 | self-review | client.py | `stop_inventory()` raised CancelledError into an uncancelled task when the connection dropped | shield the ack; raise ConnectionLostError | ::test_stop_inventory_connection_lost_is_not_cancellation |
| 13 | self-review | setup.py | int region/work_mode raised AttributeError (even in dry run) | coerce to enums, ValidationError on bad codes | ::test_provision_accepts_int_codes_and_rejects_bad_ones |
| 14 | self-review | cli.py, client.py | bad host (`a..b`) or port 70000 crashed with exit 1 | validated port type; host errors → ValidationError (exit 2) | ::test_cli_bad_host_and_ports_are_usage_errors |
| 15 | self-review | cli.py | missing `--confirm` against an unreachable host exited 3, not 5 | guard checked before connecting | ::test_cli_guard_refuses_before_connecting |
| 16 | self-review | listener.py | ERROR log with traceback for every failing callback (log spam) | first + every 1000th at ERROR, rest at DEBUG | ::test_listener_callback_errors_are_rate_limited |
| 17 | self-review | client.py | `duration=nan` never ended | `not duration > 0` | ::test_nan_duration_rejected |
| 18 | self-review | listener.py | unbounded async callback tasks under a UDP flood | cap 1000 running, count `callbacks_dropped` | ::test_listener_bounds_running_async_callbacks |
| 19 | test (CI, macOS) | tests | timing flake: a fixed 50 ms sleep was shorter than macOS's disconnect detection | `eventually()` polling helper replaces fixed sleeps that wait for async effects | test_client::test_callback_exception_is_logged (+8 tests) |

## Risks for hardware bring-up
1. **Auto mode may stop TCP command handling.** Provisioning sets auto mode before the IP. If the reader ignores TCP in auto mode, the IP step fails. *Covered:* hardware-validation §C tests auto ↔ command on the bench before any IP change, and the bring-up guide provisions without `--new-ip` first.
2. **IP change drops the link or needs a power cycle.** *Covered:* the IP change is always last, the result says "reconnect and verify", and bring-up §4 and §7 cover recovery (alias, discover, factory reset).
3. **Strict tag parsing rejects real records** if the firmware appends extra fields (phase, frequency). *Covered:* `--debug` shows the dropped frames; `strict_tags=False` falls back to the SDK's tolerance; validation §B records the record length.
4. **The start-inventory reply or stop-ack format differs.** A missing stop ack is already tolerated, and unexpected frames are dropped, never misassigned.
5. **macOS firewall silently drops UDP.** *Covered:* the CLI prints a note; bring-up §4.
6. **Factory reset resets the network.** *Covered:* doc warning, `--confirm` guard, run last in validation §D.
7. **The 0x34 region ambiguity and the trigger time units** are irrelevant for a US reader in auto mode, but they matter for door-trigger mode.

## First steps when the reader arrives
1. `sudo ifconfig en<N> alias 192.168.99.100 255.255.255.0` and `ping -c 3 192.168.99.202`
2. `aiochainway --debug --host 192.168.99.202 version`
3. `aiochainway --host 192.168.99.202 --json info` (save the original settings)
4. Start Wireshark with filter `ip.addr == 192.168.99.202` and run every row of hardware-validation §A.
5. `aiochainway --debug --host 192.168.99.202 inventory --seconds 3`, with a tag on each antenna in turn (§B).
6. `aiochainway --host 192.168.99.202 provision --region usa --antennas 1,2,3,4 --power 25 --udp-dest 192.168.99.100:8888 --mode auto --dry-run`
7. The same command with `--confirm` instead of `--dry-run`, then `aiochainway listen --port 8888 --source 192.168.99.202 --seconds 30`
8. `aiochainway --host 192.168.99.202 mode --set command` (does TCP still answer in auto mode?)
9. Hardware-validation §C writes, then §D network changes last. Attach the pcaps to a follow-up PR that flips rows to `verified`.
