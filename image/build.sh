#!/bin/bash
# Build the appliance rootfs and a GPT disk (san-esp, san-root-a, san-root-b, san-data).
# image/scst.sh compiles SCST and installs the binary packages, not scst-dkms.
# image/iso.sh packs that disk into a bootable installation ISO.
# ZFS, Docker and the ESOS ISO are out of this script.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
OUT="${REPO_ROOT}/image/out"
WORK="${REPO_ROOT}/image/work"
ROOTFS="${WORK}/rootfs"
BUILD_ROOT="${WORK}/build-root"
WEBUI_SRC="${WORK}/esos-webui"

log() { printf 'san-appliance: %s\n' "$*"; }
die() { printf 'san-appliance: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "run as root so mmdebstrap can build the chroots"
fi

read_kv() {
    local key="$1" file="$2" value
    value=$(awk -F= -v k="$key" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$file")
    [ -n "$value" ] || die "missing ${key} in ${file}"
    printf '%s' "$value"
}

pkg_list() {
    awk 'NF && $1 !~ /^#/ { printf "%s%s", sep, $1; sep = "," } END { print "" }' "$1"
}

RELEASE_FILE="${SCRIPT_DIR}/ubuntu-release"
SUITE=$(read_kv suite "$RELEASE_FILE")
ARCH=$(read_kv arch "$RELEASE_FILE")
MIRROR=$(read_kv mirror "$RELEASE_FILE")
COMPONENTS=$(read_kv components "$RELEASE_FILE")
VERSION=$(read_kv version "$RELEASE_FILE")

SHA=$(tr -d '[:space:]' < "${SCRIPT_DIR}/third_party/esos-webui.sha256")
[[ "$SHA" =~ ^[0-9a-f]{40}$ ]] || die "esos-webui pin is not a 40-digit git sha"

command -v mmdebstrap >/dev/null || die "mmdebstrap is required"
command -v git >/dev/null || die "git is required"

INCLUDE=$(pkg_list "${SCRIPT_DIR}/packages.txt")
BUILD_INCLUDE=$(pkg_list "${SCRIPT_DIR}/build-packages.txt")

SOURCES=(
    "deb ${MIRROR} ${SUITE} ${COMPONENTS}"
    "deb ${MIRROR} ${SUITE}-updates ${COMPONENTS}"
    "deb ${MIRROR} ${SUITE}-security ${COMPONENTS}"
)

cleanup_build_mounts() {
    local mnt
    for mnt in dev sys proc; do
        if mountpoint -q "${BUILD_ROOT}/${mnt}"; then
            umount "${BUILD_ROOT}/${mnt}" || true
        fi
    done
}

if [ "${SAN_SCST_ONLY:-}" = 1 ]; then
    [ -d "$ROOTFS/lib/modules" ] || die "appliance root is missing; run a full build first"
    "${SCRIPT_DIR}/scst.sh"
    log "scst packages installed"
    exit 0
fi

if [ "${SAN_POPULATE_ONLY:-}" != 1 ]; then
log "bootstrap build chroot (${SUITE} ${VERSION})"
rm -rf "$BUILD_ROOT" "$ROOTFS" "$WEBUI_SRC"
mkdir -p "$OUT" "$WORK"
mmdebstrap --variant=minbase --arch="$ARCH" \
    --aptopt='Apt::Install-Recommends "false";' \
    --include="$BUILD_INCLUDE" \
    "$SUITE" "$BUILD_ROOT" "${SOURCES[@]}"

log "fetch esos-webui ${SHA}"
git -c safe.directory='*' init "$WEBUI_SRC"
git -c safe.directory='*' -C "$WEBUI_SRC" remote add origin https://github.com/vincenttisseront/esos-webui.git
git -c safe.directory='*' -C "$WEBUI_SRC" fetch --depth 1 origin "$SHA"
git -c safe.directory='*' -C "$WEBUI_SRC" checkout --detach FETCH_HEAD
[ "$(git -c safe.directory='*' -C "$WEBUI_SRC" rev-parse HEAD)" = "$SHA" ] || die "pin mismatch"

mkdir -p "$BUILD_ROOT/src"
cp -a "$WEBUI_SRC" "$BUILD_ROOT/src/esos-webui"
rm -rf "$BUILD_ROOT/src/esos-webui/.git"

for mnt in proc sys dev; do
    mount --bind "/${mnt}" "${BUILD_ROOT}/${mnt}"
done
trap cleanup_build_mounts EXIT
if [ -f /etc/resolv.conf ]; then
    cp /etc/resolv.conf "$BUILD_ROOT/etc/resolv.conf"
fi

chroot "$BUILD_ROOT" /bin/bash -lc 'cd /src/esos-webui && NODE_OPTIONS=--max-old-space-size=3072 npm ci && NODE_OPTIONS=--max-old-space-size=3072 npm run build'
cleanup_build_mounts
trap - EXIT

[ -f "$BUILD_ROOT/src/esos-webui/.output/server/index.mjs" ] || die "webui bundle missing"
[ -f "$BUILD_ROOT/src/esos-webui/scripts/serve-local.mjs" ] || die "serve-local.mjs missing from the pinned tree"

log "bootstrap appliance root (${SUITE})"
mmdebstrap --variant=minbase --arch="$ARCH" \
    --aptopt='Apt::Install-Recommends "false";' \
    --include="$INCLUDE" \
    "$SUITE" "$ROOTFS" "${SOURCES[@]}"

mkdir -p "$ROOTFS/usr/lib/san-webui/server/db"
cp -a "$BUILD_ROOT/src/esos-webui/scripts" "$ROOTFS/usr/lib/san-webui/scripts"
cp -a "$BUILD_ROOT/src/esos-webui/.output" "$ROOTFS/usr/lib/san-webui/.output"
# Drizzle looks up server/db/migrations from the process working directory.
cp -a "$BUILD_ROOT/src/esos-webui/server/db/migrations" "$ROOTFS/usr/lib/san-webui/server/db/migrations"
cp -a "$BUILD_ROOT/src/esos-webui/package.json" "$ROOTFS/usr/lib/san-webui/package.json"
[ -f "$ROOTFS/usr/lib/san-webui/server/db/migrations/meta/_journal.json" ] || die "drizzle journal missing"
chown -R root:root "$ROOTFS/usr/lib/san-webui"
find "$ROOTFS/usr/lib/san-webui" -type d -exec chmod 0755 {} +
find "$ROOTFS/usr/lib/san-webui" -type f -exec chmod 0644 {} +

cp -a "${SCRIPT_DIR}/rootfs/." "$ROOTFS/"
chmod 0755 \
    "$ROOTFS/usr/local/sbin/san-webui-listen-check" \
    "$ROOTFS/usr/local/sbin/san-webui-secrets" \
    "$ROOTFS/etc/initramfs-tools/hooks/san-install" \
    "$ROOTFS/etc/initramfs-tools/scripts/init-premount/san-install"

if [ ! -x "$ROOTFS/usr/bin/node" ] && [ -x "$ROOTFS/usr/bin/nodejs" ]; then
    ln -s nodejs "$ROOTFS/usr/bin/node"
fi

log "generate initramfs"
mount --bind /proc "$ROOTFS/proc"
mount --bind /sys "$ROOTFS/sys"
mount --bind /dev "$ROOTFS/dev"
mounted_pts=0
if mountpoint -q /dev/pts; then
    mount --bind /dev/pts "$ROOTFS/dev/pts" && mounted_pts=1
fi
set +e
chroot "$ROOTFS" bash -lc 'if compgen -G /boot/initrd.img-* > /dev/null; then update-initramfs -u -k all; else update-initramfs -c -k all; fi'
initrd_rc=$?
set -e
if [ "$mounted_pts" = 1 ]; then
    umount "$ROOTFS/dev/pts" || true
fi
umount "$ROOTFS/dev" || true
umount "$ROOTFS/sys" || true
umount "$ROOTFS/proc" || true
[ "$initrd_rc" -eq 0 ] || die "update-initramfs failed"

fstab_line='LABEL=san-data /var/lib/san ext4 noatime,nofail,x-systemd.device-timeout=10s 0 2'
if ! grep -q 'LABEL=san-data' "$ROOTFS/etc/fstab"; then
    printf '%s\n' "$fstab_line" >> "$ROOTFS/etc/fstab"
fi

systemctl --root="$ROOTFS" enable \
    san-webui-secrets.service \
    san-webui.service \
    nftables.service \
    ssh.service \
    systemd-networkd.service
systemctl --root="$ROOTFS" mask apt-daily.timer apt-daily-upgrade.timer

for forbidden in build-essential dkms scst-dkms zfsutils-linux docker.io docker-ce; do
    state=$(chroot "$ROOTFS" dpkg-query -W -f='${db:Status-Status}' "$forbidden" 2>/dev/null || true)
    [ "$state" = "installed" ] && die "${forbidden} is installed in the appliance root"
done

[ -x "$ROOTFS/usr/bin/node" ] || die "nodejs did not provide /usr/bin/node"
grep -q '^PermitRootLogin no$' "$ROOTFS/etc/ssh/sshd_config.d/10-san.conf"
grep -q 'tcp dport 3000 iifname != "lo" drop' "$ROOTFS/etc/nftables.conf"
grep -q '/usr/lib/san-webui/scripts/serve-local.mjs' "$ROOTFS/etc/systemd/system/san-webui.service"
if grep -q 'server/index.mjs' "$ROOTFS/etc/systemd/system/san-webui.service"; then
    die "san-webui.service must not launch server/index.mjs"
fi

"${SCRIPT_DIR}/scst.sh"

fi

populate_esp() {
    local raw="$1"
    local loop mnt_root mnt_esp kver efi label data_label
    loop=$(losetup -f --show "$raw")
    mnt_root=$(mktemp -d)
    mnt_esp=$(mktemp -d)
    esp_img=""
    cleanup_esp() {
        umount "$mnt_esp" 2>/dev/null || true
        umount "$mnt_root" 2>/dev/null || true
        rmdir "$mnt_esp" "$mnt_root" 2>/dev/null || true
        if [ -n "${esp_img:-}" ]; then
            rm -f "$esp_img"
        fi
        if [ -n "${loop:-}" ]; then
            losetup -d "$loop" 2>/dev/null || true
        fi
    }
    trap cleanup_esp EXIT
    # udev often creates the partition devices before partx -a, which then
    # returns EBUSY. The devices are what matter.
    partx -a "$loop" >/dev/null 2>&1 || true
    local wait_i
    for wait_i in 1 2 3 4 5 6 7 8 9 10; do
        if [ -b "${loop}p1" ] && [ -b "${loop}p2" ] && [ -b "${loop}p4" ]; then
            break
        fi
        sleep 0.2
    done
    if [ ! -b "${loop}p1" ] || [ ! -b "${loop}p2" ] || [ ! -b "${loop}p4" ]; then
        cleanup_esp
        die "GPT partitions did not appear on ${loop}"
    fi
    if ! mount "${loop}p2" "$mnt_root"; then
        cleanup_esp
        die "cannot mount san-root-a"
    fi
    shopt -s nullglob
    local mods=("${mnt_root}/lib/modules"/*)
    shopt -u nullglob
    if [ "${#mods[@]}" -eq 0 ]; then
        cleanup_esp
        die "no kernel modules in san-root-a"
    fi
    kver=$(basename "${mods[0]}")
    # systemd-repart's ext4 copy leaves /boot empty. The kernel and initrd
    # still live in the staged rootfs.
    if [ ! -f "${mnt_root}/boot/vmlinuz-${kver}" ] && [ -d "${ROOTFS}/boot" ]; then
        cp -a "${ROOTFS}/boot/." "${mnt_root}/boot/"
    fi
    if [ ! -f "${mnt_root}/boot/vmlinuz-${kver}" ] || [ ! -f "${mnt_root}/boot/initrd.img-${kver}" ]; then
        cleanup_esp
        die "kernel or initrd missing for ${kver}"
    fi
    efi="${mnt_root}/usr/lib/systemd/boot/efi/systemd-bootx64.efi"
    if [ ! -f "$efi" ]; then
        cleanup_esp
        die "systemd-bootx64.efi missing"
    fi
    # The build host has no vfat driver. Partition devices on this kernel
    # sometimes report a short size, so the ESP is built as a plain file and
    # written at the GPT offset.
    command -v mkfs.vfat >/dev/null || { cleanup_esp; die "dosfstools is required to fill san-esp"; }
    command -v mcopy >/dev/null || { cleanup_esp; die "mtools is required to fill san-esp"; }
    local start_sectors sector_count esp_bytes
    start_sectors=$(partx -o START -g -n 1 "$raw" | tr -d ' ')
    sector_count=$(partx -o SECTORS -g -n 1 "$raw" | tr -d ' ')
    esp_bytes=$((sector_count * 512))
    [ "$esp_bytes" -ge $((500 * 1024 * 1024)) ] || { cleanup_esp; die "san-esp is ${esp_bytes} bytes"; }
    esp_img=$(mktemp)
    truncate -s "$esp_bytes" "$esp_img"
    mkfs.vfat -F 32 -n SAN-ESP "$esp_img"
    export MTOOLS_SKIP_CHECK=1
    mmd -i "$esp_img" ::/EFI ::/EFI/BOOT ::/EFI/systemd ::/loader ::/loader/entries
    mcopy -i "$esp_img" "$efi" ::/EFI/BOOT/BOOTX64.EFI
    mcopy -i "$esp_img" "$efi" ::/EFI/systemd/systemd-bootx64.efi
    mcopy -i "$esp_img" "${mnt_root}/boot/vmlinuz-${kver}" ::/vmlinuz
    mcopy -i "$esp_img" "${mnt_root}/boot/initrd.img-${kver}" ::/initrd.img
    local loader_conf entry
    loader_conf=$(mktemp)
    entry=$(mktemp)
    printf '%s\n' 'default san.conf' 'timeout 3' > "$loader_conf"
    cat > "$entry" << EOF
title SAN appliance
linux /vmlinuz
initrd /initrd.img
options root=LABEL=san-root-a rw console=ttyS0,115200
EOF
    mcopy -i "$esp_img" "$loader_conf" ::/loader/loader.conf
    mcopy -i "$esp_img" "$entry" ::/loader/entries/san.conf
    rm -f "$loader_conf" "$entry"
    dd if="$esp_img" of="$raw" bs=512 seek="$start_sectors" conv=notrunc status=none
    rm -f "$esp_img"
    esp_img=""
    label=$(blkid -s LABEL -o value "${loop}p2" || true)
    data_label=$(blkid -s LABEL -o value "${loop}p4" || true)
    umount "$mnt_root"
    losetup -d "$loop"
    loop=""
    rmdir "$mnt_esp" "$mnt_root"
    [ "$label" = "san-root-a" ] || die "san-root-a filesystem label is '${label}'"
    [ "$data_label" = "san-data" ] || die "san-data filesystem label is '${data_label}'"
    trap - EXIT
    log "bootloader installed on san-esp, root label ${label}"
}

if [ "${SAN_POPULATE_ONLY:-}" = 1 ]; then
    raw="${OUT}/san-appliance.raw"
    [ -f "$raw" ] || die "missing ${raw}"
    [ -d "${ROOTFS}/boot" ] || die "missing staged /boot; cannot refill the ESP"
    populate_esp "$raw"
    log "disk image: ${raw}"
    log "done"
    exit 0
fi

tar -C "$ROOTFS" -cf "$OUT/rootfs.tar" .
log "rootfs archive: ${OUT}/rootfs.tar"

if command -v systemd-repart >/dev/null; then
    command -v mkfs.vfat >/dev/null || die "dosfstools (mkfs.vfat) is required on the build host"
    command -v mkfs.ext4 >/dev/null || die "e2fsprogs (mkfs.ext4) is required on the build host"
    raw="${OUT}/san-appliance.raw"
    rm -f "$raw"
    systemd-repart \
        --definitions="${SCRIPT_DIR}/repart.d" \
        --empty=create \
        --size=18G \
        --offline=yes \
        --copy-source="$ROOTFS" \
        "$raw"
    populate_esp "$raw"
    log "disk image: ${raw}"
    log "san-root-b stays formatted and empty; the rootfs is copied to san-root-a"
else
    log "systemd-repart is absent; GPT image was not built"
fi

log "done"
