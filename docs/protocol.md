# Chainway UR4 network protocol

This is an **unofficial** description of the protocol the Chainway UR4 speaks on
its network ports. It was reconstructed by disassembling Chainway's Java SDK
(`ReaderAPI20240822.jar`) with `javap -c -p -constants` and by reading the public
method signatures and javadoc that ship with it. No Chainway code, text or binary
is reproduced here. Everything below is written in our own words.

Each row has a **status**:

| Status | Meaning |
|---|---|
| `sdk-derived` | Bytes read directly from the SDK's builder/parser bytecode. |
| `inferred` | Reasoned from the SDK, javadoc or demo, but not fully pinned down by bytecode. |
| `verified` | Confirmed with a packet capture from real hardware. **Nothing is verified yet.** |

"Source" names the SDK method the row came from (`i` = base protocol class
`com.rscja.deviceapi.i`, `j` = UR4 subclass `com.rscja.deviceapi.j`, `h` = tag
record parser, `c.b`/`c.f` = frame splitter and UR4 data handler, `UR4` =
`RFIDWithUHFNetworkUR4`).

## 1. Transport

| Item | Value | Source | Status |
|---|---|---|---|
| Command channel | TCP, reader is the server | `UR4.init(String,int)` → `c.connect` | sdk-derived |
| Default address | `192.168.99.202:8888` (from Chainway documentation and the demo) | demo defaults | inferred |
| Connect timeout | 5000 ms | `c.<init>` constant | sdk-derived |
| Reply timeout | 2000 ms default per command | `c.sendAndReceive([B)` | sdk-derived |
| Stale replies | the SDK discards queued replies older than 5000 ms | `c.b.a(int,int)` | sdk-derived |
| On connect | the SDK sends **stop inventory** first, then marks the reader powered (`aiochainway`: opt-in `stop_on_connect=True`) | `UR4.init(String,int)` | sdk-derived |
| Idle TCP sessions | whether the reader closes an idle command connection, and after how long, is **unknown**; the SDK has a connection-state checker but no keepalive command (`aiochainway`: opt-in `keepalive_interval_s`, using get-version as the ping) | `c.setAutoCheckConnectStatus` | inferred |
| Auto/trigger mode data | UDP, reader → configured destination | `SocketUdp`, `UR4.startReciveUHFTagsByUDP` | sdk-derived |
| UDP receive buffer | 1024-byte datagrams, fed into **one** shared stream splitter (frames may span datagrams; several frames per datagram are fine) | `SocketUdp$ThreadUdpReceive.run`, `c.b.a([B)` | sdk-derived |
| Device discovery | reader broadcasts on UDP **1111**; host only listens | `SocketUdpUR4SearchDevice` | sdk-derived (passive listen); announcement format below |
| Addressing | static IPv4 only; the SDK has no DHCP setting | `j.setReaderIpSendData` | sdk-derived |

## 2. Frame envelope

```
A5 5A | LEN_H LEN_L | CMD | DATA… | XOR | 0D 0A
```

| Field | Rule | Source | Status |
|---|---|---|---|
| Header | `A5 5A` | `i.b(int,byte[])` | sdk-derived |
| LEN | big-endian u16, total frame length = `len(DATA) + 8` | `i.b` | sdk-derived |
| XOR | XOR of every byte from `LEN_H` to the last `DATA` byte | `i.b` | sdk-derived |
| Trailer | `0D 0A` | `i.b` | sdk-derived |
| Accepted LEN | **8 ≤ LEN ≤ 2048**; anything else is rejected by the splitter | `c.b.a([B)`, `i.a(int,int,byte[],StringBuilder)` | sdk-derived |
| Reply CMD | request CMD **+ 1** | `c.sendAndReceive([B,int,int)` | sdk-derived |
| Reply matching | the SDK matches by reply CMD only (sub-command optional) | `c.b.a(int,int)` | sdk-derived |
| Error frames | none: failure is a status byte inside the normal reply, or no reply at all | all parsers | sdk-derived |

Golden vectors: stop inventory `A5 5A 00 08 8C 84 0D 0A` (the SDK hard-codes
this string), get work mode `A5 5A 00 09 A1 06 AE 0D 0A`.

**Resynchronisation.** The SDK's splitter restarts its state machine at the byte
*after* a rejected one, without rewinding. `aiochainway` rescans from the byte
after the rejected `A5`, so a valid frame that starts inside a rejected
candidate is still found.

## 3. Status-byte conventions

| Reply | Convention | Source |
|---|---|---|
| `0xA2` reply to an `0xA1` **get** | `data[0]` echoes the sub-command, fields follow | `j.parseGet*` |
| `0xA2` reply to an `0xA1` **set** | `data[0]` is status, `01` = success (it is **not** an echo) | `j.parseSet*` |
| Most other replies | `data[0]` is status, `01` = success | `i.parse*` |
| Get power `0x13` | `data[0] == 00` means success | `i.parseGetPowerAllData` |
| Version `0x03`, Gen2 `0x23`, antennas `0x2B` | no status byte | `i.parseVersionData`, `i.parseGetGen2Data`, `j.parseGetAntData` |

