#!/bin/bash
# Compile SCST against the appliance kernel and install the binary packages.
# scst-dkms and the compiler stay out of the delivered root.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
WORK="${REPO_ROOT}/image/work"
ROOTFS="${WORK}/rootfs"
SCST_SRC="${WORK}/scst"
SCST_BUILD="${WORK}/scst-build"

log() { printf 'san-appliance: %s\n' "$*"; }
die() { printf 'san-appliance: %s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    die "run as root"
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

PIN=$(tr -d '[:space:]' < "${SCRIPT_DIR}/third_party/scst.sha256")
[[ "$PIN" =~ ^[0-9a-f]{40}$ ]] || die "scst pin is not a 40-digit git sha"

[ -d "$ROOTFS/lib/modules" ] || die "appliance root has no kernel modules"
mapfile -t kvers < <(find "$ROOTFS/lib/modules" -mindepth 1 -maxdepth 1 -type d -printf '%f\n')
[ "${#kvers[@]}" -eq 1 ] || die "expected one kernel in the appliance root, found: ${kvers[*]:-none}"
KVER="${kvers[0]}"
[ -f "$ROOTFS/boot/config-${KVER}" ] || die "missing /boot/config-${KVER} in the appliance root"
log "SCST ${PIN} for kernel ${KVER}"

SOURCES=(
    "deb ${MIRROR} ${SUITE} ${COMPONENTS}"
    "deb ${MIRROR} ${SUITE}-updates ${COMPONENTS}"
    "deb ${MIRROR} ${SUITE}-security ${COMPONENTS}"
)

need_bootstrap=0
if [ ! -x "${SCST_BUILD}/usr/bin/gcc" ]; then
    need_bootstrap=1
fi
if [ ! -d "${SCST_BUILD}/usr/src/linux-headers-${KVER}" ] && [ ! -e "${SCST_BUILD}/lib/modules/${KVER}/build" ]; then
    need_bootstrap=1
fi
if [ "$need_bootstrap" -eq 1 ]; then
    log "bootstrap SCST build chroot"
    rm -rf "$SCST_BUILD"
    mkdir -p "$WORK"
    INCLUDE="$(pkg_list "${SCRIPT_DIR}/build-scst-packages.txt"),linux-headers-${KVER}"
    mmdebstrap --variant=minbase --arch="$ARCH" \
        --aptopt='Apt::Install-Recommends "false";' \
        --include="$INCLUDE" \
        "$SUITE" "$SCST_BUILD" "${SOURCES[@]}"
fi

if [ ! -e "${SCST_BUILD}/lib/modules/${KVER}/build" ]; then
    headers=""
    for candidate in \
        "${SCST_BUILD}/usr/src/linux-headers-${KVER}" \
        "${SCST_BUILD}/usr/src/linux-headers-${KVER%-generic}"
    do
        if [ -d "$candidate" ]; then
            headers="$candidate"
            break
        fi
    done
    [ -n "$headers" ] || die "linux-headers-${KVER} did not land in the build chroot"
    mkdir -p "${SCST_BUILD}/lib/modules/${KVER}"
    ln -sfn "${headers#"${SCST_BUILD}"}" "${SCST_BUILD}/lib/modules/${KVER}/build"
fi

short_pin=${PIN:0:12}
if compgen -G "${SCST_BUILD}/src/scst/dpkg/scst_*${short_pin}*_amd64.deb" > /dev/null \
    && compgen -G "${SCST_BUILD}/src/scst/dpkg/scstadmin_*${short_pin}*_amd64.deb" > /dev/null \
    && compgen -G "${SCST_BUILD}/src/scst/dpkg/iscsi-scst_*${short_pin}*_amd64.deb" > /dev/null; then
    log "reusing SCST packages built for ${short_pin}"
else
log "fetch SCST ${PIN}"
rm -rf "$SCST_SRC"
git -c safe.directory='*' init "$SCST_SRC"
git -c safe.directory='*' -C "$SCST_SRC" remote add origin https://github.com/SCST-project/scst.git
git -c safe.directory='*' -C "$SCST_SRC" fetch --depth 1 origin "$PIN"
git -c safe.directory='*' -C "$SCST_SRC" checkout --detach FETCH_HEAD
[ "$(git -c safe.directory='*' -C "$SCST_SRC" rev-parse HEAD)" = "$PIN" ] || die "SCST pin mismatch"

rm -rf "${SCST_BUILD}/src/scst"
mkdir -p "${SCST_BUILD}/src" "${SCST_BUILD}/boot"
cp -a "$SCST_SRC" "${SCST_BUILD}/src/scst"
cp -a "$ROOTFS/boot/config-${KVER}" "${SCST_BUILD}/boot/config-${KVER}"

for mnt in proc sys dev; do
    if ! mountpoint -q "${SCST_BUILD}/${mnt}"; then
        mount --bind "/${mnt}" "${SCST_BUILD}/${mnt}"
    fi
done
cleanup_mounts() {
    local mnt
    for mnt in dev sys proc; do
        if mountpoint -q "${SCST_BUILD}/${mnt}"; then
            umount "${SCST_BUILD}/${mnt}" || true
        fi
    done
}
trap cleanup_mounts EXIT
if [ -f /etc/resolv.conf ]; then
    cp /etc/resolv.conf "${SCST_BUILD}/etc/resolv.conf"
fi

log "make dpkg"
chroot "$SCST_BUILD" /bin/bash -lc \
    "cd /src/scst && make dpkg KVER='${KVER}' KDIR='/lib/modules/${KVER}/build' WERROR=n"
cleanup_mounts
trap - EXIT
fi

mapfile -t debs < <(find "${SCST_BUILD}/src/scst/dpkg" -maxdepth 1 -type f -name '*.deb' | sort)
[ "${#debs[@]}" -gt 0 ] || die "make dpkg produced no .deb"
log "packages: ${debs[*]}"

install_debs=()
for deb in "${debs[@]}"; do
    base=$(basename "$deb")
    case "$base" in
        scst-dkms_*|scst-dev_*)
            log "not installed: ${base}"
            ;;
        scst_*|scstadmin_*|iscsi-scst_*)
            install_debs+=("$deb")
            ;;
        *)
            log "ignored: ${base}"
            ;;
    esac
