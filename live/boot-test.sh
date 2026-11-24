#!/bin/bash
# Boot the live ISO from a virtual CD. Disposable disk, isolated NIC.
# SeaBIOS and OVMF (Secure Boot not enabled). Exits non-zero on failure.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
ISO=${SAN_ISO:-$REPO_ROOT/dist/san-appliance-live-24.04-amd64.iso}
CODE=/usr/share/OVMF/OVMF_CODE_4M.fd
VARS_SRC=/usr/share/OVMF/OVMF_VARS_4M.fd

[ -f "$ISO" ] || { printf 'boot-test: missing %s\n' "$ISO" >&2; exit 1; }
[ -f "$CODE" ] || { printf 'boot-test: missing %s\n' "$CODE" >&2; exit 1; }
command -v qemu-system-x86_64 >/dev/null || { printf 'boot-test: qemu-system-x86_64 missing\n' >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
dd if=/dev/urandom of="$work/disk.raw" bs=1M count=32 status=none
sha_before=$(sha256sum "$work/disk.raw" | awk '{print $1}')

run_qemu() {
    local name=$1
    shift
    local log=$work/$name.log
    qemu-system-x86_64 \
        -display none -no-reboot \
        -cpu max -m 2048 -smp 2 \
        -cdrom "$ISO" \
        -drive file="$work/disk.raw",format=raw,if=virtio \
        -netdev user,id=n0,restrict=on \
        -device virtio-net-pci,netdev=n0 \
        -serial file:"$log" \
        "$@" >/dev/null 2>&1 &
    local pid=$!
    local i marker=0
    for i in $(seq 1 90); do
        if grep -q 'san-live: mode=' "$log" 2>/dev/null; then
            marker=1
            break
        fi
        if ! kill -0 "$pid" 2>/dev/null; then
            break
        fi
        sleep 2
    done
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    if [ "$marker" != 1 ]; then
        printf 'boot-test: %s did not reach san-live\n' "$name" >&2
        tail -c 2500 "$log" >&2 || true
        return 1
    fi
    printf 'boot-test: %s %s\n' "$name" "$(grep 'san-live: mode=' "$log" | tail -1)"
}

run_qemu bios-live -machine pc,accel=kvm:tcg
cp -f "$VARS_SRC" "$work/vars.fd"
run_qemu uefi-live \
    -machine q35,accel=kvm:tcg \
    -drive if=pflash,format=raw,readonly=on,file="$CODE" \
    -drive if=pflash,format=raw,file="$work/vars.fd"

# Second GRUB entry, on SeaBIOS. OVMF_CODE_4M.fd has Secure Boot off.
diag_log=$work/bios-diag.log
qemu-system-x86_64 \
    -machine pc,accel=kvm:tcg \
    -cpu max -m 2048 -smp 2 \
    -display none -no-reboot \
    -cdrom "$ISO" \
    -drive file="$work/disk.raw",format=raw,if=virtio \
    -netdev user,id=n0,restrict=on \
    -device virtio-net-pci,netdev=n0 \
    -serial tcp:127.0.0.1:4577,server=on,wait=off \
    >/dev/null 2>"$work/qemu-diag.err" &
diag_pid=$!
set +e
python3 - "$diag_log" << 'PY'
import socket, sys, time
log = sys.argv[1]
deadline = time.time() + 45
s = None
while time.time() < deadline and s is None:
    try:
        s = socket.create_connection(("127.0.0.1", 4577), timeout=2)
    except OSError:
        time.sleep(0.3)
if s is None:
    sys.stderr.write("boot-test: diagnostic serial did not open\n")
    sys.exit(2)
buf = b""
sent = False
end = time.time() + 150
while time.time() < end:
    s.settimeout(1)
    try:
        chunk = s.recv(65536)
    except socket.timeout:
        chunk = b""
    if chunk:
        buf += chunk
        with open(log, "ab") as fh:
            fh.write(chunk)
    if not sent and b"SAN appliance" in buf:
        time.sleep(0.4)
        s.sendall(b"\x1b[B\r")
        sent = True
    if b"san-live: mode=diagnostic" in buf:
        sys.exit(0)
sys.stderr.write("boot-test: diagnostic marker missing\n")
sys.exit(1)
PY
diag_rc=$?
set -e
kill "$diag_pid" 2>/dev/null || true
wait "$diag_pid" 2>/dev/null || true
[ "$diag_rc" -eq 0 ] || { tail -c 2500 "$diag_log" >&2 || true; exit 1; }
printf 'boot-test: bios-diagnostic %s\n' "$(grep 'san-live: mode=' "$diag_log" | tail -1)"

sha_after=$(sha256sum "$work/disk.raw" | awk '{print $1}')
[ "$sha_before" = "$sha_after" ] || { printf 'boot-test: disposable disk changed\n' >&2; exit 1; }
printf 'boot-test: disposable disk unchanged\n'