## 4. Commands

All payloads are hex. `ip4` = 4 address bytes, `port2` = big-endian u16.

### 4.1 Identity and status

| Command | Request CMD / DATA | Reply CMD / DATA | Source | Status |
|---|---|---|---|---|
| Firmware version | `02` / — | `03` / `maj min patch [..]`, rendered `V<maj>.<min>.<patch>` | `i.getVersionSendData`, `i.parseVersionData` | sdk-derived |
| Temperature | `34` / — | `35` / `01 tH tL`: value/100 °C; if `tH ≥ F0` the value is `-((FFFF - t) / 100)` | `i.getTemperatureSendData`, `i.parseTemperatureData` | sdk-derived |

### 4.2 Inventory

| Command | Request | Reply | Source | Status |
|---|---|---|---|---|
| Start continuous inventory | `82` / `00 00` | none awaited; tags arrive as `83` frames | `i.getStartInventoryTagSendData`; `UR4.startInventoryTag` only sends | sdk-derived |
| Stop inventory | `8C` / — | `8D` / `01`. The SDK sends stop and sleeps 50 ms without waiting for this reply | `i.getStopInventorySendData`, `i.parseStopInventoryData`, `UR4.stopInventory` | sdk-derived |
| Tag report | — | `83` / tag record (§5) | `c.f.b`, `i.D` | sdk-derived |
| Read tag buffer | `E0` / — | `E1` / list format. The UR4 network path never calls it (BLE devices do) | `i.getReadTagSendData`, `h.B` | sdk-derived builder; not used on UR4 |

### 4.3 Reader configuration group `A1` (reply `A2`)

| Sub | Purpose | Request DATA | Reply DATA | Source | Status |
|---|---|---|---|---|---|
| `01` | Set IP, port, mask, gateway | `01 ip4 port2 mask4 gw4` (or `01 ip4 port2` when mask/gateway are empty) | `01` = OK | `j.setReaderIpSendData`, `j.parseSetReaderIpData` | sdk-derived |
| `02` | Get IP, port, mask, gateway | `02` | `02 ip4 port2 [mask4 gw4]`; without mask/gateway the SDK assumes `255.255.255.0` and `a.b.c.1` | `j.getReaderIpSendData`, `j.parseGetReaderIpData` | sdk-derived |
| `03` | Set UDP destination | `03 ip4 port2` | `01` = OK | `j.setReaderDestIpSendData`, `j.parseSetReaderDestIpData` | sdk-derived |
| `04` | Get UDP destination | `04` | `04 ip4 port2` | `j.getReaderDestIpSendData`, `j.parseGetReaderDestIpData` | sdk-derived |
| `05` | Set work mode | `05 mode` (`00` command, `01` auto; the javadoc for get lists `02` trigger) | `01` = OK | `j.setWorkModeSendData`, `UR4.setWorkMode` javadoc | sdk-derived |
| `06` | Get work mode | `06` | `06 mode` | `j.getWorkModeSendData`, `j.parseGetWorkModeReceiveData` | sdk-derived |
| `07` | Set beep on read | `07 00\|01` | `01` = OK | `j.getBeepSendData(boolean)`, `j.parseBeepData` | sdk-derived |
| `08` | Get beep state | `08` | `08 state` | `j.getReaderBeepStatusSendData`, `j.parseGetReaderBeepStatusData` | sdk-derived |
| `09` | Set GPO | `09 out1 out2 relay` (each `00`/`01`; the SDK refuses relay values other than 0/1) | `01` = OK | `j.setIOControlSendData`, `UR4.setGPO` javadoc | sdk-derived |
| `09` (UR1A) | Set GPO on the UR1A | `09 00 00 00 o1 o2` | `01` = OK | `j.a(byte,byte)`, `UR4.setGpoOnUR1A` | sdk-derived (not used: UR1A only) |
| `0A` | Get GPI | `0A` | `0A gpi1 gpi2` (`00` low, `01` high) | `j.getIOControlSendData`, `j.parseGetIOControlReceiveData` | sdk-derived |
| `0B` | Set trigger parameters | `0B gpi work2 interval2 report 00`: gpi `00` = input 1, `01` = input 2; work/interval in **10 ms** units; report `00` serial, `01` UDP; trailing `00` pad | `01` = OK | `j.setTriggerWorkModeParaSendData`; units and meanings from the C# demo's `SetWorkModePara` doc comment | bytes sdk-derived; units **inferred** |
| `0C` | Get trigger parameters | `0C` | `0C gpi work2 interval2 report` | `j.getTriggerWorkModeParaSendData`, `j.parseGetTriggerWorkModeParaReceiveData` | sdk-derived |