done
[ "${#install_debs[@]}" -eq 3 ] || die "expected scst, scstadmin and iscsi-scst debs"

log "install runtime dependencies"
for mnt in proc sys dev; do
    if ! mountpoint -q "${ROOTFS}/${mnt}"; then
        mount --bind "/${mnt}" "${ROOTFS}/${mnt}"
    fi
done
cleanup_root_mounts() {
    local mnt
    for mnt in dev sys proc; do
        if mountpoint -q "${ROOTFS}/${mnt}"; then
            umount "${ROOTFS}/${mnt}" || true
        fi
    done
}
trap cleanup_root_mounts EXIT
if [ -f /etc/resolv.conf ]; then
    cp /etc/resolv.conf "${ROOTFS}/etc/resolv.conf"
fi
install -D -m 0644 \
    "${SCRIPT_DIR}/rootfs/etc/modprobe.d/san-scst.conf" \
    "${ROOTFS}/etc/modprobe.d/san-scst.conf"

# Resolute minbase has dpkg and no apt. Download scstadmin's runtime
# closure with an isolated apt rooted on the host, aimed at the same mirror.
apt_stage=$(mktemp -d)
mkdir -p \
    "$apt_stage/etc/apt/sources.list.d" \
    "$apt_stage/etc/apt/preferences.d" \
    "$apt_stage/etc/apt/trusted.gpg.d" \
    "$apt_stage/var/lib/apt/lists/partial" \
    "$apt_stage/var/cache/apt/archives/partial" \
    "$apt_stage/var/lib/dpkg"
# Ignore the SCST packages themselves so a half-configured scstadmin
# does not make the dependency solver stop.
python3 - "$ROOTFS/var/lib/dpkg/status" "$apt_stage/var/lib/dpkg/status" << 'PY'
import sys
from pathlib import Path
src = Path(sys.argv[1]).read_text()
drop = {"scst", "scstadmin", "iscsi-scst", "scst-dkms", "scst-dev"}
blocks = []
for block in src.split("\n\n"):
    name = None
    for line in block.splitlines():
        if line.startswith("Package: "):
            name = line.split(": ", 1)[1].strip()
            break
    if name in drop or not block.strip():
        continue
    blocks.append(block.strip())
