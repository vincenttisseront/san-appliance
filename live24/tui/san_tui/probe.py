"""Inventaire en lecture seule : disques, identifiants stables, interfaces."""

import json
import os
import re
import subprocess
from pathlib import Path


BOOT_TARGETS = {"/", "/boot", "/boot/efi", "/run/live", "/run/live/medium", "/cdrom"}


def inventory(probe):
    """Photographie le système. Ne monte rien, n'exporte rien, n'assemble rien."""
    return {
        "disks": probe.list_volumes(),
        "interfaces": probe.list_interfaces(),
        "exports": [],
    }


def parse_mountinfo(text):
    mounts = []
    for line in text.splitlines():
        parts = line.split()
        if "-" not in parts or len(parts) < 5:
            continue
        dash = parts.index("-")
        if dash + 2 >= len(parts):
            continue
        mounts.append((parts[dash + 2], parts[4]))
    return mounts


def disk_of(kernel_name):
    """Nom du disque entier pour une partition (sda1 -> sda, nvme0n1p2 -> nvme0n1)."""
    if re.match(r"nvme\d+n\d+p\d+$", kernel_name):
        return re.sub(r"p\d+$", "", kernel_name)
    if re.match(r"mmcblk\d+p\d+$", kernel_name):
        return re.sub(r"p\d+$", "", kernel_name)
    if re.match(r"(?:sd|vd|xvd|hd)[a-z]+\d+$", kernel_name):
        return re.sub(r"\d+$", "", kernel_name)
    return kernel_name


def boot_disks(mounts):
    found = set()
    for source, target in mounts:
        if target in BOOT_TARGETS:
            found.add(disk_of(source.rsplit("/", 1)[-1]))
    return found


def mounted_names(mounts):
    return {source.rsplit("/", 1)[-1] for source, _target in mounts}


class SysfsProbe:
    """Lecture de sysfs et de /dev/disk/by-id. Aucune écriture."""

    def __init__(self, sysfs="/sys", dev="/dev", proc="/proc"):
        self.sysfs = Path(sysfs)
        self.dev = Path(dev)
        self.proc = Path(proc)

    def list_volumes(self):
        mounts = parse_mountinfo(_read(self.proc / "self" / "mountinfo"))
        boots = boot_disks(mounts)
        mounted = mounted_names(mounts)
        by_id = self.dev / "disk" / "by-id"
        chosen = {}
        if by_id.is_dir():
            for entry in sorted(by_id.iterdir()):
                if entry.name.startswith(("dm-", "lvm-", "md-")):
                    continue
                try:
                    target = entry.resolve()
                except OSError:
                    continue
                name = target.name
                rank = _id_rank(entry.name)
                current = chosen.get(name)
                if current is None or rank < current[0]:
                    chosen[name] = (rank, entry.name, target)
        volumes = []
        for name, (_rank, id_name, target) in sorted(chosen.items()):
            volumes.append(self._volume(name, id_name, target, mounts, boots, mounted))
        return volumes

    def get_volume(self, stable_id):
        needle = stable_id.split("/")[-1]
        for volume in self.list_volumes():
            if volume["id"].endswith("/" + needle) or volume["id"] == stable_id:
                return volume
        return None

    def list_interfaces(self):
        net = self.sysfs / "class" / "net"
        found = []
        if not net.is_dir():
            return found
        for iface in sorted(p for p in net.iterdir() if p.name != "lo"):
            found.append({
                "name": iface.name,
                "addresses": [],
                "operstate": _read(iface / "operstate"),
                "mac": _read(iface / "address"),
            })
        return found

    def interface_has_address(self, iface, address):
        for item in self.list_interfaces():
            if item["name"] == iface:
                hosts = [a.split("/", 1)[0] for a in item["addresses"]]
                return address in hosts
        return False

    def _volume(self, name, id_name, target, mounts, boots, mounted):
        block = self.sysfs / "class" / "block" / name
        parent = disk_of(name)
        identity = self.sysfs / "class" / "block" / parent
        own_mounts = [tgt for src, tgt in mounts if src.rsplit("/", 1)[-1] == name]
        holders = _dirnames(block / "holders")
        disk_mounted = name in mounted or any(
            disk_of(dev) == name and dev in mounted for dev in mounted
        )
        serial = _serial(block) or _serial(identity)
        return {
            "id": "/dev/disk/by-id/" + id_name,
            "kernel": name,
            "path": str(target),
            "wwn": _first(identity, ("device/wwid", "wwid", "device/eui")),
            "serial": serial,
            "model": _read(identity / "device" / "model"),
            "size": _size(block),
            "block_size": _block_size(block),
            "mounts": own_mounts if not disk_mounted or own_mounts else own_mounts,
            "holders": holders,
            "active": bool(holders) or (disk_mounted and name == parent and not own_mounts),
            "boot": parent in boots,
            "kind": "block",
            "group_id": _read(block / "san_group"),
            "state": _read(block / "device" / "state"),
            "io_errors": _io_errors(block),
        }


def _id_rank(name):
    order = ("wwn-", "nvme-eui.", "eui.", "nvme-", "scsi-", "ata-", "virtio-", "usb-")
    for index, prefix in enumerate(order):
        if name.startswith(prefix):
            return index + (50 if "-part" in name else 0)
    return 1000


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _first(block, relatives):
    for relative in relatives:
        value = _read(block / relative)
        if value:
            return value
    return ""


def _serial(block):
    value = _first(block, ("device/serial", "device/vpd_pg80", "serial"))
    return value.split()[0] if value else ""


def _block_size(block):
    raw = _read(block / "queue" / "logical_block_size")
    if raw.isdigit():
        return int(raw)
    return None


def _size(block):
    raw = _read(block / "size")
    if not raw.isdigit():
        return None
    return int(raw) * 512


def _io_errors(block):
    raw = _read(block / "device" / "ioerr_cnt").lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    if not raw or any(ch not in "0123456789abcdef" for ch in raw):
        return 0
    return int(raw, 16)


def _dirnames(path):
    if not path.is_dir():
        return []
    return sorted(entry.name for entry in path.iterdir())


class CommandProbe(SysfsProbe):
    """Sysfs plus les adresses vues par `ip -j addr`. Lecture seule."""

    def list_interfaces(self):
        base = {item["name"]: item for item in super().list_interfaces()}
        for name, addresses, state, mac in _ip_addresses():
            item = base.setdefault(name, {
                "name": name,
                "addresses": [],
                "operstate": state,
                "mac": mac,
            })
            item["addresses"] = addresses
            if state:
                item["operstate"] = state
            if mac:
                item["mac"] = mac
        return [base[name] for name in sorted(base)]


def _ip_addresses():
    for candidate in ("/usr/sbin/ip", "/sbin/ip", "ip"):
        if candidate == "ip" or os.path.exists(candidate):
            ip = candidate
            break
    else:
        return []
    try:
        proc = subprocess.run(
            [ip, "-j", "addr"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []
    found = []
    for item in data:
        name = item.get("ifname")
        if not name or name == "lo":
            continue
        addresses = []
        for addr in item.get("addr_info") or []:
            local = addr.get("local")
            prefix = addr.get("prefixlen")
            if local:
                addresses.append("{}/{}".format(local, prefix))
        found.append((
            name,
            addresses,
            item.get("operstate") or "",
            item.get("address") or "",
        ))
    return found