### 4.4 Antennas, power, region, RF

| Command | Request | Reply | Source | Status |
|---|---|---|---|---|
| Set antennas | `28` / `01 hi lo`: `01` = the save flag the SDK always sends; `lo` bit n-1 = ANT n (1–8), `hi` = ANT 9–16 | `29` / `01` | `j.setAntSendData`, `UR4.setAntenna` | sdk-derived |
| Set antennas, no save | `28` / `00 hi lo`. `j.setAntSendData` takes the flag as a parameter, but the UR4 class only ever passes `01`; that `00` means "don't save" is a guess | `29` / `01` | `j.setAntSendData(char,byte[])` | **inferred** (`aiochainway`: `ensure_config(save=False, allow_unverified=True)`) |
| Get antennas | `2A` / — | `2B` / `hi lo` (no status) | `j.getAntSendData`, `j.parseGetAntData`, `UR4.getAntenna` | sdk-derived |
| Set power (one antenna) | `10` / `02 ant rH rL wH wL`: read and write power in dBm×100, both set to the same value; the SDK clamps to 1–30 dBm. The meaning of the constant `02` is unknown (it may be a save flag), so whether power survives a power cycle is untested (hardware-validation §F) | `11` / `01` | `i.setPowerSendData(int,int)`, `i.parseSetPowerData` | sdk-derived |
| Set power (several) | `10` / `02 (ant rH rL wH wL)*` | `11` / `01` | `i.setPowerAllSendData`, `i.parseSetPowerAllDataData` | sdk-derived |
| Get power | `12` / — | `13` / `00 (ant rH rL wH wL)*`; antenna `00` means 1; power `0` means "same as ANT1"; the SDK fills missing antennas 1–8 with ANT1's value | `i.getPowerSendData`, `i.parseGetPowerAllData` | sdk-derived |
| Set region | `2C` / `01 code` (`01` = save flag) | `2D` / `01` | `i.setFrequencyModeSendData`, `i.parseSetFrequencyModeData` | sdk-derived |
| Get region | `2E` / — | `2F` / `01 code` | `i.getFrequencyModeSendData`, `i.parseGetFrequencyModeData` | sdk-derived |
| Set RF link | `52` / `00 01 link` | `53` / `01` | `i.setRFLinkSendData`, `i.parseSetRFLinkData` | sdk-derived |
| Get RF link | `54` / `00 00` | `55` / `01 ?? link` (byte 1 unknown) | `i.getRFLinkSendData`, `i.parseGetRFLinkData` | sdk-derived |
| Set Gen2 | `20` / 4 bit-packed bytes (below) | `21` / `01` | `i.setGen2SendData`, `i.parseSetGen2Data` | sdk-derived |
| Get Gen2 | `22` / — | `23` / the same 4 bytes, **no status** | `i.getGen2SendData`, `i.parseGetGen2Data` | sdk-derived |
| Set inventory mode | `70` / `01 mode uoff ulen`: mode `00` EPC, `01` EPC+TID, `02` EPC+TID+USER (USER offset and length; the unit is not shown in the SDK) | `71` / `01` | `i.setEPCModeSendData`, `i.setEPCAndTIDModeSendData`, `i.setEPCAndTIDUserModeSendData` | sdk-derived |
| Get inventory mode | `72` / `00 00` | `73` / `01 mode uoff ulen` | `i.getEPCTIDModeSendData`, `i.parseGetEPCTIDModeData` | sdk-derived |
| Set antenna dwell | `4A` / `ant tH tL`, ms, 10–65535 (the UR4 class rejects others) | `4B` / `01` | `j.setAntWorkTimeSendData`, `UR4.setAntennaWorkTime` | sdk-derived |
| Get antenna dwell | `4C` / `ant 00` | `4D` / `01 ?? tH tL`. The SDK checks only for 2 bytes, then reads bytes 2–3 | `j.getAntWorkTimeSendData`, `j.parseGetAntWorkTimeReceiveData` | **inferred** (byte 1 unknown) |
| Restore factory settings | `74` / — | `75` / `01` | `j.getSetSoftResetSendData`; javadoc of `UR4.resetUHFSoft`: "Restore factory settings" | sdk-derived |
| FastID | `5C` / `en 00` | `5D` / `01` | `i.setFastIdSendData`. `UR4.setFastID` returns false **without sending** | sdk-derived builder; **unsupported on UR4** |
| TagFocus | `60` / `en 00` | `61` / `01` | `i.setTagfocusSendData`. `UR4.setTagFocus` returns false **without sending** | sdk-derived builder; **unsupported on UR4** |

Gen2 packing (`i.setGen2SendData` / `i.parseGetGen2Data`):

