#!/bin/sh
# Copie la TUI dans une racine d'image. N'active rien sur la machine hôte.
set -eu
dest=${1:?racine de destination}
src=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
mkdir -p "$dest/usr/lib/san-tui" "$dest/usr/local/sbin" "$dest/etc/systemd/system" "$dest/etc/san"
rm -rf "$dest/usr/lib/san-tui/san_tui"
cp -a "$src/san_tui" "$dest/usr/lib/san-tui/san_tui"
cp -a "$src/bin/san-tui" "$dest/usr/local/sbin/san-tui"
cp -a "$src/systemd/san-inventory.service" "$dest/etc/systemd/system/san-inventory.service"
cp -a "$src/systemd/san-restore.service" "$dest/etc/systemd/system/san-restore.service"
chmod 0755 "$dest/usr/local/sbin/san-tui"
echo "installé dans $dest"
echo "activer dans l'image, pas sur l'hôte : systemctl --root=$dest enable san-inventory.service san-restore.service"
