#!/bin/bash
# Build a BIOS and UEFI live ISO for Ubuntu 24.04. Not bit-reproducible.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf 'build-iso: run as root (make iso)\n' >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
WORK=$SCRIPT_DIR/work
ROOT=$WORK/rootfs
TREE=$WORK/iso
DIST=$REPO_ROOT/dist
ISO=$DIST/san-appliance-live-24.04-amd64.iso
KEYRING=/usr/share/keyrings/ubuntu-archive-keyring.gpg

log() { printf 'build-iso: %s\n' "$*"; }
die() { printf 'build-iso: %s\n' "$*" >&2; exit 1; }

"$SCRIPT_DIR/doctor.sh"

cleanup() {
    umount "$ROOT/dev/pts" 2>/dev/null || true
    umount "$ROOT/dev" 2>/dev/null || true
    umount "$ROOT/proc" 2>/dev/null || true
    umount "$ROOT/sys" 2>/dev/null || true
}
trap cleanup EXIT

rm -rf "$WORK"
mkdir -p "$ROOT" "$TREE/casper" "$TREE/boot/grub" "$TREE/.disk" "$DIST"

mapfile -t packages < <(awk 'NF && $1 !~ /^#/ {print $1}' "$SCRIPT_DIR/packages.txt")
[ "${#packages[@]}" -gt 0 ] || die "package list is empty"
include=$(IFS=,; echo "${packages[*]}")

log "bootstrap noble (${#packages[@]} packages)"
mmdebstrap --variant=minbase \
    --keyring="$KEYRING" \
    --aptopt='Apt::Install-Recommends "false";' \
    --aptopt='Acquire::AllowInsecureRepositories "false";' \
    --aptopt='APT::Get::AllowUnauthenticated "false";' \
    --include="$include" \
    noble "$ROOT" \
    "deb http://archive.ubuntu.com/ubuntu noble main" \
    "deb http://archive.ubuntu.com/ubuntu noble-updates main" \
    "deb http://security.ubuntu.com/ubuntu noble-security main"

for forbidden in ubiquity ubiquity-frontend-gtk subiquity curtin cloud-init openssh-server; do
    state=$(chroot "$ROOT" dpkg-query -W -f='${db:Status-Status}' "$forbidden" 2>/dev/null || true)
    [ "$state" = "installed" ] && die "${forbidden} is installed in the live root"
done
chroot "$ROOT" dpkg-query -W -f='${db:Status-Status}' casper | grep -qx installed || die "casper is not installed"
chroot "$ROOT" dpkg-query -W -f='${db:Status-Status}' localechooser-data | grep -qx installed || die "localechooser-data is not installed"

for required in python3-rtslib-fb targetcli-fb open-iscsi; do
    state=$(chroot "$ROOT" dpkg-query -W -f='${db:Status-Status}' "$required" 2>/dev/null || true)
    [ "$state" = "installed" ] || die "${required} is not installed in the live root"
done

log "overlay"
cp -a "$SCRIPT_DIR/rootfs/." "$ROOT/"
install_sh=$REPO_ROOT/live24/tui/install.sh
[ -x "$install_sh" ] || die "missing $install_sh"
"$install_sh" "$ROOT"
chmod 0755 \
    "$ROOT/usr/local/sbin/san-live-boot" \
    "$ROOT/usr/local/sbin/san-conf-bind" \
    "$ROOT/usr/local/sbin/san-net-apply" \
    "$ROOT/usr/local/sbin/san-restore-boot" \
    "$ROOT/usr/local/sbin/san-tui"
chown root:root "$ROOT/etc/sudoers.d" "$ROOT/etc/sudoers.d/sanadmin"
chmod 0755 "$ROOT/etc/sudoers.d"
chmod 0440 "$ROOT/etc/sudoers.d/sanadmin"
chown root:root \
    "$ROOT/usr/local/sbin/san-live-boot" \
    "$ROOT/usr/local/sbin/san-conf-bind" \
    "$ROOT/usr/local/sbin/san-net-apply" \
    "$ROOT/usr/local/sbin/san-restore-boot" \
    "$ROOT/usr/local/sbin/san-tui" \
    "$ROOT/usr/lib/san-tui" \
    "$ROOT/etc/systemd/system/san-live-boot.service" \
    "$ROOT/etc/systemd/system/san-conf-bind.service" \
    "$ROOT/etc/systemd/system/san-net-apply.service" \
    "$ROOT/etc/systemd/system/san-inventory.service" \
    "$ROOT/etc/systemd/system/san-restore.service" \
    "$ROOT/etc/systemd/system/san-restore.service.d" \
    "$ROOT/etc/systemd/system/san-restore.service.d/live.conf"
printf 'LANG=C.UTF-8\n' > "$ROOT/etc/default/locale"
systemctl --root="$ROOT" enable \
    san-live-boot.service \
    san-inventory.service \
    san-restore.service \
    san-conf-bind.service \
    san-net-apply.service

