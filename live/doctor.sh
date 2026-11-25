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

verify() {
    local url=$1 name=$2 tmp err sha
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
    ok "APT signature OK ${name} sha256=${sha}"
}

verify "http://archive.ubuntu.com/ubuntu/dists/noble/InRelease" "noble"
verify "http://archive.ubuntu.com/ubuntu/dists/noble-updates/InRelease" "noble-updates"
verify "http://security.ubuntu.com/ubuntu/dists/noble-security/InRelease" "noble-security"

ok "ready to build"
