# t70-linux — WatchGuard Firebox T70 hardware support for Ubuntu/Debian

Debian packages that make a repurposed **WatchGuard Firebox T70** a complete
Linux box:

| Package | What it does |
|---|---|
| `t70-dsa-dkms` | Panel ports 3–7 appear as `lan3`–`lan7` (Marvell 88E6176 via mainline DSA). DKMS rebuilds on every kernel upgrade. |
| `t70-led` | `t70-led` drives the MODE/ATTN/FAILOVER/STATUS LEDs; MODE lights while the OS is up, ATTN blinks while apt updates are pending. |
| `t70-poe` | `t70-poe` reads the PoE controller: per-port class, volts, mA, watts on panel ports 6 and 7. |

Ports 1–3 (`enp1s0`–`enp3s0`) are ordinary Intel I210 NICs and work without
any of this. PoE on ports 6/7 is powered by the hardware on its own.

## Requirements

- Ubuntu Server 26.04 (tested) or another Debian-family release with a recent
  kernel. Nothing here is Ubuntu-specific.
- `mv88e6xxx` and `tag_dsa` in the kernel — on Ubuntu that is
  `linux-modules-extra-<version>-generic`, installed by default on Server.
- `t70-led` uses the sysfs GPIO interface (`CONFIG_GPIO_SYSFS`), present in
  Ubuntu kernels. If a future kernel drops it, the script will move to libgpiod.
- Root for `t70-led <led> on|off` and `t70-poe`; `t70-led show` works unprivileged.

## Install

Download the packages and their checksums from the [latest release](https://github.com/rtober/t70-linux/releases/latest), verify, install:

```bash
V=1.0.0
mkdir -p ~/t70 && cd ~/t70
for f in SHA256SUMS t70-dsa-dkms_${V}-1_all.deb t70-led_${V}-1_all.deb t70-poe_${V}-1_all.deb; do
  wget -q "https://github.com/rtober/t70-linux/releases/download/v${V}/$f"
done
sha256sum -c SHA256SUMS
sudo apt install ./t70-dsa-dkms_*.deb ./t70-led_*.deb ./t70-poe_*.deb
sudo reboot     # or: sudo modprobe t70-dsa
```

Set `V` to the release you want; the checksum check fails loudly if a download is truncated or tampered with.

`apt` pulls in `dkms`, the compiler and `i2c-tools`. After the reboot
`ip link` lists `lan3`–`lan7` (DHCP if a cable is present, never delays boot —
override in `/etc/netplan/`), the MODE LED is on, and `dkms status` reports
`t70-dsa/1.0.0, <kernel>: installed`.

Uninstall: `sudo apt remove t70-dsa-dkms t70-led t70-poe`.

## Usage

```bash
t70-led show                 # state of the four LEDs and pending-update count
sudo t70-led status on       # mode | attn | failover | status | all; on | off
sudo t70-led attn blink      # 1 Hz in the foreground until killed
sudo t70-poe                 # one reading; -w refreshes every 2 s
```

## Hardware notes

[docs/hardware.md](docs/hardware.md) has the GPIO map, the switch/SMI details,
the I210 MDIO register behaviour and the PoE chip — everything needed to
understand or port this. `tools/` holds the read-only probes used to find it.

## Disclaimer

This project is not affiliated with or endorsed by WatchGuard Technologies.
WatchGuard and Firebox are trademarks of WatchGuard Technologies, Inc. The
packages replace the vendor operating system on hardware you own; doing so
voids any support agreement. Everything here was written from scratch and is
licensed under the GNU GPL v2 (see [LICENSE](LICENSE)).
