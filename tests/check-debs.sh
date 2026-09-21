#!/bin/bash
# tests/check-debs.sh — assert the built .debs actually contain what debian/*.install
# and debian/rules say they should (dh_installsystemd only picks up debian/<pkg>.<name>
# units, so a missing rename/append here silently drops a unit from every package).
# Usage: bash tests/check-debs.sh out/*.deb
# Needs: dpkg-deb (present anywhere dpkg-buildpackage runs).
set -uo pipefail
fail=0
err() { echo "check-debs: $*" >&2; fail=1; }

[ "$#" -ge 1 ] || { echo "check-debs: usage: $0 out/*.deb" >&2; exit 1; }

ver=$(dpkg-parsechangelog -S Version)
upstream=${ver%-*}

get_deb() {
    local pkg="$1"; shift
    for d in "$@"; do
        case "$(basename "$d")" in
            "$pkg"_*.deb) echo "$d"; return 0 ;;
        esac
    done
    return 1
}

dsa_deb=$(get_deb t70-dsa-dkms "$@") || err "t70-dsa-dkms .deb not found among: $*"
led_deb=$(get_deb t70-led "$@") || err "t70-led .deb not found among: $*"
poe_deb=$(get_deb t70-poe "$@") || err "t70-poe .deb not found among: $*"

# dpkg-deb -c prints "perms owner/group size date time ./path[ -> target]";
# field 6 is always the bare path. Extract it so matching doesn't depend on
# the width of the preceding columns.
paths_of() {
    dpkg-deb -c "$1" | awk '{print $6}'
}

# Both unit dirs are legal debhelper output; find which one dh_installsystemd used.
unit_dir_for() {
    local paths="$1"
    if echo "$paths" | grep -q '^\./usr/lib/systemd/system/'; then
        echo "usr/lib/systemd/system"
    elif echo "$paths" | grep -q '^\./lib/systemd/system/'; then
        echo "lib/systemd/system"
    else
        echo ""
    fi
}

check_contents() {
    local deb="$1"; shift
    [ -n "$deb" ] || return 1
    local paths
    paths=$(paths_of "$deb") || { err "$deb: dpkg-deb -c failed"; return 1; }
    local udir
    udir=$(unit_dir_for "$paths")
    echo "check-debs: $(basename "$deb"): unit dir = ${udir:-<none found>}"
    for path in "$@"; do
        local p="$path"
        # Substitute the systemd unit dir placeholder if present.
        case "$p" in
            UNITDIR/*)
                [ -n "$udir" ] || { err "$deb: no systemd unit dir (usr/lib or lib) found, cannot check ${p#UNITDIR/}"; continue; }
                p="$udir/${p#UNITDIR/}"
                ;;
        esac
        echo "$paths" | grep -qFx "./$p" \
            || err "$deb: missing ./$p"
    done
}

check_contents "$dsa_deb" \
    "usr/src/t70-dsa-${upstream}/t70-dsa.c" \
    "usr/src/t70-dsa-${upstream}/Makefile" \
    "usr/src/t70-dsa-${upstream}/dkms.conf" \
    "etc/modprobe.d/t70-dsa.conf" \
    "usr/lib/netplan/60-t70-lan.yaml" \
    "UNITDIR/t70-dsa.service"

check_contents "$led_deb" \
    "usr/bin/t70-led" \
    "usr/bin/t70-updates-check" \
    "etc/apt/apt.conf.d/99-t70-led" \
    "UNITDIR/t70-led.service" \
    "UNITDIR/t70-attn-blink.service" \
    "UNITDIR/t70-updates-check.service" \
    "UNITDIR/t70-updates-check.timer"

check_contents "$poe_deb" \
    "usr/bin/t70-poe"

# Control-file (maintainer script) checks.
check_control() {
    local deb="$1" want_file="$2"; shift 2
    [ -n "$deb" ] || return 1
    local dir
    dir=$(mktemp -d)
    dpkg-deb -e "$deb" "$dir" || { err "$deb: dpkg-deb -e failed"; rm -rf "$dir"; return 1; }
    if [ ! -f "$dir/$want_file" ]; then
        err "$deb: control file $want_file not present"
        rm -rf "$dir"
        return 1
    fi
    for needle in "$@"; do
        grep -q -- "$needle" "$dir/$want_file" \
            || err "$deb: $want_file does not mention '$needle'"
    done
    rm -rf "$dir"
}

check_control "$dsa_deb" postinst dkms t70-dsa.service
check_control "$dsa_deb" prerm dkms
check_control "$led_deb" postinst t70-led.service t70-updates-check.timer

# dkms.conf inside the deb must carry the real version, not the placeholder.
check_dkms_conf() {
    local deb="$1"
    [ -n "$deb" ] || return 1
    local content
    content=$(dpkg-deb --fsys-tarfile "$deb" | tar -xO "./usr/src/t70-dsa-${upstream}/dkms.conf" 2>/dev/null)
    if [ -z "$content" ]; then
        err "$deb: could not extract dkms.conf from usr/src/t70-dsa-${upstream}/"
        return 1
    fi
    echo "$content" | grep -q "PACKAGE_VERSION=\"${upstream}\"" \
        || err "$deb: dkms.conf does not contain PACKAGE_VERSION=\"${upstream}\""
}
check_dkms_conf "$dsa_deb"

exit $fail
