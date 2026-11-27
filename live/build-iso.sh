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
ISO=${SAN_ISO:-$DIST/san-appliance-live-24.04-amd64.iso}
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
    "deb http://archive.ubuntu.com/ubuntu noble main universe" \
    "deb http://archive.ubuntu.com/ubuntu noble-updates main universe" \
    "deb http://security.ubuntu.com/ubuntu noble-security main"

for forbidden in ubiquity ubiquity-frontend-gtk subiquity curtin cloud-init; do
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
"$SCRIPT_DIR/install-webui.sh" "$ROOT"
install -m 0644 "$SCRIPT_DIR/webui/front.mjs" "$ROOT/usr/lib/san-webui/front.mjs"
install -m 0644 "$SCRIPT_DIR/webui/boards.mjs" "$ROOT/usr/lib/san-webui/boards.mjs"
install -m 0644 "$SCRIPT_DIR/webui/guard.mjs" "$ROOT/usr/lib/san-webui/guard.mjs"
install -m 0644 "$SCRIPT_DIR/webui/audit-route.mjs" "$ROOT/usr/lib/san-webui/audit-route.mjs"
install -m 0644 "$SCRIPT_DIR/webui/session.mjs" "$ROOT/usr/lib/san-webui/session.mjs"
install -m 0644 "$SCRIPT_DIR/webui/status.mjs" "$ROOT/usr/lib/san-webui/status.mjs"
install -d -m 0755 "$ROOT/usr/share/san"
cp -a "$REPO_ROOT/docs/first-boot.md" "$ROOT/usr/share/san/first-boot.txt"
cp -a "$REPO_ROOT/docs/rollback.md" "$ROOT/usr/share/san/rollback.txt"
chmod 0755 \
    "$ROOT/usr/local/sbin/san-live-boot" \
    "$ROOT/usr/local/sbin/san-conf-bind" \
    "$ROOT/usr/local/sbin/san-net-apply" \
    "$ROOT/usr/local/sbin/san-restore-boot" \
    "$ROOT/usr/local/sbin/san-raid" \
    "$ROOT/usr/local/sbin/san-raid-scrub" \
    "$ROOT/usr/local/sbin/san-raid-pace" \
    "$ROOT/usr/local/sbin/san-health" \
    "$ROOT/usr/local/sbin/san-portal-follow" \
    "$ROOT/usr/local/sbin/san-lvm" \
    "$ROOT/usr/local/sbin/san-nvme" \
    "$ROOT/usr/local/sbin/san-perf" \
    "$ROOT/usr/local/sbin/san-ab" \
    "$ROOT/usr/local/sbin/san-ollama" \
    "$ROOT/usr/local/sbin/san-ssh" \
    "$ROOT/usr/local/sbin/san-tui" \
    "$ROOT/usr/local/sbin/san-webui-listen-check" \
    "$ROOT/usr/local/sbin/san-webui-secrets" \
    "$ROOT/usr/local/sbin/san-webui-roles" \
    "$ROOT/usr/local/sbin/san-webui-cert" \
    "$ROOT/usr/local/sbin/san-syslog" \
    "$ROOT/usr/local/sbin/san-release-listen" \
    "$ROOT/usr/local/sbin/san-release-apply" \
    "$ROOT/usr/local/sbin/san-channels" \
    "$ROOT/usr/local/sbin/san-rescue" \
    "$ROOT/usr/local/sbin/san-latency" \
    "$ROOT/usr/local/sbin/san-scsi-log" \
    "$ROOT/usr/local/sbin/san-ntp" \
    "$ROOT/usr/local/sbin/san-comfort" \
    "$ROOT/usr/local/sbin/san-replicate" \
    "$ROOT/usr/local/sbin/san-inventory-api" \
    "$ROOT/usr/local/sbin/san-metrics" \
    "$ROOT/usr/local/sbin/san-retire" \
    "$ROOT/usr/local/sbin/san-quota" \
    "$ROOT/usr/local/sbin/san-snapshot" \
    "$ROOT/usr/local/sbin/san-clone" \
    "$ROOT/usr/local/sbin/san-thin" \
    "$ROOT/usr/local/sbin/san-crypt" \
    "$ROOT/usr/local/sbin/san-chap-rotate" \
    "$ROOT/usr/local/sbin/san-portal-watch" \
    "$ROOT/usr/local/sbin/san-lacp" \
    "$ROOT/usr/local/sbin/san-auto-boot" \
    "$ROOT/usr/local/sbin/san-auto-layout" \
    "$ROOT/usr/local/sbin/san-release-boot" \
    "$ROOT/usr/local/sbin/san-release-health"
