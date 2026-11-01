#!/bin/bash
# Build the skeleton rootfs and, when systemd-repart is available, a GPT disk.
# SCST, ZFS, Docker and the ESOS ISO are out of this script.
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

log "bootstrap build chroot (${SUITE} ${VERSION})"
rm -rf "$BUILD_ROOT" "$ROOTFS" "$WEBUI_SRC"
mkdir -p "$OUT" "$WORK"
mmdebstrap --variant=minbase --arch="$ARCH" --include="$BUILD_INCLUDE" \
    "$SUITE" "$BUILD_ROOT" "${SOURCES[@]}"

log "fetch esos-webui ${SHA}"
git init "$WEBUI_SRC"
git -C "$WEBUI_SRC" remote add origin https://github.com/vincenttisseront/esos-webui.git
git -C "$WEBUI_SRC" fetch --depth 1 origin "$SHA"
git -C "$WEBUI_SRC" checkout --detach FETCH_HEAD
[ "$(git -C "$WEBUI_SRC" rev-parse HEAD)" = "$SHA" ] || die "pin mismatch"

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

chroot "$BUILD_ROOT" /bin/bash -lc 'cd /src/esos-webui && npm ci && npm run build'
cleanup_build_mounts
trap - EXIT

[ -f "$BUILD_ROOT/src/esos-webui/.output/server/index.mjs" ] || die "webui bundle missing"
[ -f "$BUILD_ROOT/src/esos-webui/scripts/serve-local.mjs" ] || die "serve-local.mjs missing from the pinned tree"

log "bootstrap appliance root (${SUITE})"
mmdebstrap --variant=minbase --arch="$ARCH" --include="$INCLUDE" \
    "$SUITE" "$ROOTFS" "${SOURCES[@]}"

mkdir -p "$ROOTFS/usr/lib/san-webui"
cp -a "$BUILD_ROOT/src/esos-webui/scripts" "$ROOTFS/usr/lib/san-webui/scripts"
cp -a "$BUILD_ROOT/src/esos-webui/.output" "$ROOTFS/usr/lib/san-webui/.output"
chown -R root:root "$ROOTFS/usr/lib/san-webui"
find "$ROOTFS/usr/lib/san-webui" -type d -exec chmod 0755 {} +
find "$ROOTFS/usr/lib/san-webui" -type f -exec chmod 0644 {} +

cp -a "${SCRIPT_DIR}/rootfs/." "$ROOTFS/"
chmod 0755 \
    "$ROOTFS/usr/local/sbin/san-webui-listen-check" \
    "$ROOTFS/usr/local/sbin/san-webui-secrets"

fstab_line='LABEL=san-data /var/lib/san ext4 noatime,nofail,x-systemd.device-timeout=10s 0 2'
if ! grep -q 'LABEL=san-data' "$ROOTFS/etc/fstab"; then
    printf '%s\n' "$fstab_line" >> "$ROOTFS/etc/fstab"
fi

systemctl --root="$ROOTFS" enable \
    san-webui-secrets.service \
    san-webui.service \
    nftables.service \
    ssh.service
systemctl --root="$ROOTFS" mask apt-daily.timer apt-daily-upgrade.timer

for forbidden in build-essential dkms scst scst-dkms zfsutils-linux docker.io docker-ce; do
    status=$(chroot "$ROOTFS" dpkg-query -W -f='${Status}' "$forbidden" 2>/dev/null || true)
    case "$status" in
        *installed*) die "${forbidden} is installed in the appliance root" ;;
    esac
done

[ -x "$ROOTFS/usr/bin/node" ] || die "nodejs did not provide /usr/bin/node"
grep -q '^PermitRootLogin no$' "$ROOTFS/etc/ssh/sshd_config.d/10-san.conf"
grep -q 'tcp dport 3000 iifname != "lo" drop' "$ROOTFS/etc/nftables.conf"
grep -q '/usr/lib/san-webui/scripts/serve-local.mjs' "$ROOTFS/etc/systemd/system/san-webui.service"
if grep -q 'server/index.mjs' "$ROOTFS/etc/systemd/system/san-webui.service"; then
    die "san-webui.service must not launch server/index.mjs"
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
        --size=32G \
        --offline=yes \
        --copy-source="$ROOTFS" \
        "$raw"
    log "disk image: ${raw}"
    log "san-root-b stays formatted and empty; the rootfs is copied to san-root-a"
else
    log "systemd-repart is absent; GPT image was not built"
fi

log "done"
