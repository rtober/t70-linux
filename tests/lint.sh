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
hits=$(grep -rn '/usr/local' led poe dsa debian 2>/dev/null | grep -v '^debian/changelog')
if [ -n "$hits" ]; then echo "$hits"; err "/usr/local reference"; fi

# Shell scripts parse.
for s in led/t70-led led/t70-updates-check poe/t70-poe; do
    [ -f "$s" ] && { bash -n "$s" || err "$s: syntax error"; }
done

# debian/ invariants.
[ -f debian/t70-dsa-dkms.dkms ] && {
    grep -q '^PACKAGE_VERSION="#MODULE_VERSION#"$' debian/t70-dsa-dkms.dkms \
        || err "dkms: PACKAGE_VERSION must be the #MODULE_VERSION# placeholder"
}
[ -f debian/changelog ] && {
    head -1 debian/changelog | grep -qE '^t70-linux \([0-9]+\.[0-9]+\.[0-9]+-[0-9]+\) ' \
        || err "changelog: first line must be 't70-linux (X.Y.Z-N) ...'"
}
# Every Exec*= in a unit points at a shipped binary (or modprobe).
for u in dsa/*.service led/*.service; do
    [ -f "$u" ] || continue
    grep -q '^ConditionPathExists=/sys/bus/acpi/devices/INT33FF:01$' "$u" || [ "$u" != led/t70-led.service ] \
        || err "$u: missing ConditionPathExists=/sys/bus/acpi/devices/INT33FF:01"
    while read -r bin; do
        case "$bin" in
            /usr/bin/*) grep -qE "^(led|poe)/$(basename "$bin") usr/bin/?$" debian/*.install \
                            || err "$u: $bin is not installed by any debian/*.install" ;;
            /sbin/modprobe|/sbin/rmmod) ;;
            *) err "$u: unexpected executable $bin" ;;
        esac
    done < <(grep -oE '^Exec[A-Za-z]*=[^ ]+' "$u" | cut -d= -f2)
done

exit $fail