chown root:root "$ROOT/etc/sudoers.d" "$ROOT/etc/sudoers.d/sanadmin"
chmod 0755 "$ROOT/etc/sudoers.d"
chmod 0440 "$ROOT/etc/sudoers.d/sanadmin"
chown -R root:root \
    "$ROOT/usr/local/sbin" \
    "$ROOT/usr/lib/san-tui" \
    "$ROOT/usr/share/san" \
    "$ROOT/etc/systemd/system" \
    "$ROOT/etc/ssh/sshd_config.d" \
    "$ROOT/etc/mdadm" \
    "$ROOT/etc/lvm" \
    "$ROOT/etc/profile.d/san-otp-lock.sh" \
    "$ROOT/usr/lib/udev/rules.d/64-md-raid-assembly.rules" \
    "$ROOT/etc/sysusers.d" \
    "$ROOT/etc/tmpfiles.d"
printf 'LANG=C.UTF-8\n' > "$ROOT/etc/default/locale"
rm -f "$ROOT"/etc/ssh/ssh_host_*
if [ -d "$ROOT/etc/ssh/sshd_config.d" ]; then
    grep -q 'PermitRootLogin no' "$ROOT/etc/ssh/sshd_config.d/10-san.conf"
    grep -q 'PasswordAuthentication no' "$ROOT/etc/ssh/sshd_config.d/10-san.conf"
fi
if [ -e "$ROOT/home/sanadmin/.ssh/authorized_keys" ]; then
    die "an admin key is baked into the image"
fi
systemctl --root="$ROOT" disable ssh.service ssh.socket >/dev/null 2>&1 || true
# The packaged LIO units call targetctl restore and load target_core_mod
# even when nothing is configured. Exports stay on san-restore only.
systemctl --root="$ROOT" disable \
    rtslib-fb-targetctl.service target.service targetclid.service targetclid.socket \
    >/dev/null 2>&1 || true
systemctl --root="$ROOT" mask \
    rtslib-fb-targetctl.service target.service targetclid.service targetclid.socket \
    >/dev/null 2>&1 || true
systemctl --root="$ROOT" enable \
    san-live-boot.service \
    san-inventory.service \
    san-restore.service \
    san-conf-bind.service \
    san-net-apply.service \
    san-raid.service \
    san-raid-scrub.timer \
    san-raid-pace.timer \
    san-health.timer \
    san-portal-follow.timer \
    san-lvm.service \
    san-nvme.service \
    san-perf.service \
    san-ab.service \
    san-ollama.service \
    san-ssh.service \
    san-webui-secrets.service \
    san-webui-roles.service \
    san-webui-cert.service \
    san-webui.service \
    san-release.service \
    san-channels.service \
    san-rescue.service \
    san-latency.service \
    san-scsi-log.service \
    san-ntp.service \
    san-syslog.service \
    san-release-apply.service \
    san-factory.service \
    san-config-import.service \
    san-config-export.service \
    san-comfort.service \
    san-replicate.service \
    san-inventory-api.service \
    san-metrics.service \
    san-retire.service \
    san-quota.service \
    san-snapshot.service \
    san-clone.service \
    san-thin.service \
    san-crypt.service \
    san-chap-rotate.timer \
    san-portal-watch.service \
    san-lacp.service \
    san-auto.service \
    san-auto-layout.service \
    san-release-boot.service \
    san-release-health.service
