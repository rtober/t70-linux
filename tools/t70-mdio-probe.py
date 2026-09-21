#!/usr/bin/env python3
"""t70-mdio-probe — spike: find the Marvell switch behind enp4s0 over the I210's MDIO.

Maps BAR0 of the I210 (0000:04:00.0) through sysfs, points MDICNFG at the
external MDIO pins and does read-only MDIC scans of all 32 SMI addresses:

  * PHY ID regs 2/3 (Marvell PHYs answer 0x0141 in reg 2)
  * Marvell 88E6xxx single-chip mode: port registers live at SMI 0x10-0x1A,
    reg 3 = switch ID (product number << 4 | rev), reg 0 = port status
  * --indirect: multi-chip mode probe (writes the SMI command register at
    each candidate address, then reads Global1 reg 3 / port 0 reg 3 via it)

I210 quirk (from WatchGuard's patched igb): in external mode the wire PHY address
is MDICNFG.PHY_ADDR, so MDICNFG is rewritten per transaction; it is restored on exit. igb is
not told about any of this; the I210 SW/FW semaphore is NOT taken, so run
it while enp4s0 is idle. Throwaway diagnostic — the real driver will be a
kernel module.

usage: sudo t70-mdio-probe.py [--pci 0000:04:00.0] [--indirect] [--regs]
"""
import argparse
import ctypes
import mmap
import os
import sys
import time

MDIC = 0x00020        # MDI control: DATA[15:0] REGADD[20:16] PHYADD[25:21] OP[27:26] R[28] I[29] E[30]
MDICNFG = 0x00E04     # MDI config: PHY_ADDR[25:21] COM_MDIO[30] DESTINATION[31] (1 = external MDIO)
CTRL = 0x00000        # SDP0/1 data bits 18/19, direction bits 22/23
CTRL_EXT = 0x00018    # bits 23:22 = link mode; SDP2/3 data bits 6/7, direction bits 10/11
OP_WRITE = 1 << 26
OP_READ = 2 << 26
READY = 1 << 28
ERROR = 1 << 30
MDICNFG_EXT = 1 << 31

# Marvell switch product numbers from mv88e6xxx (switch ID reg 3, bits 15:4)
PRODUCTS = {
    0x04a: '88E6085', 0x095: '88E6095', 0x099: '88E6097', 0x106: '88E6131', 0x115: '88E6320',
    0x121: '88E6123', 0x161: '88E6161', 0x165: '88E6165', 0x171: '88E6171', 0x172: '88E6172',
    0x175: '88E6175', 0x176: '88E6176', 0x190: '88E6190', 0x191: '88E6191', 0x1a7: '88E6185',
    0x220: '88E6220', 0x240: '88E6240', 0x250: '88E6250', 0x290: '88E6290', 0x310: '88E6321',
    0x340: '88E6141', 0x341: '88E6341', 0x352: '88E6352', 0x361: '88E6361', 0x371: '88E6350',
    0x375: '88E6351', 0x390: '88E6390', 0x393: '88E6393',
}


class I210:
    def __init__(self, pci):
        path = f'/sys/bus/pci/devices/{pci}/resource0'
        self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        self.size = os.fstat(self.fd).st_size
        self.mm = mmap.mmap(self.fd, self.size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)

    def reg(self, off):
        # ctypes gives a true 32-bit load/store; slice assignment via memcpy may split or repeat it
        return ctypes.c_uint32.from_buffer(self.mm, off)

    def rd(self, off):
        return self.reg(off).value

    def wr(self, off, val):
        self.reg(off).value = val & 0xFFFFFFFF

    last = None           # raw MDIC completion word (or 'timeout') of the most recent transaction
    cfg = None            # MDICNFG base value when addressing externally (Fireware quirk, see mdio())

    def mdio(self, phy, reg, op, data=0):
        if self.cfg is not None:
            # I210 quirk learned from WatchGuard's igb: with DESTINATION = external, the PHY
            # address driven on the wire is MDICNFG.PHY_ADDR[25:21]; MDIC.PHYADD is sent as 0.
            self.wr(MDICNFG, (self.cfg & 0xFC1FFFFF) | (phy << 21) | MDICNFG_EXT)
            phy = 0
        self.wr(MDIC, (data & 0xFFFF) | (reg << 16) | (phy << 21) | op)
        for _ in range(2000):
            v = self.rd(MDIC)
            if v & READY:
                self.last = v
                if v & ERROR:
                    return None
                return v & 0xFFFF
            time.sleep(0.00005)
        self.last = 'timeout'
        return None

    def read(self, phy, reg):
        return self.mdio(phy, reg, OP_READ)

    def write(self, phy, reg, val):
        return self.mdio(phy, reg, OP_WRITE, val)


def fmt(v):
    return '  ----' if v is None else f'0x{v:04x}'


