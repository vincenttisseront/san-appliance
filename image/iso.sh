#!/bin/bash
# Build the appliance disk, then a BIOS/UEFI installation ISO.
# The default menu entry does not write any disk.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
OUT="${REPO_ROOT}/image/out"
RAW="${OUT}/san-appliance.raw"
ZST="${OUT}/san-appliance.raw.zst"
ISO="${OUT}/san-appliance.iso"
TREE="${OUT}/iso-tree"

log() { printf 'san-appliance: %s\n' "$*"; }
die() { printf 'san-appliance: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "run as root"
fi

if [ "${SAN_SKIP_BUILD:-}" != 1 ]; then
    "${SCRIPT_DIR}/build.sh"
fi
[ -f "$RAW" ] || die "missing ${RAW}"
command -v zstd >/dev/null || die "zstd is required"
command -v grub-mkrescue >/dev/null || die "grub-mkrescue is required"

log "compress disk image"
rm -f "$ZST"
zstd -T0 -3 -f -o "$ZST" "$RAW"

rm -rf "$TREE"
mkdir -p "$TREE/boot/grub"
cp "${SCRIPT_DIR}/grub.cfg" "$TREE/boot/grub/grub.cfg"
# Kernel and initrd for the installer menu. Taken from the ESP of the disk.
loop=$(losetup -f --show "$RAW")
cleanup() {
    losetup -d "$loop" 2>/dev/null || true
}
trap cleanup EXIT
partx -a "$loop"
mnt=$(mktemp -d)
mount "${loop}p1" "$mnt"
cp "$mnt/vmlinuz" "$TREE/boot/vmlinuz"
cp "$mnt/initrd.img" "$TREE/boot/initrd.img"
umount "$mnt"
rmdir "$mnt"
losetup -d "$loop"
trap - EXIT
cp "$ZST" "$TREE/san-appliance.raw.zst"

log "write ${ISO}"
rm -f "$ISO"
grub-mkrescue -o "$ISO" "$TREE" -- -volid SANAPPL
log "iso: ${ISO}"
ls -lh "$ISO" "$ZST" "$RAW"
