#!/bin/bash
# Host checks for the Ubuntu 24.04 live ISO. No root required.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg

fail() { printf 'doctor: %s\n' "$*" >&2; exit 1; }
ok() { printf 'doctor: %s\n' "$*"; }

[ -r /etc/os-release ] || fail "cannot read /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || fail "build host is ${ID:-unknown}, want Ubuntu"
[ "${VERSION_ID:-}" = "24.04" ] || fail "build host is Ubuntu ${VERSION_ID:-unknown}, want 24.04"
[ "$(uname -m)" = "x86_64" ] || fail "build host is $(uname -m), want x86_64"
ok "host Ubuntu ${VERSION_ID} amd64"

for cmd in mmdebstrap mksquashfs xorriso grub-mkrescue gpgv curl mformat; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        if [ "$cmd" = "mformat" ]; then
            fail "missing mformat from the mtools package, required by grub-mkrescue"
        fi
        fail "missing host command: $cmd"
    fi
done
[ -f "$KEYRING" ] || fail "Ubuntu archive keyring missing: $KEYRING"
ok "host tools and Ubuntu keyring present"

avail_kb=$(df -Pk "$REPO_ROOT" | awk 'NR==2 {print $4}')
[ "$avail_kb" -ge $((8 * 1024 * 1024)) ] || fail "less than 8 GiB free on ${REPO_ROOT}"
ok "disk space OK"

grub=$SCRIPT_DIR/grub.cfg
[ -f "$grub" ] || fail "missing $grub"
first=$(awk '
    $1 == "menuentry" { if (seen++) exit }
    seen == 1 { print }
' "$grub")
printf '%s\n' "$first" | grep -q 'boot=casper' || fail "first GRUB entry is not a casper live boot"
printf '%s\n' "$first" | grep -q 'san.mode=diagnostic' && fail "first GRUB entry is the diagnostic mode"
printf '%s\n' "$first" | grep -Eiq 'ubiquity|autoinstall|preseed|install' && fail "first GRUB entry looks like an installer"
grep -q 'menuentry "SAN appliance live"' "$grub" || fail "live menu entry missing"
grep -q 'san.mode=diagnostic' "$grub" || fail "diagnostic menu entry missing"
grep -q 'nopersistent' "$grub" || fail "live entries must pass nopersistent"
if grep -Eiq 'menuentry ".*install' "$grub"; then
    fail "GRUB menu contains an installer entry"
fi
ok "GRUB default is live, diagnostic is separate, no installer"

# shellcheck disable=SC1091
. "$SCRIPT_DIR/apt-pin.sh"
PIN_FILE=$SCRIPT_DIR/apt-pins.sha256
DECISION_FILE=$SCRIPT_DIR/apt-decisions.txt

verify() {
    local url=$1 name=$2 tmp err sha pin_status
    tmp=$(mktemp)
    err=$(mktemp)
    curl -4 -fsSL --max-time 30 --retry 3 --retry-delay 2 "$url" -o "$tmp"
    if ! gpgv --keyring "$KEYRING" "$tmp" >"$err" 2>&1; then
        cat "$err" >&2 || true
        rm -f "$tmp" "$err"
        fail "APT signature check failed for ${name}"
    fi
    sha=$(sha256sum "$tmp" | awk '{print $1}')
    rm -f "$tmp" "$err"
    set +e
    apt_pin_check "$PIN_FILE" "$DECISION_FILE" "$name" "$sha"
    pin_status=$?
    set -e
    if [ "$pin_status" -eq 2 ]; then
        fail "APT signature changed for ${name} without an explicit decision"
    fi
    if [ "$pin_status" -ne 0 ]; then
        fail "APT pin missing for ${name}"
    fi
    ok "APT signature OK ${name} sha256=${sha}"
}

verify "http://archive.ubuntu.com/ubuntu/dists/noble/InRelease" "noble"
verify "http://archive.ubuntu.com/ubuntu/dists/noble-updates/InRelease" "noble-updates"
verify "http://security.ubuntu.com/ubuntu/dists/noble-security/InRelease" "noble-security"

pin_file=$SCRIPT_DIR/rootfs/usr/share/san/kernel-pin
[ -f "$pin_file" ] || fail "kernel pin missing"
pin_value=$(tr -d '[:space:]' < "$pin_file")
printf '%s\n' "$pin_value" | grep -Eq '^6\.8\.0-[0-9]+\.[0-9]+$' \
    || fail "kernel pin leaves 6.8: ${pin_value}"

generic_version() {
    local url=$1 name=$2
    local rel err sha pkg base found
    rel=$(mktemp)
    err=$(mktemp)
    pkg=$(mktemp)
    curl -4 -fsSL --max-time 30 --retry 3 --retry-delay 2 "$url" -o "$rel"
    if ! gpgv --keyring "$KEYRING" "$rel" >"$err" 2>&1; then
        cat "$err" >&2 || true
        rm -f "$rel" "$err" "$pkg"
        fail "APT signature check failed for ${name}"
    fi
    sha=$(SAN_INRELEASE="$rel" PYTHONPATH="$REPO_ROOT/live24/tui" python3 -c '
import os
from pathlib import Path
from san_tui.kernelpin import packages_sha256
print(packages_sha256(Path(os.environ["SAN_INRELEASE"]).read_text(encoding="utf-8")))
') || {
        rm -f "$rel" "$err" "$pkg"
        fail "signed InRelease has no Packages.gz hash for ${name}"
    }
    rm -f "$rel" "$err"
    base=${url%/InRelease}
    # The plain Packages.gz path can lag the signed InRelease. The by-hash
    # object is the file that signature names.
    curl -4 -fsSL --max-time 60 --retry 3 --retry-delay 2 \
        "$base/main/binary-amd64/by-hash/SHA256/$sha" -o "$pkg"
    printf '%s  %s\n' "$sha" "$pkg" | sha256sum -c - >/dev/null \
        || { rm -f "$pkg"; fail "Packages.gz hash mismatch for ${name}"; }
    found=$(gzip -dc "$pkg" | SAN_PIN_STREAM=1 PYTHONPATH="$REPO_ROOT/live24/tui" python3 -c '
import sys
from san_tui.kernelpin import newest_generic
print(newest_generic(sys.stdin.read()))
') || { rm -f "$pkg"; fail "kernel package is not a 6.8 image in ${name}"; }
    rm -f "$pkg"
    printf '%s\n' "$found" | grep -Eq '^6\.8\.0-[0-9]+\.[0-9]+$' \
        || fail "resolved kernel leaves 6.8: ${found}"
    printf '%s\n' "$found"
}

updates=$(generic_version "http://archive.ubuntu.com/ubuntu/dists/noble-updates/InRelease" "noble-updates kernel")
security=$(generic_version "http://security.ubuntu.com/ubuntu/dists/noble-security/InRelease" "noble-security kernel")
resolved=$(PYTHONPATH="$REPO_ROOT/live24/tui" python3 - "$updates" "$security" << 'PY'
import sys
from san_tui.kernelpin import version_key
versions = [item.strip() for item in sys.argv[1:] if item.strip()]
print(max(versions, key=version_key))
PY
)
[ "$resolved" = "$pin_value" ] || fail "kernel pin ${pin_value} is not the newest 6.8 image ${resolved}"
ok "kernel pin ${pin_value} matches the newest Ubuntu 24.04 6.8 image"

ok "ready to build"