Path(sys.argv[2]).write_text("\n\n".join(blocks) + "\n")
PY
keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg
[ -f "$keyring" ] || die "Ubuntu archive keyring not found on the build host"
cp "$keyring" "$apt_stage/etc/apt/trusted.gpg.d/ubuntu-archive-keyring.gpg"
cat > "$apt_stage/etc/apt/sources.list" << EOF
deb ${MIRROR} ${SUITE} ${COMPONENTS}
deb ${MIRROR} ${SUITE}-updates ${COMPONENTS}
deb ${MIRROR} ${SUITE}-security ${COMPONENTS}
EOF
apt-get -o "Dir=${apt_stage}" update
mapfile -t dep_pkgs < <(apt-get -o "Dir=${apt_stage}" -s install --no-install-recommends perl psmisc lsb-base | awk '/^Inst / { print $2 }')
if [ "${#dep_pkgs[@]}" -gt 0 ]; then
    log "runtime debs: ${dep_pkgs[*]}"
    (cd "$apt_stage/var/cache/apt/archives" && apt-get -o "Dir=${apt_stage}" download "${dep_pkgs[@]}")
else
    log "perl, psmisc and lsb-base are already installed"
fi

mkdir -p "${ROOTFS}/tmp/scst-debs"
rm -f "${ROOTFS}/tmp/scst-debs/"*.deb
shopt -s nullglob
for deb in "$apt_stage/var/cache/apt/archives/"*.deb; do
    cp -a "$deb" "${ROOTFS}/tmp/scst-debs/"
done
shopt -u nullglob
for deb in "${install_debs[@]}"; do
    cp -a "$deb" "${ROOTFS}/tmp/scst-debs/"
done
rm -rf "$apt_stage"
chroot "$ROOTFS" bash -lc 'dpkg -i /tmp/scst-debs/*.deb'
chroot "$ROOTFS" dpkg --configure -a
rm -rf "${ROOTFS}/tmp/scst-debs"

# The binary package preinst removes ib_srpt.ko. Ubuntu ships ib_srpt.ko.zst.
find "${ROOTFS}/lib/modules/${KVER}" -name 'ib_srpt.ko*' ! -path '*/extra/*' -delete
chroot "$ROOTFS" depmod -a "$KVER"
cleanup_root_mounts
trap - EXIT

pkg_state() {
    chroot "$ROOTFS" dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null || true
}
for forbidden in scst-dkms dkms build-essential gcc; do
    [ "$(pkg_state "$forbidden")" = "installed" ] && die "${forbidden} is installed in the appliance root"
done
for required in scst scstadmin iscsi-scst; do
    [ "$(pkg_state "$required")" = "installed" ] || die "${required} is not installed in the appliance root"
done

require_mod() {
    local name="$1"
    if ! find "${ROOTFS}/lib/modules/${KVER}" \
        \( -name "${name}.ko" -o -name "${name}.ko.zst" -o -name "${name}.ko.xz" \) \
        | grep -q .; then
        die "kernel module ${name} was not built"
    fi
    log "module ${name}"
}
require_mod scst
require_mod iscsi-scst
require_mod isert-scst
require_mod qla2x00tgt
require_mod qla2xxx_scst
require_mod ib_srpt
require_mod scst_vdisk
require_mod scst_disk

srpt=$(find "${ROOTFS}/lib/modules/${KVER}" \( -name 'ib_srpt.ko' -o -name 'ib_srpt.ko.zst' -o -name 'ib_srpt.ko.xz' \))
case "$srpt" in
    */extra/*) ;;
    *) die "ib_srpt is not the SCST module under extra/: ${srpt}" ;;
esac
[ -f "${ROOTFS}/etc/modprobe.d/san-scst.conf" ] || die "LIO blacklist missing"
log "SCST binary packages installed for ${KVER}"
