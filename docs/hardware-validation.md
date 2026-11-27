# Hardware validation checklist

Use this when the UR4 arrives. Each row is one command: run the CLI line, check
the frames in the `--debug` output (`>>` sent, `<<` received) against the
expected bytes, and tick the box. A follow-up PR attaches the captures and
flips the matching rows in `docs/protocol.md` to `verified`.

Before you start, follow `docs/bringup-macos.md` steps 0–3. Set:

```sh
export AIOCHAINWAY_HOST=192.168.99.202
alias ur4='aiochainway --debug'
```

## Capture procedure

1. Start Wireshark on the Mac's Ethernet interface, display filter
   `ip.addr == 192.168.99.202`.
2. Run the rows below in order, one at a time, and write the time next to each.
3. Save as `captures/YYYY-MM-DD-ur4-<firmware>.pcapng`. Also save the terminal
   output: `script -q captures/YYYY-MM-DD-cli.txt`, then run the commands.
4. For UDP rows, capture on the interface the reader sends to.
5. In the follow-up PR, add the pcap and CLI log under `captures/`, change each
   checked row's status in `docs/protocol.md` to `verified`, and note any byte
   that differs in the PR description.

`XX` in expected bytes is the XOR byte, `..` is a value that depends on the
reader. Request frames are exact.

## A. Read-only (safe)

| ✓ | Command | CLI | Request (exact) | Expected reply |
|---|---|---|---|---|
| [ ] | Firmware version | `ur4 version` | `A5 5A 00 08 02 0A 0D 0A` | `A5 5A 00 0B 03 .. .. .. XX 0D 0A` (more data bytes are fine) |
| [ ] | Temperature | `ur4 info` (2nd frame) | `A5 5A 00 08 34 3C 0D 0A` | `A5 5A 00 0B 35 01 .. .. XX 0D 0A` |
| [ ] | Work mode | `ur4 mode` | `A5 5A 00 09 A1 06 AE 0D 0A` | `A5 5A 00 0A A2 06 0? XX 0D 0A` |
| [ ] | Network config | `ur4 network` | `A5 5A 00 09 A1 02 AA 0D 0A` | `A5 5A 00 17 A2 02 C0 A8 63 CA 22 B8 FF FF FF 00 .. .. .. .. XX 0D 0A` (if only 15 bytes, mask/gateway are absent) |
| [ ] | UDP destination | `ur4 destination` | `A5 5A 00 09 A1 04 AC 0D 0A` | `A5 5A 00 0F A2 04 .. .. .. .. .. .. XX 0D 0A` |
| [ ] | Antennas | `ur4 antennas` | `A5 5A 00 08 2A 22 0D 0A` | `A5 5A 00 0A 2B 00 0F XX 0D 0A` (no status byte) |
| [ ] | Power | `ur4 power` | `A5 5A 00 08 12 1A 0D 0A` | `A5 5A .. .. 13 00 01 .. .. .. .. 02 … XX 0D 0A` (status `00` = OK) |
| [ ] | Region | `ur4 region` | `A5 5A 00 08 2E 26 0D 0A` | `A5 5A 00 0A 2F 01 08 XX 0D 0A` (US model → `08`) |
| [ ] | Beep | `ur4 beep` | `A5 5A 00 09 A1 08 A0 0D 0A` | `A5 5A 00 0A A2 08 0? XX 0D 0A` |
| [ ] | GPI | `ur4 gpi` | `A5 5A 00 09 A1 0A A2 0D 0A` | `A5 5A 00 0B A2 0A 0? 0? XX 0D 0A` |
| [ ] | Trigger params | `ur4 trigger` | `A5 5A 00 09 A1 0C A4 0D 0A` | `A5 5A 00 0F A2 0C .. .. .. .. .. .. XX 0D 0A` |
| [ ] | Discovery | `aiochainway discover --seconds 30` | (none sent) | a UDP/1111 datagram `MAC(6) IP(4) PORT(2)`; note how often |

## B. Inventory in command mode

