# aiochainway build report
- Version: 0.3.0
- Date: 2026-11-27
- PR: https://github.com/thermitehash/aiochainway/pull/3 (v0.3.0); earlier: #1 (v0.1.0), #2 (v0.2.0)
- Commit: 83aba79 (v0.3.0 code; this report is committed on top)
- CI: on 83aba79, Ruff + mypy --strict, build and version-bump pass; pytest ubuntu-latest and macos-latest were still running when this report was committed (see PR #3 checks)

## Summary
v0.3.0 adds door-driven command-mode sweeps for Pantry Hub v0.3.0:

- `async with client.inventory(duration=45) as sweep` always sends stop and waits for the ack when the block is left.
- `sweep.result` (`SweepResult`) tells a completed sweep from an aborted one: `end_reason`, `completed`, counts, unique EPCs, per-antenna counts and stop-ack status.
- `ping()`, opt-in keepalive, opt-in auto-reconnect with backoff, `connect(retries=)` and `stop_on_connect`.
- FakeUR4 scenarios: scripted presence over time, seeded edge-of-field flicker, per-tag antennas, mid-sweep drop, delayed or missing stop ack, idle-connection drop.
- CLI `ping` / `sweep` / `idle`, and hardware-validation §E (sweep repeatability, idle-session survival, power draw).

No protocol changes. Whether the reader drops idle TCP sessions is recorded as inferred/unknown. An independent hostile review of the change found 4 defects (3 fixed, 1 resolved as a documented rule) and 3 suspected issues (fixed).

Not done:
- Nothing is verified on hardware.
- PyPI publish is still off.
- Pantry Hub (thermitehash/ha-apps, pantry-hub/) is deliberately not changed from this session.

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
| Ping (client-side, v0.3.0) | 0x02 → 0x03 (get version) | reuses get_version | reuses parse_get_version | sdk-derived bytes; use as keepalive is inferred | i.getVersionSendData |
| Idle TCP session behaviour (v0.3.0) | n/a | n/a | n/a | inferred (unknown until hardware-validation §E.2) | c.setAutoCheckConnectStatus |

## Spec changes vs. the starting spec
1. No protocol changes in v0.3.0. Every byte layout is the same as v0.2.0 (see the v0.1.0 list below).
2. New client behaviour, not protocol:
   - `ping()` reuses get-version (0x02 → 0x03).
   - Keepalive pings only after idle time, never during a sweep or a pending stop ack. A ping timeout aborts the link.
3. `stop_on_connect` matches the SDK, which sends stop inventory on `init()`. It is opt-in here.
4. **Behaviour changes:**
   - `stop_drain` now defaults to `timeout` (was 0.5 s), because exiting a sweep waits for the ack.
   - An abandoned stream stopped by `__del__` now logs a WARNING.
5. **Sweep end rule** (defined here, not by the SDK; decided by Heather):
   - `duration_elapsed` (`completed`) means the consumer's `async for` ran until the requested duration ran out, with no connection drop or error. At the deadline, stop is sent and every tag already received is still delivered, so a completed sweep delivers exactly the `reports` it counted.
   - Any early exit is `stopped` (`completed=False`), however late the block is left afterwards: `break`, leaving the block without iterating, `aclose()`, cancellation. Consumers derive presence from the tags they iterate, so such a sweep can under-report.
6. Carried over from v0.1.0 (javap vs. starting spec):
   - Reply CMD = request + 1.
   - 0xA2 get replies echo the sub-command, set replies carry a status byte.
   - Max LEN is 2048.
   - Work mode 2 = trigger.
   - 0x74 = factory reset.
   - FastID, TagFocus and 0xE0 are unsupported on the UR4.
   - Region USA = 0x08.
   - Discovery runs on UDP 1111.

## Open questions
1. Does the UR4 drop idle TCP sessions, and after how long? Needs hardware-validation §E.2 (`aiochainway idle --minutes 60`). This decides whether Pantry Hub keeps a connection open with keepalive or connects per sweep.
2. Does the reader answer commands during an inventory? Keepalive avoids pinging mid-sweep either way. Needs a capture (validation §B).
3. How repeatable is a 45 s sweep over a static tag set (unique_epcs variance), and is 45 s long enough? Needs §E.1 (`aiochainway sweep --seconds 45 --repeat 10`).
4. Does the reader keep inventorying after its TCP session drops? `stop_on_connect` covers it, but the behaviour is unknown. Needs an observation.
5. Power draw idle vs reading, and boot time. Needs §E.3.
6. Still open from v0.1.0:
   - Does start inventory (0x82) get a reply?
   - Is a power cycle needed after an IP change?
   - What does factory reset reset?
   - What are the unknown reply bytes (0x4D byte 1, 0x55 byte 1)?
   - Is region 0x34 New Zealand or Taiwan?
   - Are trigger units really 10 ms?
   - How often does the reader announce on UDP 1111?
   - Are there several frames per UDP datagram?
   - Do settings persist across a power cycle?
   - Is there a serial/MAC read command?
   - DHCP: still waiting on Chainway.
   - What is the USER window unit in inventory mode?
7. Pantry Hub lives in thermitehash/ha-apps (pantry-hub/). It will pin v0.3.0 after the tag exists; it isn't changed from this session.
8. PyPI stays off until hardware validation flips rows to verified (decided). Pin via `git+https://github.com/thermitehash/aiochainway@v0.3.0`.

## Hardware impact
- Door-driven sweeps run in **command mode**: no UDP destination or auto mode needed. HA → reader on TCP 8888 is the only flow.
- If the reader drops idle TCP (§E.2), use `keepalive_interval_s` at about half the drop time, or connect per sweep with `stop_on_connect=True`.
- Sweep length and power trade off against repeatability. Use the §E.1 data to choose duration, power (1–30 dBm) and antenna dwell (10–65535 ms).
- Power budget: measure idle vs reading (§E.3) against the PoE class (802.3af 15.4 W / at 30 W).
- Door trigger options:
  - HA's own door sensor triggers a TCP sweep (this release).
  - Or wire the reed switch to the UR4 GPI and use trigger mode (work mode 2, UDP). That path is not used by Pantry Hub v0.3.0.
- Unchanged from earlier releases:
  - static IPv4, no DHCP;
  - 4 antennas;
  - region USA = 0x08;
  - 2 GPIs, 2 GPOs and 1 relay.

## Test results
- Tests: 673/680 (673 passed, 7 skipped by design, 0 failed); 3 consecutive clean full runs. Includes 53 sweep, keepalive and reconnect tests and 5 macOS accept-timing regressions.
- Coverage: protocol/ 100%, presence 100%, overall 98% (client 94%, testing 98%).
- Mutation score (protocol/): 92.3% (1566 killed + 4 timeouts of 1701). protocol/ is unchanged since v0.1.0. Survivors: 97 message text, 15 log text, 6 `to_bytes` default byte order, 3 antenna-mask high byte, 2 `pos += 2`, 1 unreachable guard, 1 RSSI rounding, 6 other equivalent changes. All are equivalent or cosmetic; the v0.1.0 report has the details.
- Fuzzing: Hypothesis CI profile, 6 properties × 2000 examples (12,000), 0 failures.

## Bugs found and fixed
| # | Found by (test / fuzz / mutation / self-review) | Module | Bug | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | test | client.py | `close()` mid-sweep was recorded as `stopped` | `close()` records `connection_lost` | test_sweeps::test_client_close_mid_sweep |
| 2 | self-review | client.py | keepalive pinged mid-sweep (an empty field means no traffic); a ping timeout aborted a valid sweep | never ping while a sweep runs or a stop ack is pending | test_sweeps::test_keepalive_never_pings_during_a_sweep |
| 3 | self-review | client.py | after a drop, repeated close markers (one per reconnect attempt) evicted real tags and inflated `dropped` | marker offered once and never evicts a tag; the lost stream is detached | test_sweeps::test_lost_stream_markers_never_evict_tags_or_inflate_dropped |
| 4 | self-review | client.py | a dead sweep stayed attached after reconnect: it blocked new sweeps, would absorb tags from the new link, and would send stop on the new link | detach on loss; never send stop for a lost sweep | test_sweeps::test_new_sweep_allowed_after_reconnect_and_old_one_not_stopped_there |
| 5 | self-review | client.py | `connect(retries=)`/`reconnect()` in another task could connect after `close()` | `_open()` no longer clears the closing flag; the loop checks it before each attempt and after success | test_sweeps::test_no_connection_after_close_while_reconnecting |
| 6 | self-review | client.py | `stop_inventory()` overwrote a pending stop-ack future, so a waiting sweep reported no ack | ack futures kept in a list; 0x8D resolves all | test_sweeps::test_all_pending_stop_acks_resolve |
| 7 | self-review | client.py | a manual `reconnect()` could race the auto-reconnect loop | manual reconnect cancels the auto task first | test_sweeps::test_manual_reconnect_replaces_auto_reconnect |
| 8 | self-review | client.py | `close()` / `_close_transport()` swallowed the caller's own CancelledError while waiting for tasks | `_cancel_and_wait()` uses `asyncio.wait` | (covered by every close test; race not reproducible) |
| 9 | self-review → decision | client.py/models.py | `break` then leaving the block after the deadline reported `completed`, yet the consumer had iterated only part of the tags; tags queued at the deadline were counted but never delivered | only the iterator reaching the deadline completes a sweep; any early exit is `stopped`; at the deadline, stop is sent and queued tags are still delivered | test_sweeps::test_break_before_deadline_then_late_exit_is_stopped, ::test_exit_after_deadline_without_iterating_is_stopped, ::test_cancel_after_deadline_is_stopped, ::test_break_then_leave_before_deadline_is_stopped, ::test_completed_sweep_delivers_every_counted_report |
| 10 | test (CI, macOS) + watchdog | testing.py (FakeUR4, not library code) | macOS pytest hung until GitHub's 6 h limit. Watchdog dump: the test was stuck in `FakeUR4.stop()` → `server.wait_closed()`, with a late `FakeUR4._handle` reading from a live client. Root cause, shared with #19 and Pantry Hub's macOS FakeUR4 flake: FakeUR4 assumed its accept handler had run by the time the client's `connect()` returned. On macOS (kqueue) it can run later, so `stop()` missed the connection and `wait_closed()` (Python ≥3.12 waits for open connections) never returned | `stop()` refuses handlers that start after it and calls `Server.close_clients()`; new `wait_for_connections(n)` replaces reading `connections` right after a connect; CI: 60 s per-test watchdog printing all asyncio task stacks, faulthandler, verbose output, permanent 20 min job timeout | test_testing::test_stop_with_a_connection_whose_handler_has_not_run, ::test_late_accept_wait_for_connections, ::test_late_accept_drop_connections, ::test_stop_right_after_connect_stress |

## Risks for hardware bring-up
1. The reader may not answer commands during an inventory. Keepalive and sweeps don't depend on it, but stop must still be honoured. Covered: stop is written synchronously and a missing ack is recorded (`stop_acknowledged=false`), not raised.
2. The reader may silently drop idle TCP sessions. Covered: keepalive (opt-in) aborts on a ping timeout, and auto-reconnect plus `stop_on_connect` recover. §E.2 measures the timeout.
3. 45 s may be too short or the power too low for a full cabinet read. Covered: §E.1 measures variance and which EPCs flicker. Pantry Hub should act only on `completed` sweeps.
4. The reader may keep inventorying after an HA crash. Covered: `stop_on_connect=True`.
5. Earlier risks still apply:
   - auto mode vs TCP;
   - IP change last;
   - strict tag parsing;
   - macOS firewall;
   - factory reset resetting the network.

## First steps when the reader arrives
1. `sudo ifconfig en<N> alias 192.168.99.100 255.255.255.0` and `ping -c 3 192.168.99.202`
2. `aiochainway --debug --host 192.168.99.202 version` and `aiochainway --host 192.168.99.202 ping --count 5`
3. `aiochainway --host 192.168.99.202 --json info` (save the original settings)
4. Wireshark (`ip.addr == 192.168.99.202`), then hardware-validation §A and §B.
5. `aiochainway --host 192.168.99.202 inventory --seconds 45 --stop-first` (check the summary line says `duration_elapsed … stop acked`)
6. §E.1: `aiochainway --host 192.168.99.202 --json sweep --seconds 45 --repeat 10 --pause 15 > captures/sweeps-45s.jsonl`
7. §E.3: read PoE watts idle, during a sweep, at 30 dBm, and at boot.
8. §E.2: `aiochainway --host 192.168.99.202 --json idle --minutes 60`
9. Then §C writes and §D network changes last. Attach the pcaps and JSONL to a follow-up PR.