| Byte | Bits 7…0 |
|---|---|
| 0 | `target(3) action(3) truncate(1) q(1)` |
| 1 | `startQ(4) minQ(4)` |
| 2 | `maxQ(4) DR(1) M(2) TRext(1)` |
| 3 | `Sel(2) Session(2) Target(1) LinkFreq(3)` |

Region codes (javadoc of `setFrequencyMode` and the demo's list):

| Code | Region |
|---|---|
| `01` | China 1 (840–845 MHz) |
| `02` | China 2 (920–925 MHz) |
| `04` | Europe (865–868 MHz) |
| `08` | **USA (902–928 MHz)** |
| `16` | Korea (917–923 MHz) |
| `32` | Japan (952–953 MHz) |
| `33` | South Africa (915–919 MHz) |
| `34` | javadoc: New Zealand; demo: Taiwan (920–928 MHz). **Conflict** |
| `36` | Peru (915–928 MHz), demo only |
| `37` | Russia (860–867.6 MHz), demo only |
| `80` | Morocco, javadoc only |

RF link values (javadoc): `0` DSB_ASK/FM0/40 kHz, `1` PR_ASK/Miller4/250 kHz,
`2` PR_ASK/Miller4/300 kHz, `3` DSB_ASK/FM0/400 kHz.

### 4.5 Antenna detection: not available

There is **no antenna-detect, return-loss, VSWR or antenna-connection-state
command** for the UR4 in Chainway's Java SDK (`ReaderAPI20240822.jar`). How
this was checked (aiochainway v0.4.0):

* `javap -p` on every class in the jar (6,067 signature lines), searched for
  `vswr`, `return loss`, `reflect`, `detect`, `antenna state/status/connect`,
  `impedance`, `standing`. The only hits are:
  * `AntennaState`, which holds an antenna name and an `enable` flag (the
    enable mask of §4.4);
  * a private `sendAntState(String)` in the A4/A8 reader classes
    (`RFIDWithUHFNetworkAx`, `RFIDWithUHFSerialPortAxBase`), which only blinks
    an antenna LED (`setAntBlink`).
* `javap -c -constants` string constants across the jar: `CONNECTED` /
  `DISCONNECTED` are the TCP link state, and `antennaEnabledState` belongs to
  the JSON protocol of other reader families.
* The UR4 command classes (`RFIDWithUHFNetworkUR4`, `j`, `i`) expose antenna
  enable, power, work (dwell) time and interval time only.
* The Windows C# demo (`UR4Demo`) declares no such native function either.

So `aiochainway` has no `get_antenna_status()`, and `ensure_config` can't
refuse an empty port. Whether an empty port is visible any other way is a
hardware-validation item (§F.2). If Chainway's official protocol document
lists a command, it can be added as `sdk-derived` or `verified` then.

## 5. Tag record (payload of `0x83`)

```
PC(2) | EPC(N) | [TID(12) | [USER(M)]] | RSSI(2) | ANT(1)
```

| Field | Rule | Source | Status |
|---|---|---|---|
| N | `(PC[0] >> 3) * 2` bytes | `h.a([B,boolean)` | sdk-derived |
| TID present | when the record has ≥ `2 + N + 12 + 3` bytes | `h.a` | sdk-derived |
| USER present | when longer than that; USER = everything between TID and RSSI | `h.a` | sdk-derived |
| RSSI | u16; `dbm = -((0xFFFF - raw) / 10)`; valid only if the magnitude is strictly between 0 and 200, otherwise "N/A" | `h.a` | sdk-derived |
| ANT | one byte; the SDK does not validate it | `h.a` | sdk-derived |

The SDK tolerates inconsistent lengths: it zero-pads a short EPC and ignores
trailing bytes after an EPC-only record. `aiochainway` rejects both by default
(`strict=True`) and also rejects antennas outside 1–4. `strict=False` accepts
the trailing bytes.

## 6. Device discovery (UDP 1111)

The SDK opens a UDP socket on port 1111 and waits; it sends nothing. Each
datagram of at least 12 bytes is read as `MAC(6) IP(4) PORT(2, big-endian)`.
Source: `SocketUdpUR4SearchDevice$ThreadUdpReceive.run`. Status: sdk-derived.
When or how often the reader broadcasts is unknown.

## 7. Bugs and quirks in the SDK (for reference)

* `getAntenna` tests ANT5–8 with masks like `(b & 0x10) == 0x08`, which are
  always false. This doesn't matter on the 4-port UR4.
* `parseGetAntWorkTimeReceiveData` checks `len ≥ 2` but reads bytes 2 and 3.
* `parseTemperatureData` checks `len > 0` but reads bytes 1 and 2.
* `parseVersionData` uses signed bytes; versions above 127 would print wrongly.
* The UR4 class's `startInventoryTag(int,int,int)` ignores its arguments.
