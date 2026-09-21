# WatchGuard Firebox T70 — hardware notes for Linux

What was found while bringing the T70's switch ports, LEDs and PoE up under
Ubuntu 26.04, and how each piece is driven. Register facts are from the
Intel I210 datasheet, the Marvell 88E6176 and TI TPS23861 datasheets, and
read-only probing with the scripts in `tools/`. Nothing from the vendor OS is
reproduced here. Not affiliated with WatchGuard Technologies.

## Ethernet interfaces

Linux sees **four** controllers, not eight ports:

| Interface | MAC | Notes |
|---|---|---|
| `enp1s0` | `00:90:7f:04:33:58` | WatchGuard OUI, discrete NIC |
| `enp2s0` | `00:90:7f:04:33:59` | WatchGuard OUI, discrete NIC — first SSH session came in here |
| `enp3s0` | `00:90:7f:04:33:5a` | WatchGuard OUI, discrete NIC |
| `enp4s0` | `00:a0:c9:00:00:00` | Intel placeholder MAC = EEPROM-less controller, almost certainly the uplink to an on-board switch chip serving the remaining front-panel ports |

Port label → interface map (all confirmed by cable test, 2026-09-18/20):

| Panel | Interface | Notes |
|---|---|---|
| 0 | `enp1s0` | discrete I210 |
| 1 | `enp2s0` | discrete I210 — the SSH/management port used throughout |
| 2 | `enp3s0` | discrete I210 |
| 3–7 | `lan3`–`lan7` | DSA user ports of the 88E6176 behind `enp4s0`, see below; 6 and 7 are the PoE jacks |

## The 5-port switch behind `enp4s0`

- `enp4s0` is an Intel I210 *backplane* variant (`8086:1537`, SGMII) — its
  wire side goes to a **Marvell 88E6xxx** switch, not a socket. Ports 3–7 show
  **no link LED at all** under Ubuntu: the switch's PHYs stay powered down until
  a driver initialises the chip.
- The vendor OS uses mainline DSA: `dsa_core`, `mdio`, **`mv88e6xxx_drv`**,
  plus a glue module `wg_dsa` (`depends: dsa_core`; strings `find_mdio`,
  `__mdiobus_register`, `wg_dsa_smi_reg`, `marvell_reset_slave`, param
  `split_mode`). Loaded by its network init script. So the switch is managed
  over Marvell SMI through the I210's MDIO pins, and `wg_dsa` only registers
  that MDIO bus + platform data for `mv88e6xxx`.
- Feasibility (2026-09-18, since done — see "DSA: ports 3–7" below): Ubuntu's kernel
  has `mv88e6xxx` with an x86 platform-data probe; what was needed was one small
  out-of-tree module providing an `mii_bus` over the I210's MDIC register plus
  the platform data — that is `dsa/t70-dsa.c`.
- From the vendor's network init script: load order is
  `dsa_core` → `mv88e6xxx_drv` → `wg_dsa` (no parameters) →
  `/sbin/setmacs`; then `ethtool -K sw10 rx off tx off` on the conduit. `wg_dsa`
  names the conduit `sw10` (`sw11` for a second chip) and user ports `eth4+`.
  It also disables ACPI `GPE17` as an "interim fix before BIOS > v1.2" — moot on
  BIOS 1.16, but check `/sys/firmware/acpi/interrupts/gpe17` if DSA ever shows
  an interrupt storm.
- `mv88e6xxx` identifies the chip from its ID register at probe, so the model
  need not be known in advance; the platform data only needs the SMI address
  and CPU port number, both readable over the bus once the MDIO bridge exists.