def direct_scan(hw, dump_regs):
    print('\n== single-chip (direct) scan: SMI addr -> reg0 reg2 reg3 ==')
    hits = []
    for phy, reg in ((0, 0), (0x10, 3)):
        hw.read(phy, reg)
        raw = hw.last if isinstance(hw.last, str) else f'0x{hw.last:08x}'
        print(f'  raw MDIC after read({phy:#x},{reg}): {raw}')
    for a in range(32):
        r0, r2, r3 = hw.read(a, 0), hw.read(a, 2), hw.read(a, 3)
        alive = any(v not in (None, 0xFFFF, 0x0000) for v in (r0, r2, r3))
        note = ''
        if r2 == 0x0141:
            note = 'Marvell PHY'
        if 0x10 <= a <= 0x1A and r3 not in (None, 0xFFFF):
            prod = r3 >> 4
            if prod in PRODUCTS:
                note = f'{PRODUCTS[prod]} rev {r3 & 0xF}  port {a - 0x10}: status {fmt(r0)}'
                hits.append((a, prod, r0))
        if alive or note:
            print(f'  0x{a:02x}: {fmt(r0)} {fmt(r2)} {fmt(r3)}  {note}')
            if dump_regs and alive:
                print('        ' + ' '.join(fmt(hw.read(a, r)) for r in range(32)))
    return hits


def decode_port_status(a, r0):
    # 88E6xxx port status reg 0: bit 11 link, bits 9:8 speed (00=10 01=100 10=1000), bit 10 duplex, bit 12 PHY detect
    link = 'up' if r0 & (1 << 11) else 'down'
    speed = {0: '10', 1: '100', 2: '1000', 3: '?'}[(r0 >> 8) & 3]
    return f'port {a - 0x10}: link {link} {speed}M {"FD" if r0 & (1 << 10) else "HD"}'


def g2_phy_scan(hw):
    # 88E6352-family internal PHYs sit behind Global2 (SMI 0x1C) SMI-PHY command (0x18) / data (0x19):
    # cmd = busy(15) | clause22(12) | read(11:10=10) | phy<<5 | reg  — the same path WatchGuard's igb uses.
    print('\n== PHYs via Global2 SMI-PHY command (0x1c/0x18,0x19) ==')
    for phy in range(8):
        ids = []
        for reg in (2, 3, 0, 1):
            for _ in range(100):
                s = hw.read(0x1C, 0x18)
                if s is not None and not (s & (1 << 15)):
                    break
            hw.write(0x1C, 0x18, 0x9800 | (phy << 5) | reg)
            for _ in range(100):
                s = hw.read(0x1C, 0x18)
                if s is not None and not (s & (1 << 15)):
                    break
            ids.append(hw.read(0x1C, 0x19))
        if any(v not in (None, 0xFFFF, 0x0000) for v in ids):
            print(f'  phy {phy}: id {fmt(ids[0])} {fmt(ids[1])}  bmcr {fmt(ids[2])} bmsr {fmt(ids[3])}')


def g2_phy_wait(hw):
    for _ in range(100):
        s = hw.read(0x1C, 0x18)
        if s is not None and not (s & (1 << 15)):
            return True
    return False


def g2_phy_read(hw, phy, reg):
    g2_phy_wait(hw)
    hw.write(0x1C, 0x18, 0x9800 | (phy << 5) | reg)
    g2_phy_wait(hw)
    return hw.read(0x1C, 0x19)


def g2_phy_write(hw, phy, reg, val):
    g2_phy_wait(hw)
    hw.write(0x1C, 0x19, val)
    hw.write(0x1C, 0x18, 0x9400 | (phy << 5) | reg)
    g2_phy_wait(hw)


def cpu_port_dump(hw):
    # Port 5 = CPU port (SerDes, C_Mode 0x9). Port regs: 0 status, 1 MAC control, 4 port control.
    # The SerDes "PHY" of a 6352-family port 5 is SMI-PHY device 0xF behind Global2.
    print('\n== CPU port 5 ==')
    r = [hw.read(0x15, i) for i in (0, 1, 4)]
    print(f'  port5 status {fmt(r[0])}  mac-ctl {fmt(r[1])}  port-ctl {fmt(r[2])}')
    g2_phy_write(hw, 0xF, 22, 1)            # 6352-family SerDes registers are on page 1 of PHY 0xF
    s = [g2_phy_read(hw, 0xF, i) for i in (0, 1, 4, 5)]
    g2_phy_write(hw, 0xF, 22, 0)
    print(f'  serdes(0xF p1) bmcr {fmt(s[0])}  bmsr {fmt(s[1])}  adv {fmt(s[2])}  lpa {fmt(s[3])}')
    if s[0] is not None:
        print(f'    bmcr: pdown={bool(s[0] & 0x0800)} an={bool(s[0] & 0x1000)} '
              f'speed1000={bool(s[0] & 0x0040)} fd={bool(s[0] & 0x0100)}; '
              f'port5 link={"up" if r[0] and r[0] & 0x0800 else "down"}')