| ✓ | Check | CLI | Expected |
|---|---|---|---|
| [ ] | Start | `ur4 inventory --seconds 3` | `>> A5 5A 00 0A 82 00 00 88 0D 0A`; does the reader send a reply frame to start? (record it) |
| [ ] | Tag frames | (same) | `<< A5 5A .. .. 83 PC PC EPC… RSSI RSSI ANT XX 0D 0A`; ANT is 1–4 |
| [ ] | RSSI plausible | (same) | −30 to −80 dBm at 10–50 cm |
| [ ] | Stop | (same, at the end) | `>> A5 5A 00 08 8C 84 0D 0A`, then `<< A5 5A 00 09 8D 01 XX 0D 0A` |
| [ ] | Stop on Ctrl-C | `ur4 inventory --seconds 30`, press Ctrl-C | stop frame still sent |
| [ ] | Each antenna | tag on ANT1…ANT4 in turn | ANT byte matches the port |
| [ ] | EPC+TID mode | Chainway demo or `python -c` with `set_inventory_mode` | record length = 2 + N + 12 + 3 |

## C. Writes (reversible; record the original values from section A first)

| ✓ | Command | CLI | Request (exact) | Expected reply |
|---|---|---|---|---|
| [ ] | Beep off/on | `ur4 beep --set off` / `--set on` | `A5 5A 00 0A A1 07 00 AC 0D 0A` / `… 01 AD …` | `A5 5A 00 09 A2 01 XX 0D 0A` |
| [ ] | Antennas 1,2 | `ur4 antennas --set 1,2` | `A5 5A 00 0B 28 01 00 03 21 0D 0A` | `A5 5A 00 09 29 01 XX 0D 0A`; read-back shows `1,2` |
| [ ] | Power 20 on ANT1 | `ur4 power --set 20 --antenna 1` | `A5 5A 00 0E 10 02 01 07 D0 07 D0 1D 0D 0A` | `A5 5A 00 09 11 01 XX 0D 0A` |
| [ ] | Power all 25 | `ur4 power --set 25` | `A5 5A 00 1D 10 02 01 09 C4 09 C4 02 09 C4 09 C4 03 09 C4 09 C4 04 09 C4 09 C4 0B 0D 0A` | `… 11 01 …` |
| [ ] | Region USA | `ur4 region --set usa` | `A5 5A 00 0A 2C 01 08 2F 0D 0A` | `A5 5A 00 09 2D 01 XX 0D 0A` |
| [ ] | Work mode auto | `ur4 mode --set auto` | `A5 5A 00 0A A1 05 01 AF 0D 0A` | `A5 5A 00 09 A2 01 XX 0D 0A`; **does the reader still answer TCP commands in auto mode?** |
| [ ] | Work mode back | `ur4 mode --set command` | `A5 5A 00 0A A1 05 00 AE 0D 0A` | as above |
| [ ] | GPO relay | `ur4 gpo --relay 1` then `ur4 gpo` | `A5 5A 00 0C A1 09 00 00 01 A5 0D 0A` | `A2 01`; relay clicks |
| [ ] | Trigger params | `ur4 trigger --set --gpi 1 --work-time 100 --report udp` | `A5 5A 00 10 A1 0B 00 00 64 00 00 01 00 DF 0D 0A` | `A2 01`; read-back matches |

## D. Network-changing (do last; have the recovery steps ready)

| ✓ | Command | CLI | Request (exact for these values) | Expected |
|---|---|---|---|---|
| [ ] | UDP destination | `ur4 destination --set --ip 192.168.99.100 --dest-port 8888 --confirm` | `A5 5A 00 0F A1 03 C0 A8 63 64 22 B8 58 0D 0A` | `A2 01`; read-back matches |
| [ ] | Auto mode UDP | `ur4 mode --set auto`, then `aiochainway listen --source 192.168.99.202 --seconds 20` | — | `83` frames over UDP; how many frames per datagram? which source port? |
| [ ] | Trigger mode | `ur4 mode --set trigger`, short GPI1 | — | reads only while triggered; `getWorkMode` returns `02`? |
| [ ] | Set IP | `ur4 network --set --ip 192.168.99.203 --gateway 192.168.99.1 --confirm` | `A5 5A 00 17 A1 01 C0 A8 63 CB 22 B8 FF FF FF 00 C0 A8 63 01 18 0D 0A` | `A2 01`; does it need a power cycle? reconnect at .203 and read back |
| [ ] | Set IP back | `aiochainway --host 192.168.99.203 network --set --ip 192.168.99.202 --gateway 192.168.99.1 --confirm` | | |
| [ ] | Factory reset | `ur4 factory-reset --confirm` | `A5 5A 00 08 74 7C 0D 0A` | `A5 5A 00 09 75 01 XX 0D 0A`; **record which settings reset** (IP? destination? power?) |

