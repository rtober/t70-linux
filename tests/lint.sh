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

exit $fail
