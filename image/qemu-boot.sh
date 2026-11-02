#!/bin/bash
# Boot the installed disk image in qemu. Does not touch a physical disk.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
RAW="${SCRIPT_DIR}/out/san-appliance.raw"
CODE=""
for candidate in \
    /usr/share/OVMF/OVMF_CODE_4M.fd \
    /usr/share/OVMF/OVMF_CODE.fd
do
    if [ -f "$candidate" ]; then
        CODE="$candidate"
        break
    fi
done
VARS_SRC=""
for candidate in \
    /usr/share/OVMF/OVMF_VARS_4M.fd \
    /usr/share/OVMF/OVMF_VARS.fd
do
    if [ -f "$candidate" ]; then
        VARS_SRC="$candidate"
        break
    fi
done
[ -n "$CODE" ] && [ -n "$VARS_SRC" ] || { echo "OVMF firmware not found" >&2; exit 1; }
[ -f "$RAW" ] || { echo "missing $RAW" >&2; exit 1; }
VARS=$(mktemp)
cp "$VARS_SRC" "$VARS"
exec qemu-system-x86_64 \
    -m 3072 -smp 2 \
    -machine q35,accel=kvm:tcg \
    -nographic \
    -serial mon:stdio \
    -drive if=pflash,format=raw,readonly=on,file="$CODE" \
    -drive if=pflash,format=raw,file="$VARS" \
    -drive file="$RAW",format=raw,if=virtio \
    -nic user,model=virtio-net-pci,hostfwd=tcp::2222-:22