## E. Door-driven sweeps, idle sessions and power (Pantry Hub v0.3.0)

Run with the reader in command mode, settings from §C restored, and a fixed
set of tags that does not move between sweeps (tape them inside the cabinet;
note how many and where).

### E.1 45 s sweep repeatability

| ✓ | Step | CLI | Record |
|---|---|---|---|
| [ ] | 10 sweeps of 45 s, 15 s apart | `aiochainway --json sweep --seconds 45 --repeat 10 --pause 15 > captures/sweeps-45s.jsonl` | the `summary` line: `unique_min`, `unique_max`, `unique_mean`, `unique_variance`, `epcs_seen_any`, `epcs_seen_every` |
| [ ] | Same, door/cabinet closed vs open | as above, two files | difference in `unique_mean` |
| [ ] | Every sweep ended `duration_elapsed` with `stop_acknowledged: true` | (in the JSONL) | any other end reason or a missing ack |
| [ ] | Tags never seen in every sweep | compare `epcs_seen_any` vs `epcs_seen_every` | which EPCs flicker, and their antenna (edge of field) |

Pass mark to decide with the data: `epcs_seen_every == ` the number of tags
placed. If not, raise power or dwell time, or lengthen the sweep, and repeat.

### E.2 Idle TCP session survival

| ✓ | Step | CLI | Record |
|---|---|---|---|
| [ ] | Hold an idle connection for 60 min, then ping | `aiochainway --json idle --minutes 60` | `survived`, or `dropped_after_s` (the reader's idle timeout) |
| [ ] | If it dropped: repeat with a keepalive shorter than that | `python -c` with `UR4Client(..., keepalive_interval_s=<timeout/2>)` | survives? |
| [ ] | Ping after a reader power cycle without reconnecting | power-cycle, then `aiochainway ping` from the old session (or watch `auto_reconnect`) | how long until a dead link is noticed |

Pantry Hub's choice depends on this row: keep one connection open with
`keepalive_interval_s`, or connect per sweep with `stop_on_connect=True`.

### E.3 Power draw

| ✓ | Step | How | Record |
|---|---|---|---|
| [ ] | Idle, command mode, no inventory | PoE switch port readout (W) or an inline PoE/plug meter | W |
| [ ] | During a 45 s sweep at the power used in E.1 | same, read mid-sweep | W (peak and typical) |
| [ ] | During a sweep at 30 dBm on all 4 antennas | same | W |
| [ ] | Boot | same, from power-on to `ping` answering | peak W, seconds to ready |

Compare with the PoE budget of the switch port (802.3af 15.4 W / at 30 W).

## F. Questions to answer with the captures

1. Does start inventory (`82`) get a reply frame?
2. In auto mode, does the reader still accept TCP commands, and does it also push
   tags over TCP?
3. UDP: one frame per datagram or several? Do frames span datagrams? Source port?
4. Do set commands persist across a power cycle (region, antennas, power, mode)?
5. What is byte 1 of the get-antenna-dwell reply (`4D`)?
6. Factory reset: which settings return to defaults?
7. How often does the reader announce itself on UDP 1111?
8. PoE: does the reader boot from your switch (802.3af vs at)? (Power figures: §E.3.)
9. Does the reader drop idle TCP sessions, and after how long? (§E.2.)
10. How repeatable is a 45 s sweep over a static tag set? (§E.1.)
