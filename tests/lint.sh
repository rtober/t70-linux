#!/bin/bash
# tests/lint.sh — static checks that need no Debian toolchain.
# Run from the repo root: bash tests/lint.sh   (Git Bash on Windows works)
set -uo pipefail
fail=0
err() { echo "lint: $*" >&2; fail=1; }

# Nothing from the vendor OS is ever committed.
if find . -path ./.git -prune -o \( -name '*.ko' -o -name 'S21eth' -o -iname '*fireware*' \
      -o -name 'sled_drv*' -o -name 'wg_dsa*' -o -name 'igb-intel*' \) -print | grep .; then
    err "vendor file present"
fi

# Everything that ships to the T70 is LF.
if grep -rlI $'\r' --exclude-dir=.git . ; then err "CRLF line endings"; fi

# Packages install to /usr/bin; nothing may point at /usr/local.
if grep -rn '/usr/local' led poe dsa debian 2>/dev/null | grep -v '^debian/changelog'; then
    err "/usr/local reference"
fi

# Shell scripts parse.
for s in led/t70-led led/t70-updates-check poe/t70-poe; do
    [ -f "$s" ] && { bash -n "$s" || err "$s: syntax error"; }
done

exit $fail