systemctl --root="$ROOT" disable systemd-time-wait-sync.service >/dev/null 2>&1 || true
systemctl --root="$ROOT" mask systemd-time-wait-sync.service >/dev/null 2>&1 || true

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
nvme_note=$ROOT/usr/share/san/nvme-of.txt
if find "$ROOT/lib/modules/$kver" -name 'nvmet.ko*' | grep -q . \
    && find "$ROOT/lib/modules/$kver" -name 'nvmet-tcp.ko*' | grep -q .; then
    printf '%s\n' "modules nvmet et nvmet-tcp présents dans $kver" > "$nvme_note"
else
    printf '%s\n' "NVMe-oF arrêté : modules absents de $kver" > "$nvme_note"
    systemctl --root="$ROOT" disable san-nvme.service >/dev/null 2>&1 || true
fi
chroot "$ROOT" python3 - << 'PY'
import inspect
from pathlib import Path
from rtslib_fb.tcm import FileIOStorageObject
names = set(inspect.signature(FileIOStorageObject.__init__).parameters)
text = "rtslib-fb n'expose pas de backstore io_uring sur ce noyau ; io_uring n'est pas activé."
if "uring" in names or "io_uring" in names:
    text = "rtslib-fb expose un paramètre io_uring."
Path("/usr/share/san/io-uring.txt").write_text(text + "\n", encoding="utf-8")
PY
install -d -m 0755 "$ROOT/usr/lib/san-app/image"
printf '0\n' > "$ROOT/usr/lib/san-app/image/version"
printf '%s\n' '#!/bin/sh' 'set -eu' 'test -s "$(dirname "$0")/version"' > "$ROOT/usr/lib/san-app/image/health"
chmod 0755 "$ROOT/usr/lib/san-app/image/health"

[ ! -e "$ROOT/var/lib/san/webui/webui.env" ] || die "webui.env is baked into the image"
[ ! -e "$ROOT/var/lib/san/webui/tls/key.pem" ] || die "webui private key is baked into the image"
[ ! -e "$ROOT/etc/san/factory" ] || die "factory marker is baked into the image"
[ ! -e "$ROOT/etc/san/github.token" ] || die "a GitHub token is baked into the image"
if find "$ROOT" \( -name volume.key -o -name secrets.json \) | grep -q .; then
    die "a volume key or CHAP secret is baked into the image"
fi
if grep -R -l -E 'BEGIN (OPENSSH |RSA |EC |ENCRYPTED )?PRIVATE KEY' \
    "$ROOT/usr" "$ROOT/etc" "$ROOT/var" 2>/dev/null | grep -q .; then
    die "a private key is baked into the image"
fi
[ -f "$ROOT/usr/share/san/kernel-pin" ] || die "kernel pin missing from the image"
grep -Eq '^6\.8\.0-[0-9]+\.[0-9]+$' "$ROOT/usr/share/san/kernel-pin" \
    || die "kernel pin is not a 6.8 package"
[ ! -e "$ROOT/run/san/webui-roles.json" ] || die "WebUI role passwords are baked into the image"
[ ! -e "$ROOT/etc/san/syslog.json" ] || die "a syslog target is baked into the image"
[ ! -e "$ROOT/etc/rsyslog.d/99-san-remote.conf" ] || die "a syslog forwarder is baked into the image"
[ ! -e "$ROOT/var/lib/san/conf/audit.key" ] || die "an audit key is baked into the image"
[ ! -e "$ROOT/etc/san/audit.key" ] || die "an audit key is baked into the image"
grep -q '/usr/lib/san-webui/scripts/serve-local.mjs' "$ROOT/etc/systemd/system/san-webui.service"
if grep -q 'server/index.mjs' "$ROOT/etc/systemd/system/san-webui.service"; then
    die "san-webui.service must not launch server/index.mjs"
fi
[ -x "$ROOT/usr/bin/node" ] || die "nodejs did not provide /usr/bin/node"

