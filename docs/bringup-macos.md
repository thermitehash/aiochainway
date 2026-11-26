# Bringing up a UR4 from a Mac

Everything here works on macOS (Apple Silicon or Intel). No Windows needed.

The UR4 ships with a static address, **192.168.99.202**, TCP port **8888**. It
has no DHCP option in Chainway's SDK, so the first contact is always a direct
link on that subnet.

## 0. What you need

* The UR4, its PoE injector or PoE switch port, an Ethernet cable.
* A Mac with Ethernet (built-in or a USB/Thunderbolt adapter).
* Homebrew (<https://brew.sh>).

```sh
brew install python@3.14 wireshark
brew install --cask temurin            # a JDK, only for Chainway's Java demo (fallback)
python3.14 -m venv ~/.venvs/ur4 && source ~/.venvs/ur4/bin/activate
pip install aiochainway                # or: pip install -e . from a checkout
aiochainway --version
```

## 1. Put the Mac on 192.168.99.0/24

Find the Ethernet interface name:

```sh
networksetup -listallhardwareports      # look for "Ethernet" or "USB 10/100/1000 LAN" → Device: en7
```

Either set it in **System Settings → Network → (your Ethernet) → Details →
TCP/IP → Configure IPv4: Manually**, address `192.168.99.100`, mask
`255.255.255.0`, no router, or add a temporary alias that disappears at reboot:

```sh
sudo ifconfig en7 alias 192.168.99.100 255.255.255.0
```

Wi-Fi can stay on for internet; the 192.168.99.0/24 route goes over Ethernet.

## 2. First contact

Power the reader (PoE), wait about 30 s, then:

```sh
ping -c 3 192.168.99.202
aiochainway --host 192.168.99.202 info
```

`info` prints the firmware version, temperature, work mode, network settings,
UDP destination, antennas, power, region and beep state. If a field shows
`error:`, the rest are still valid. Add `--debug` to see every frame in hex:

```sh
aiochainway --debug --host 192.168.99.202 version
```

Try a read in command mode with a tag near antenna 1:

```sh
aiochainway --host 192.168.99.202 inventory --seconds 5 --unique
```

## 3. Watch the wire (optional but recommended)

Open Wireshark, capture on the Ethernet interface with the display filter

```
ip.addr == 192.168.99.202
```

Keep the capture running during sections 4–5 and save it as `.pcapng`. Those
captures are what turn rows in `docs/protocol.md` from `sdk-derived` into
`verified` (see `docs/hardware-validation.md`).

## 4. Provision for auto mode (dry run first)

Pick the reader's permanent address on your IoT VLAN and the Home Assistant
host that will receive UDP tag frames. A dry run prints every frame and sends
nothing:

```sh
aiochainway --host 192.168.99.202 provision \
  --region usa --antennas 1,2,3,4 --power 25 \
  --udp-dest 192.168.99.100:8888 --mode auto \
  --dry-run
```

To test auto mode on the bench before changing the reader's IP, send it for
real with the Mac as the UDP destination, and leave out `--new-ip`:

```sh
aiochainway --host 192.168.99.202 provision \
  --region usa --antennas 1,2,3,4 --power 25 \
  --udp-dest 192.168.99.100:8888 --mode auto --confirm
aiochainway listen --port 8888 --source 192.168.99.202 --seconds 30
```

**macOS firewall:** the first time `listen` runs, macOS may ask whether Python
may accept incoming connections. Click **Allow**. If you clicked Deny, or the
firewall is in stealth mode, UDP tag frames are silently dropped. Check
**System Settings → Network → Firewall → Options**.

When auto mode works, move the reader to its permanent address. The IP change
is always the **last** step, because the reader drops the TCP connection when
its address changes:

```sh
aiochainway --host 192.168.99.202 provision \
  --udp-dest 10.20.30.5:8888 \
  --new-ip 10.20.30.40 --mask 255.255.255.0 --gateway 10.20.30.1 \
  --confirm
```

The reader may need a power cycle. Then, from a machine on that VLAN:

```sh
aiochainway --host 10.20.30.40 info
```

Read every field back. A `set` acknowledgement only means the reader accepted
the command, not that it took effect.

To go back to command mode: `aiochainway --host <ip> mode --set command`.

## 5. Fallback: Chainway's Java demo

If something looks wrong, Chainway's own demo (`UHFUR4Demo`, from the SDK zip)
runs on macOS with the JDK installed above:

```sh
java -jar <path-to-demo-jar>      # or open the Gradle project in IntelliJ
```

Use its Config tab to read and set IP, destination, work mode, power and
region. It is also a good second opinion when a `--debug` trace looks odd. The
library never needs the demo installed.

## 6. Restore the Mac's networking

```sh
sudo ifconfig en7 -alias 192.168.99.100     # if you used the alias
```

or set the Ethernet service back to **Using DHCP** in System Settings.

## 7. Recovery

* **Lost the reader's address:** listen for its announcement on UDP 1111:
  `aiochainway discover --seconds 30` (the same passive search Chainway's demo
  uses; unverified on hardware). The demo's device search does the same.
  Wireshark with filter `udp.port == 1111` also shows it.
* **Wrong subnet:** add an alias for the reader's subnet on the Mac (step 1)
  and talk to it directly.
* **Factory reset:** `aiochainway --host <ip> factory-reset --confirm`
  (SDK `resetUHFSoft`, documented as "Restore factory settings"). Whether this
  also resets the network settings to 192.168.99.202 is **not yet known**; the
  hardware may also have a reset button (see the UR4 manual).