def cpu_port_force(hw):
    # What phylink+mv88e6xxx would do for a fixed-link 1000base-x CPU port:
    # SerDes BMCR = 0x0140 (powered, AN off, 1000/FD); port 5 MAC control = force link up, 1000, full duplex.
    print('\n== forcing CPU port 5 up (SerDes no-AN 1000/FD, MAC forced) ==')
    g2_phy_write(hw, 0xF, 22, 1)
    g2_phy_write(hw, 0xF, 0, 0x0140)
    g2_phy_write(hw, 0xF, 22, 0)
    hw.write(0x15, 1, 0x003E)
    time.sleep(1.0)
    cpu_port_dump(hw)


def indirect_scan(hw):
    print('\n== multi-chip (indirect) scan: SMI command reg at each addr ==')
    for a in range(32):
        # SMI command: bit15 busy, bit12 mode(1=clause22), bits 11:10 op (10=read), 9:5 dev addr, 4:0 reg
        cmd = (1 << 15) | (1 << 12) | (2 << 10) | (0x10 << 5) | 3      # read port0 reg 3
        if hw.write(a, 0, cmd) is None:
            continue
        for _ in range(100):
            s = hw.read(a, 0)
            if s is not None and not (s & (1 << 15)):
                break
        d = hw.read(a, 1)
        if d not in (None, 0xFFFF, 0x0000):
            prod = d >> 4
            print(f'  0x{a:02x}: port0 switch ID {fmt(d)}  {PRODUCTS.get(prod, "unknown product")}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pci', default='0000:04:00.0')
    ap.add_argument('--indirect', action='store_true', help='also probe multi-chip SMI mode (one register write per addr)')
    ap.add_argument('--regs', action='store_true', help='dump regs 0-31 of every responding address')
    ap.add_argument('--cpu', action='store_true', help='dump CPU port 5 + its SerDes (read-only)')
    ap.add_argument('--cpu-force', action='store_true', help='force CPU port 5 up: SerDes AN off 1000/FD, MAC forced link (writes!)')
    ap.add_argument('--internal', action='store_true', help='leave MDICNFG destination = internal PHY (sanity check of the scan itself)')
    ap.add_argument('--com', action='store_true', help='also set MDICNFG Com_MDIO (bit 30)')
    ap.add_argument('--sdp', type=int, choices=(0, 1, 2, 3), help='pulse this I210 SDP pin low for 20 ms as an output before scanning (external PHY/switch reset on Intel reference designs)')
    args = ap.parse_args()

    try:
        with open('/sys/kernel/security/lockdown') as f:
            print('lockdown:', f.read().strip())
    except OSError:
        print('lockdown: (no securityfs entry)')

    hw = I210(args.pci)
    print(f'BAR0 mapped, {hw.size} bytes')
    ctrl_ext = hw.rd(CTRL_EXT)
    print(f'CTRL_EXT = 0x{ctrl_ext:08x}  link mode = {(ctrl_ext >> 22) & 3} (0 copper/internal, 2 SGMII, 3 SerDes/KX)')
    saved = hw.rd(MDICNFG)
    print(f'MDICNFG  = 0x{saved:08x}  ext_mdio={bool(saved & MDICNFG_EXT)} com_mdio={bool(saved & (1 << 30))} phy_addr={(saved >> 21) & 0x1F}')

    try:
        if args.sdp is not None:
            # SDP0/1 live in CTRL (data bits 18/19, dir bits 22/23); SDP2/3 in CTRL_EXT (data 6/7, dir 10/11)
            off, data_bit, dir_bit = (CTRL, 18 + args.sdp, 22 + args.sdp) if args.sdp < 2 else (CTRL_EXT, 6 + args.sdp - 2, 10 + args.sdp - 2)
            orig = hw.rd(off)
            print(f'SDP{args.sdp}: was data={(orig >> data_bit) & 1} dir={"out" if orig & (1 << dir_bit) else "in"}; pulsing low 20 ms')
            hw.wr(off, (orig | (1 << dir_bit)) & ~(1 << data_bit))
            time.sleep(0.02)
            hw.wr(off, orig | (1 << dir_bit) | (1 << data_bit))
            time.sleep(0.2)                       # 88E6xxx needs a few ms after reset before SMI answers
        want = saved
        if not args.internal:
            want |= MDICNFG_EXT
        if args.com:
            want |= 1 << 30
        if want != saved:
            hw.wr(MDICNFG, want)
            print(f'MDICNFG  -> 0x{want:08x} (temporary)')
        if not args.internal:
            hw.cfg = want
        hits = direct_scan(hw, args.regs)
        if not args.internal:
            g2_phy_scan(hw)
        if args.cpu or args.cpu_force:
            cpu_port_dump(hw)
        if args.cpu_force:
            cpu_port_force(hw)
        if hits:
            print('\nswitch ports (single-chip mode):')
            for a, prod, r0 in hits:
                print('  ' + decode_port_status(a, r0))
            g1 = hw.read(0x1B, 0)
            print(f'Global1 reg0 (switch global status) = {fmt(g1)}')
        if not args.internal and (args.indirect or not hits):
            indirect_scan(hw)
    finally:
        hw.wr(MDICNFG, saved)
        print(f'\nMDICNFG restored to 0x{saved:08x}')


if __name__ == '__main__':
    if os.geteuid() != 0:
        sys.exit('run as root')
    main()
