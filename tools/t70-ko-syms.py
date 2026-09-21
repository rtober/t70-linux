#!/usr/bin/env python3
"""t70-ko-syms — list a kernel module's symbols without binutils (ELF64 .symtab).

usage: t70-ko-syms.py FILE.ko [REGEX]
Prints defined functions/objects (with section, address, size) and imports
(UND). REGEX (case-insensitive) filters by name. Spike tooling for reading
the vendor's kernel modules.
"""
import re
import struct
import sys

STT = {0: 'NOTYPE', 1: 'OBJECT', 2: 'FUNC', 3: 'SECTION', 4: 'FILE'}


def main(path, pattern):
    d = open(path, 'rb').read()
    assert d[:4] == b'\x7fELF' and d[4] == 2, 'not ELF64'
    shoff, = struct.unpack_from('<Q', d, 0x28)
    shentsize, shnum, shstrndx = struct.unpack_from('<HHH', d, 0x3A)
    sh = [struct.unpack_from('<IIQQQQIIQQ', d, shoff + i * shentsize) for i in range(shnum)]

    def cstr(off):
        return d[off:d.index(b'\0', off)].decode(errors='replace')

    shstr = sh[shstrndx][4]
    names = [cstr(shstr + s[0]) for s in sh]
    symtab = next(s for s in sh if s[1] == 2)
    strtab = sh[symtab[6]][4]
    rx = re.compile(pattern, re.I) if pattern else None
    rows = []
    for i in range(symtab[5] // 24):
        name, info, other, shndx, value, size = struct.unpack_from('<IBBHQQ', d, symtab[4] + i * 24)
        n = cstr(strtab + name)
        t = STT.get(info & 0xF, str(info & 0xF))
        if not n or t in ('SECTION', 'FILE'):
            continue
        if rx and not rx.search(n):
            continue
        sec = 'UND' if shndx == 0 else names[shndx] if shndx < len(names) else f'#{shndx}'
        rows.append((sec, value, size, t, n))
    for sec, value, size, t, n in sorted(rows):
        print(f'{sec:22s} 0x{value:06x} {size:6d} {t:6s} {n}')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
