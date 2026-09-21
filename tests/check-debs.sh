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

# Control-file (maintainer script) checks. These assert real dh_installsystemd
# enable/start snippets and dh_dkms add/build/install calls are present, not
# just that a unit name is mentioned somewhere (e.g. in an unmask line).
extract_ctrl() {
    # extract_ctrl <deb> <script name> -> prints path to the extracted script,
    # or nothing (and errs) if the deb or script is missing.
    local deb="$1" script="$2" dir
    dir=$(mktemp -d)
    dpkg-deb -e "$deb" "$dir" || { err "$deb: dpkg-deb -e failed"; rm -rf "$dir"; return 1; }
    if [ ! -f "$dir/$script" ]; then
        err "$deb: control file $script not present"
        rm -rf "$dir"
        return 1
    fi
    echo "$dir/$script"
}

require_fixed() {
    # require_fixed <file> <fixed string> <description>
    local file="$1" needle="$2" desc="$3"
    grep -qF -- "$needle" "$file" && return 0
    err "$desc: missing fixed string: $needle"
    echo "check-debs: --- $file (for diagnosis) ---" >&2
    cat "$file" >&2
    return 1
}

require_line_pair() {
    # require_line_pair <file> <substr1> <substr2> <description>
    # True if some single line contains both fixed substrings (e.g. the
    # deb-systemd-invoke call together with the quoted unit name — the verb
    # in between is a shell variable, not a literal "start").
    local file="$1" a="$2" b="$3" desc="$4"
    if grep -F -- "$a" "$file" | grep -qF -- "$b"; then
        return 0
    fi
    err "$desc: no line contains both '$a' and '$b'"
    echo "check-debs: --- $file (for diagnosis) ---" >&2
    cat "$file" >&2
    return 1
}

check_dsa_postinst() {
    local deb="$1"; [ -n "$deb" ] || return 1
    local f; f=$(extract_ctrl "$deb" postinst) || return 1
    require_fixed "$f" "deb-systemd-helper enable 't70-dsa.service'" "$deb postinst"
    require_line_pair "$f" "deb-systemd-invoke" "'t70-dsa.service'" "$deb postinst"
    # Current dh_dkms (>=3) doesn't call `dkms add`/`build`/`install` directly;
    # it delegates to common.postinst with the module name and version, which
    # does the add/build/install internally. Assert that delegation call.
    require_fixed "$f" "DKMS_NAME=t70-dsa" "$deb postinst"
    require_fixed "$f" "DKMS_VERSION=${upstream}" "$deb postinst"
    require_fixed "$f" '$DKMS_POSTINST $DKMS_NAME $DKMS_VERSION' "$deb postinst"
    rm -rf "$(dirname "$f")"
}

check_dsa_prerm() {
    local deb="$1"; [ -n "$deb" ] || return 1
    local f; f=$(extract_ctrl "$deb" prerm) || return 1
    require_fixed "$f" "dkms" "$deb prerm"
    rm -rf "$(dirname "$f")"
}

check_led_postinst() {
    local deb="$1"; [ -n "$deb" ] || return 1
    local f; f=$(extract_ctrl "$deb" postinst) || return 1
    require_fixed "$f" "deb-systemd-helper enable 't70-led.service'" "$deb postinst"
    require_fixed "$f" "deb-systemd-helper enable 't70-updates-check.timer'" "$deb postinst"
    require_line_pair "$f" "deb-systemd-invoke" "'t70-led.service'" "$deb postinst"
    require_line_pair "$f" "deb-systemd-invoke" "'t70-updates-check.timer'" "$deb postinst"
    rm -rf "$(dirname "$f")"
}

check_dsa_postinst "$dsa_deb"
check_dsa_prerm "$dsa_deb"
check_led_postinst "$led_deb"

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