**Probed 2026-09-20 with `tools/t70-mdio-probe.py`** (BAR0 via sysfs, read-only
MDIC scans): the switch is an **88E6176 rev 1** (switch ID `0x1761`) in
**single-chip addressing** (`sw_addr` 0; ports at SMI `0x10–0x16`, Global1 `0x1B`,
Global2 `0x1C`). **CPU port = 5** (C_Mode 1000BASE-X SerDes, the I210's KX link);
**user ports 0–4** (internal PHYs, ID `0x0141 0x0eb1`, reachable via Global2 SMI-PHY
regs `0x18/0x19`); port 6 is RGMII and unconnected. The SerDes is unpowered until a
driver enables it, which is why `enp4s0` never links.

**I210 external-PHY MDIO mode** (Intel I210 datasheet, MDICNFG register 0x0E04; the
vendor's igb build uses the same mode): with `MDICNFG.Destination = external` (bit 31) the PHY
address driven on the wire is **`MDICNFG.PHY_ADDR` bits 25:21**, and `MDIC.PHYADD`
must be 0. Putting the address in MDIC (the documented way) talks to address 0 and
reads all-ones with the error bit. The vendor's igb registers the resulting
`mii_bus` with direct access for addresses > 0xF and Global2
SMI-PHY access for 0–0xF — the same split mainline `mv88e6xxx` uses for the 6352
family. No GPIO/SDP reset is involved; the switch is alive at power-on.

Tooling: `tools/t70-mdio-probe.py` (`--internal` sanity-scans the I210's own PHY,
`--regs` dumps every responding address) and `tools/t70-ko-syms.py` (lists a
module's ELF symbols without binutils). Both are throwaway diagnostics.

## DSA: ports 3–7 as `lan3`–`lan7`

`dsa/t70-dsa.c` bridges the switch's SMI onto the I210's MDIC block (external-PHY
mode above, under the I210 SW/FW semaphore) and registers a `dsa_mv88e6xxx_pdata`
(compatible `marvell,mv88e6085`, `sw_addr` 0, CPU port 5, user ports 0–4). Mainline
`mv88e6xxx` + `tag_dsa` do everything else; `enp4s0` is the conduit and must stay
unconfigured. The `t70-dsa-dkms` package installs the module source for DKMS, the
modprobe softdeps and port names (`/etc/modprobe.d/t70-dsa.conf`), a systemd unit
bound to `enp4s0`'s device, and a netplan drop-in in `/usr/lib/netplan/` (DHCP,
optional — override in `/etc/netplan/`).

| Panel label | Switch port | Interface |
|---|---|---|
| 3 | 0 | lan3 |
| 4 | 1 | lan4 |
| 5 | 2 | lan5 |
| 6 | 3 | lan6 |
| 7 | 4 | lan7 |

Verified 2026-09-20: dmesg shows `t70-dsa: 88E6176 rev 1 on enp4s0
(0000:04:00.0)`, then `mv88e6085 t70-smi:00: skipping link registration
for CPU port 5`, then `t70-dsa: CPU port 5 SerDes forced up (1000BASE-X,
no autoneg)`. Without a device-tree node DSA skips phylink for CPU port 5
and mainline `mv88e6xxx` only powers a SerDes via phylink, so the module
itself writes the SerDes BMCR (page 1 of SMI-PHY `0xF` via Global2)
to powered/no-autoneg/1000/full and forces port 5's MAC control up after
`mv88e6xxx` probes — `igb` runs the I210 in 1000BASE-KX parallel-detect mode,
which needs autoneg off on the switch side. `lan3` got a DHCP lease, pinged
`1.1.1.1`, and fetched a 255 kB file over HTTP (TCP) with conduit checksum
offload left enabled, so no `ethtool -K` workaround is needed. `dkms status`
reports `t70-dsa/1.0.0, <kernel>: installed`. `rmmod` tears the tree down
cleanly. The unit stops the module with rmmod deliberately — modprobe -r
would also unload the softdeps, including igb, and drop every NIC (found
2026-09-21 while testing apt remove). Reboot: after `sudo reboot` the unit
started at t=20 s once `enp4s0` existed, the module and switch detection
logged at t=21 s, the SerDes fixup at t=23 s, `enp4s0` had carrier and `lan3`
held a single networkd DHCP lease within a minute. `tools/t70-mdio-probe.py`
must not be run while `t70-dsa` is loaded. DKMS rebuilds the module for new
kernels automatically; if a kernel update ever breaks the build, `dkms status`
shows it and the ports are simply absent — the discrete NICs are unaffected.

## PoE on panel ports 6 and 7

Autonomous — nothing to drive from Linux. The vendor OS has no PoE driver for
the T70 either. Verified: a Ubiquiti PoE camera
powered up on panel 6 and on panel 7 under Ubuntu.

The PSE controller is a **TI TPS23861** (4-port) on the I801 SMBus, `i2c-6` address
`0x20` (`0x30` is its broadcast alias), running in auto mode (reg `0x12` = `0xff`).
Chip ports 1 and 2 are panel 6 and 7; 3 and 4 are unwired. `t70-poe` (needs
`i2c-tools`, root) prints supply voltage, chip temperature and per-port
detect/class/volts/mA/watts; `t70-poe -w` refreshes every 2 s. Measured with the camera:
53.8 V, 48 mA, 2.6 W, class 0; supply 53.6 V; die 69 °C. Mainline's `tps23861` hwmon
driver cannot bind on this kernel (it has only device-tree aliases and Ubuntu's x86
kernel has no `CONFIG_OF`); an upstream `i2c_device_id` table would fix that. Installed by the t70-poe package.

## Front-panel LEDs

Panel layout: a 2×2 block — **POWER** (green, hard-wired, on whenever the box
has power), **ATTN** (yellow), **MODE** (green), **STATUS** (red) — then the
8×2 NIC link/speed LEDs (bottom = 10/100, top = 1000), then a lone
**FAILOVER** (yellow) at the far end.

The four controllable LEDs are ordinary Braswell SoC GPIOs on the **north**
pin community (`chv_gpio`, ACPI `INT33FF:01`, the 73-line chip — `gpiochip1`
on this kernel). The vendor's LED driver names the same GPIO_DFX pins, so this
is by design. Mainline Linux already drives these lines, so no extra driver is
needed.

| `gpiochip1` line | Function | Notes |
|---|---|---|
| 1 | MODE, green | |
| **2** | **NIC / PHY reset** | **Never drive it.** Pulling it low resets every Ethernet port; links only came back after a reboot. |
| 3 | ATTN, yellow | |
| 5 | FAILOVER, yellow | |
| 7 | STATUS, red | |
| 0, 4, 6, 8 | inputs | reset button and spares; untested |

All five outputs are **active-low**: `0` = on, `1` = off. The BIOS leaves them
at `1`, which is why every LED except POWER is dark after a plain install.

The t70-led package installs t70-led (t70-led status on, t70-led all off,
t70-led show — show needs no root) and t70-led.service, which lights MODE at
boot and clears all four at shutdown.

Verified 2026-09-18: across a reboot MODE goes dark at shutdown, stays off
through POST/GRUB, and comes back on as the serial console reaches the login
prompt. The same reboot confirmed the serial-console GRUB drop-in survives
`apt upgrade`.

**ATTN blinks while apt updates are pending** (added 2026-09-20). `t70-led attn
blink` toggles line 3 at 1 Hz in the foreground; `t70-updates-check` counts
`apt-get -s dist-upgrade` "Inst" lines, records the number in `/run/t70-updates`
(shown by `t70-led show`) and starts/stops `t70-attn-blink.service`. A timer runs
the check 5 min after boot and every 6 h; `/etc/apt/apt.conf.d/99-t70-led`
re-runs it after every `apt update` and dpkg run so the LED clears the moment an
upgrade finishes. All of it is installed and enabled by the t70-led package.

Verified 2026-09-20 on the box: with no updates pending the check wrote `0`, left
`t70-attn-blink.service` inactive and ATTN dark; a forced `systemctl start
t70-attn-blink` blinked the yellow LED at 1 Hz and `stop` turned it off via
`ExecStopPost`; `apt update` rewrote `/run/t70-updates` through the apt.conf.d
hook.

Two libgpiod pitfalls learned the hard way: `gpioget` without `--as-is`
reconfigures lines as inputs (use `gpioget --as-is`), and `gpioset` releases
the line when it exits but the level persists — so a "blink" loop leaves LEDs
in whatever state the last write set.

The NCT6779D SuperIO that `sensors-detect` finds is **not** involved in the
LEDs; `tools/t70-sio-dump.py` was written while chasing that theory and is
kept only as a read-only register dump tool.
