#!/usr/bin/env python3
"""Read-only dump of the Nuvoton NCT6779D SuperIO GPIO registers on the T70.

Run on the T70 as root:  sudo python3 t70-sio-dump.py

Enters the SuperIO's configuration mode (the same thing sensors-detect does),
prints the chip ID, then for each GPIO logical device prints the config-space
registers 0xE0-0xFF. Nothing is written except the config-mode enter/exit
sequence and the logical-device select register.

Register meaning (NCT6779D datasheet, per GPIO group N):
  IO   register: bit=1 input, bit=0 output
  DATA register: output level / input reading
  INV  register: bit=1 inverts the pin
  LDN 7 -> GPIO6 (E0/E1/E2), GPIO7 (E4/E5/E6), GPIO8 (F4/F5/F6 approx.)
  LDN 8 -> WDT1, GPIO0 (E0/E1/E2), GPIO1 (F0/F1/F2)
  LDN 9 -> GPIO1..GPIO5 (E0.. / E4.. / E8.. / EC.. / F0..)
  LDN F -> GPIO push-pull / open-drain selection
Groups whose IO register has 0 bits are configured as outputs -> LED candidates.

If this fails with 'Operation not permitted' the kernel is in lockdown mode
(Secure Boot on): check `mokutil --sb-state` and disable Secure Boot in the BIOS.
"""
import os
import sys

CONFIG_PORTS = [(0x2E, 0x2F), (0x4E, 0x4F)]
GPIO_LDNS = [0x7, 0x8, 0x9, 0xF]

try:
    fd = os.open('/dev/port', os.O_RDWR)
except PermissionError:
    sys.exit("need root: sudo python3 t70-sio-dump.py")


def outb(port, val):
    os.pwrite(fd, bytes([val]), port)


def inb(port):
    return os.pread(fd, 1, port)[0]


def read_reg(idx, dat, reg):
    outb(idx, reg)
    return inb(dat)


def write_reg(idx, dat, reg, val):
    outb(idx, reg)
    outb(dat, val)


for idx, dat in CONFIG_PORTS:
    outb(idx, 0x87)          # enter config mode
    outb(idx, 0x87)
    chip = (read_reg(idx, dat, 0x20) << 8) | read_reg(idx, dat, 0x21)
    if chip in (0x0000, 0xFFFF):
        outb(idx, 0xAA)      # leave config mode
        continue
    family = "NCT6779D family" if (chip >> 4) == 0xC56 else "unknown"
    print(f"SuperIO at index 0x{idx:02X}: chip ID 0x{chip:04X} ({family})")
    print("       " + " ".join(f"{r:02X}" for r in range(0xE0, 0x100)))
    for ldn in GPIO_LDNS:
        write_reg(idx, dat, 0x07, ldn)
        active = read_reg(idx, dat, 0x30)
        regs = [read_reg(idx, dat, r) for r in range(0xE0, 0x100)]
        print(f"LDN {ldn:X} (active=0x{active:02X}): " + " ".join(f"{v:02X}" for v in regs))
    outb(idx, 0xAA)          # leave config mode
    break
else:
    print("no SuperIO answered at 0x2E or 0x4E")