log "initramfs"
mount --bind /proc "$ROOT/proc"
mount --bind /sys "$ROOT/sys"
mount --bind /dev "$ROOT/dev"
if mountpoint -q /dev/pts; then
    mount --bind /dev/pts "$ROOT/dev/pts"
fi
chroot "$ROOT" env LC_ALL=C update-initramfs -u -k all
cleanup
trap - EXIT

shopt -s nullglob
kernels=("$ROOT/boot/vmlinuz-"*)
shopt -u nullglob
[ "${#kernels[@]}" -ge 1 ] || die "no kernel in the live root"
kver=$(basename "${kernels[0]}")
kver=${kver#vmlinuz-}
[ -f "$ROOT/boot/initrd.img-$kver" ] || die "initrd missing for $kver"
initrd_list=$(chroot "$ROOT" env LC_ALL=C lsinitramfs "/boot/initrd.img-$kver")
printf '%s\n' "$initrd_list" | grep -q 'scripts/casper$' || die "initrd has no casper script"
if ! printf '%s\n' "$initrd_list" | grep -q 'isofs.ko'; then
    die "initrd has no isofs module; the live CD would not mount"
fi
find "$ROOT/lib/modules/$kver" -name 'target_core_mod.ko*' | grep -q . \
    || die "kernel $kver has no target_core_mod"
find "$ROOT/lib/modules/$kver" -name 'iscsi_target_mod.ko*' | grep -q . \
    || die "kernel $kver has no iscsi_target_mod"
grep -q '^CONFIG_CONFIGFS_FS=' "$ROOT/boot/config-$kver" \
    || die "kernel $kver has no configfs"

log "squashfs"
mksquashfs "$ROOT" "$TREE/casper/filesystem.squashfs" -comp gzip -noappend -no-progress
cp -a "$ROOT/boot/vmlinuz-$kver" "$TREE/casper/vmlinuz"
cp -a "$ROOT/boot/initrd.img-$kver" "$TREE/casper/initrd"
du -sx --block-size=1 "$ROOT" | awk '{print $1}' > "$TREE/casper/filesystem.size"
chroot "$ROOT" dpkg-query -W -f='${Package}\t${Version}\n' | sort > "$TREE/casper/filesystem.manifest"
cp -a "$SCRIPT_DIR/grub.cfg" "$TREE/boot/grub/grub.cfg"
printf '%s\n' "SAN appliance 24.04 LTS noble live" > "$TREE/.disk/info"

log "iso"
rm -f "$ISO"
grub-mkrescue -o "$ISO" "$TREE" -- -volid SANLIVE24
xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$WORK/grub.fromiso.cfg" >/dev/null
cmp -s "$SCRIPT_DIR/grub.cfg" "$WORK/grub.fromiso.cfg" || die "ISO grub.cfg does not match the source menu"
xorriso -indev "$ISO" -find /casper -name filesystem.squashfs | grep -q filesystem.squashfs || die "squashfs missing from ISO"
if ! xorriso -indev "$ISO" -report_el_torito as_mkisofs 2>/dev/null | grep -Eiq 'efi|uefi'; then
    die "ISO has no UEFI El Torito image"
fi

manifest=$DIST/packages.manifest
meta=$DIST/build-metadata.json
cp -a "$TREE/casper/filesystem.manifest" "$manifest"
sha=$(sha256sum "$ISO" | awk '{print $1}')
printf '%s  %s\n' "$sha" "$(basename "$ISO")" > "$ISO.sha256"

git_commit=$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
git_dirty=false
if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null || true)" ]; then
    git_dirty=true
fi
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
python3 - "$meta" "$kver" "$git_commit" "$git_dirty" "$built_at" "$sha" << 'PY'
import json, sys
path, kver, commit, dirty, built_at, sha = sys.argv[1:]
doc = {
    "name": "san-appliance-live",
    "ubuntu": "24.04",
    "codename": "noble",
    "arch": "amd64",
    "kernel": kver,
    "boot": "casper",
    "filesystem": "squashfs",
    "firmwares_booted_by_grub": ["bios", "uefi"],
    "secure_boot": "not-tested",
    "bit_reproducible": False,
    "installer": False,
    "lio": "python3-rtslib-fb",
    "tui": "san-tui",
    "default_entry": "SAN appliance live",
    "diagnostic_entry": "SAN appliance diagnostic (no LUN export)",
    "persistence": "nopersistent",
    "git_commit": commit,
    "git_dirty": dirty == "true",
    "built_at": built_at,
    "iso": "san-appliance-live-24.04-amd64.iso",
    "iso_sha256": sha,
    "packages_manifest": "packages.manifest",
    "apt_suites_verified": ["noble", "noble-updates", "noble-security"],
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
PY

log "iso $ISO"
log "sha256 $sha"
ls -lh "$ISO" "$ISO.sha256" "$manifest" "$meta"