log "squashfs"
mksquashfs "$ROOT" "$TREE/casper/filesystem.squashfs" -comp gzip -noappend -no-progress
cp -a "$ROOT/boot/vmlinuz-$kver" "$TREE/casper/vmlinuz"
cp -a "$ROOT/boot/initrd.img-$kver" "$TREE/casper/initrd"
du -sx --block-size=1 "$ROOT" | awk '{print $1}' > "$TREE/casper/filesystem.size"
chroot "$ROOT" dpkg-query -W -f='${Package}\t${Version}\n' | sort > "$TREE/casper/filesystem.manifest"
cp -a "$SCRIPT_DIR/grub.cfg" "$TREE/boot/grub/grub.cfg"
printf '%s\n' "SAN appliance 24.04 LTS noble live" > "$TREE/.disk/info"

if grep -q 'toram' "$SCRIPT_DIR/grub.cfg"; then
    die "grub.cfg enables toram without a RAM measurement"
fi
log "md5sum.txt"
(
    cd "$TREE"
    find . -type f ! -name md5sum.txt -print0 | sort -z | xargs -0 md5sum
) > "$TREE/md5sum.txt"

log "iso"
rm -f "$ISO"
grub-mkrescue -o "$ISO" "$TREE" -- -volid SANLIVE24
xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$WORK/grub.fromiso.cfg" >/dev/null
cmp -s "$SCRIPT_DIR/grub.cfg" "$WORK/grub.fromiso.cfg" || die "ISO grub.cfg does not match the source menu"
xorriso -indev "$ISO" -find /casper -name filesystem.squashfs | grep -q filesystem.squashfs || die "squashfs missing from ISO"
if ! xorriso -indev "$ISO" -report_el_torito as_mkisofs 2>/dev/null | grep -Eiq 'efi|uefi'; then
    die "ISO has no UEFI El Torito image"
fi
xorriso -indev "$ISO" -find /md5sum.txt | grep -q md5sum.txt || die "md5sum.txt missing from ISO"
rm -rf "$WORK/cdrom"
mkdir -p "$WORK/cdrom" "$ROOT/mnt/cdrom"
xorriso -osirrox on -indev "$ISO" -extract / "$WORK/cdrom" >/dev/null
mount --bind "$WORK/cdrom" "$ROOT/mnt/cdrom"
if ! chroot "$ROOT" /usr/lib/casper/casper-md5check /mnt/cdrom /mnt/cdrom/md5sum.txt; then
    umount "$ROOT/mnt/cdrom" || true
    die "casper-md5check failed"
fi
umount "$ROOT/mnt/cdrom"

manifest=$DIST/packages.manifest
meta=$DIST/build-metadata.json
cp -a "$TREE/casper/filesystem.manifest" "$manifest"
sha=$(sha256sum "$ISO" | awk '{print $1}')
printf '%s  %s\n' "$sha" "$(basename "$ISO")" > "$ISO.sha256"
(
    cd "$(dirname "$ISO")"
    sha256sum -c "$(basename "$ISO.sha256")"
)

git_commit=$(git -C "$REPO_ROOT" -c safe.directory="$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
git_dirty=false
if [ -n "$(git -C "$REPO_ROOT" -c safe.directory="$REPO_ROOT" status --porcelain 2>/dev/null || true)" ]; then
    git_dirty=true
fi
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
webui_commit=$(tr -d '[:space:]' < "$ROOT/usr/lib/san-webui/SOURCE_COMMIT")
node_version=$(chroot "$ROOT" /usr/bin/node -p 'process.version')
python3 - "$meta" "$kver" "$git_commit" "$git_dirty" "$built_at" "$sha" "$webui_commit" "$node_version" << 'PY'
import json, sys
path, kver, commit, dirty, built_at, sha, webui_commit, node_version = sys.argv[1:]
doc = {
    "name": "san-appliance-live",
    "ubuntu": "24.04",
    "codename": "noble",
    "arch": "amd64",
    "kernel": kver,
    "boot": "casper",
    "filesystem": "squashfs",
    "firmwares_booted_by_grub": ["bios", "uefi"],
    "secure_boot": "unsupported",
    "bit_reproducible": False,
    "installer": False,
    "lio": "python3-rtslib-fb",
    "tui": "san-tui",
    "webui": "esos-webui",
    "webui_commit": webui_commit,
    "webui_listen": "127.0.0.1:3000",
    "node": node_version,
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
