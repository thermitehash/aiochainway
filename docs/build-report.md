# aiochainway build report
- Version: 0.4.0
- Date: 2026-11-27
- PR: https://github.com/thermitehash/aiochainway/pull/4 (v0.4.0); earlier: #1 (v0.1.0), #2 (v0.2.0), #3 (v0.3.0)
- Commit: see PR #4 (squash-merged to main; release.yml tags v0.4.0)
- CI: Ruff + mypy --strict, pytest on ubuntu-latest and macos-latest, build, version-bump and mutation jobs (see PR #4 checks)

## Summary
v0.4.0 adds explicit reader-config enforcement:

- `client.read_config()` returns the enabled antennas and every antenna's power.
- `client.ensure_config(antennas, power_dbm, *, save=True)` reads the mask and per-antenna power and writes only what differs: the antenna mask with the save flag, then power for each enabled antenna that differs. It then reads back and verifies. It returns `ConfigResult(changed, before, after, writes, verified, mismatches, dry_run)`. It raises `ValidationError` for an empty set, ports outside 1–4 or power outside 1–30, and nothing is sent in that case. It is never called implicitly, and it refuses to run during a sweep.
- CLI `config --antennas 1,2 --power 27 [--dry-run|--confirm]`, with the same guard rules as provision: exit 5 without one of the flags, exit 4 on a read-back mismatch. `antennas` now shows the mask, per-antenna power and detection status.
- FakeUR4 keeps the mask and power as state. Factory default is {1,2,3,4} at 30 dBm. Settings survive `await fake.power_cycle()` only if written with the save flag. `faults.ignore_writes` makes a write succeed without taking effect.
- hardware-validation §F covers persistence across a power cycle (F.1) and antenna-detection observations (F.2).

**Antenna detection: not available.** I searched the Java SDK (javap over every class), the Java demo and the C# demo for antenna detect, return loss, VSWR, reflected power and connection state. Nothing addresses the UR4:
- `sendAntState` (A4/A8 classes) only blinks an antenna LED.
- `AntennaState` only carries the enable flag.
- The CONNECTED/DISCONNECTED strings describe the TCP link.
- `antennaEnabledState` belongs to the JSON protocol of other readers.
- The UR4 command classes cover only enable, power, work time and interval.

No command was invented. `antennas` reports `detection: unsupported`, and docs/protocol.md §4.5 records the search.

Not done:
- Nothing is verified on hardware.
- PyPI is still off.
- Pantry Hub is not changed from this session.

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
| Set antennas (save) | 0x28 `01 hi lo` → 0x29 | yes | yes | sdk-derived | j.setAntSendData / parseSetAntData |
| Set antennas, no save (v0.4.0) | 0x28 `00 hi lo` → 0x29 | yes | yes | inferred (the SDK only sends `01`; guarded by `allow_unverified`) | j.setAntSendData (flag byte) |
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
| Read config (v0.4.0, client-side) | 0x2A + 0x12 | reuses get_antennas/get_power | reuses parsers | sdk-derived | j.getAntSendData, i.getPowerSendData |
| Ensure config (v0.4.0, client-side) | 0x28 (if mask differs), 0x10 per differing antenna, then re-read | reuses builders | reuses parsers | sdk-derived bytes; persistence of power inferred (unknown until §F.1) | j.setAntSendData, i.setPowerSendData |
| Antenna detection / return loss / VSWR (v0.4.0) | none | no | no | **not in the SDK**; not implemented (see §4.5 of protocol.md) | none found |

## Spec changes vs. the starting spec
1. **Antenna detection (item 2) is not implemented.** The SDK has no such command (see Summary). The spec allowed for this ("if not, document that").
2. **The antenna "save" flag.** `0x28` data is `flag hi lo`. The SDK always sends `01` (save). `save=False` sends `00`, which is **inferred**, so it needs `allow_unverified=True` (CLI `--no-save --allow-unverified`). The spec's `save=False` default path exists but is guarded.
3. **Power has no save flag.** Power writes use the SDK layout `0x10 02 ant hi lo`, where the meaning of `02` is unknown, so `save` doesn't apply to power. Whether power persists is hardware-validation §F.1. FakeUR4 treats `02` as "save" (a documented fake assumption).
4. **Additions beyond the spec:**
   - `dry_run=True` returns the planned writes (with frame hex) without sending.
   - `ConfigResult.mismatches` gives human-readable read-back differences.
   - `ensure_config` raises `UnsafeOperationError` if an inventory is running.
5. Antennas not in the target set are disabled, and their power is left alone. Power is only compared and written for target antennas (tolerance 0.005 dB).
6. The reader reports current values, not saved state. A mask left in RAM by `save=False` looks correct to a later `save=True` call and stays unsaved (documented).
7. Carried over: v0.3.0 sweep rules, and the v0.1.0 javap-derived corrections (reply CMD = request + 1, 0xA2 echo/status, max LEN 2048, trigger = mode 2, 0x74 = factory reset, FastID/TagFocus/0xE0 unsupported, region USA = 0x08, discovery on UDP 1111).

## Open questions
1. Do the antenna mask (flag `01`) and power survive a power cycle? Needs §F.1 (`config --confirm`, power-cycle, `antennas`).
2. Does flag `00` mean "don't save", and does the reader accept it at all? Needs §F.1. Until then `save=False` stays guarded.
3. What does byte `02` in the power command mean (save / temporary / something else)? Needs §F.1 with a capture.
4. Does the reader show anything for an unplugged antenna (an error status, zero reads, a LED)? Needs §F.2. The integration should watch `per_antenna_counts` meanwhile.
5. Does the reader enforce a minimum of one enabled antenna, or limit power per region? The library requires ≥1 and allows 1–30.
6. Still open from v0.3.0:
   - Idle TCP drop (§E.2).
   - Commands during inventory.
   - Sweep repeatability (§E.1).
   - Inventory after a dropped session.
   - Power draw (§E.3).
7. Still open from v0.1.0:
   - Start-inventory reply.
   - Power cycle after an IP change.
   - Factory-reset scope.
   - Unknown reply bytes (0x4D, 0x55).
   - Region 0x34.
   - Trigger units.
   - Discovery interval.
   - Frames per datagram.
   - Serial/MAC read.
   - DHCP.
   - USER window unit.
8. Pantry Hub (thermitehash/ha-apps, pantry-hub/) pins by git tag; it isn't changed from this session. PyPI stays off until hardware validation flips rows to verified. Pin: `git+https://github.com/thermitehash/aiochainway@v0.4.0`.

## Hardware impact
- Config enforcement runs over TCP 8888 only. It doesn't change the network, mode or UDP destination.
- Writes go to non-volatile settings (flag `01`), so don't call `ensure_config` in a loop. Call it at setup or from an "apply" button. It writes nothing when the settings already match.
- No antenna detection: an unplugged or broken cable is only visible as an enabled port with zero reads. Label the cables and check each port in §F.2.
- Power per antenna is 1–30 dBm. More power means more reach and more cross-shelf reads; tune with §E.1.
- Unchanged: static IPv4, no DHCP; 4 antennas; region USA = 0x08; 2 GPIs, 2 GPOs and 1 relay.

## Test results
- Tests: 706/713 (706 passed, 7 skipped by design, 0 failed); 3 consecutive clean full runs before the final mutation-gap tests, then a clean full run and a clean CI-profile run. New: 26 tests in test_config.py (plus golden vectors and parser cases for `set_antennas_no_save`).
- Coverage: protocol/ + presence 100%, overall 98% (client 94%, cli 98%, testing 98%).
- Mutation score (protocol/): 92.3% (1587 killed + 4 timeouts of 1723). Survivors are 132, all equivalent or cosmetic:
  - mostly message and log text;
  - `to_bytes` default byte order;
  - the antenna-mask high byte;
  - `pos += 2`;
  - an unreachable guard;
  - RSSI rounding.
  - New in v0.4.0: `save=None` in `build_set_antennas_no_save` (falsy, so equivalent).
  The 9 "no tests" mutants on the new no-save parser were closed with parser tests.
- Fuzzing: Hypothesis CI profile, 6 properties × 2000 examples (12,000), 0 failures.

## Bugs found and fixed
| # | Found by (test / fuzz / mutation / self-review) | Module | Bug | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | test | protocol/commands.py | the new `set_antennas_no_save` registry entry had no golden vector, so the registry-completeness test failed | golden vectors for both `build_set_antennas_no_save` and `save=False` | test_commands golden vectors `A55A000B28000003200D0A` |
| 2 | self-review | client.py | `ensure_config` during a running sweep would interleave config requests with the tag stream | raises `UnsafeOperationError`; nothing sent | test_config::test_refuses_during_a_sweep |
| 3 | mutation | protocol/commands.py | `parse_set_antennas_no_save` had no tests in the mutation selection (9 "no tests" mutants) | parser added to the status-parser table and the command-name test | test_commands::test_parse_failure_status[parse_set_antennas_no_save], test_protocol_edges::test_ack_parsers_carry_command_name |
| 4 | self-review | client.py / docs | `save=True` after an earlier `save=False` saw a matching mask and wrote nothing, leaving it unsaved; undocumented | documented in the docstring and integration notes (the protocol can't report saved state) | n/a (documented limitation) |

## Risks for hardware bring-up
1. The save flag may not work as inferred. Covered: `save=True` uses the SDK's byte; `00` is guarded; §F.1 checks persistence.
2. Power may not persist across a power cycle. Covered: `ensure_config` at every HA setup is cheap (read-only when the settings match) and re-applies.
3. The reader may round or clamp power (e.g. per region). Covered: read-back mismatch → `verified=False` with `mismatches`, CLI exit 4.
4. A write may be acknowledged but not applied. Covered: always re-read after writes (FakeUR4 `ignore_writes` tests that path).
5. An unplugged antenna is invisible. Covered: documented; watch `per_antenna_counts`.
6. Earlier risks still apply:
   - commands during inventory;
   - idle TCP drop;
   - sweep length;
   - IP change last;
   - strict tag parsing;
   - macOS firewall;
   - factory reset resetting the network.

## First steps when the reader arrives
1. `sudo ifconfig en<N> alias 192.168.99.100 255.255.255.0` and `ping -c 3 192.168.99.202`
2. `aiochainway --debug --host 192.168.99.202 version` and `aiochainway --host 192.168.99.202 ping --count 5`
3. `aiochainway --host 192.168.99.202 --json info > captures/original.json` (save the original settings)
4. `aiochainway --host 192.168.99.202 antennas` (factory default expected: 1,2,3,4 at 30 dBm)
5. Wireshark (`ip.addr == 192.168.99.202`), then hardware-validation §A and §B.
6. `aiochainway --debug --host 192.168.99.202 config --antennas 1,2 --power 27 --dry-run`, then `--confirm`; check exit 0 and `verified`.
7. §F.1: power-cycle the reader, then `aiochainway --host 192.168.99.202 antennas` (persisted?).
8. §F.2: unplug one antenna, run `inventory --seconds 10`, and note statuses, per-antenna counts and LEDs.
9. §E.1–E.3 sweeps, idle and power, then §C writes and §D network changes last. Attach pcaps and JSONL to a follow-up PR.
