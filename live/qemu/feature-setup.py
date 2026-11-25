#!/usr/bin/python3
"""Prépare conf, RAID, LVM, NVMe-oF et le slot A/B. Le boot suivant les applique."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys

ADMIN_MAC = "52:54:00:aa:00:01"
STORAGE_MAC = "52:54:00:aa:00:02"
ADMIN_ADDR = "192.168.10.10/24"
STORAGE_ADDR = "10.1.0.10/24"
HOST_NQN = "nqn.2026-11.local.client:nvme"
SUB_NQN = "nqn.2026-11.local.san:nvme"
PAYLOAD = b"SAN-SLOT-B\n"


def run(args, stdin=None):
    proc = subprocess.run(args, text=True, input=stdin, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "")
        raise SystemExit("commande en échec : {}".format(" ".join(args[:6])))
    return proc.stdout


def iface_by_mac(mac):
    want = mac.lower()
    for entry in pathlib.Path("/sys/class/net").iterdir():
        if entry.name == "lo":
            continue
        if (entry / "address").read_text(encoding="utf-8").strip().lower() == want:
            return entry.name
    raise SystemExit("interface absente : {}".format(mac))


def volume_by_serial(serial):
    inventory = json.loads(run(["san-tui", "inventory"]))
    kernel = None
    for block in pathlib.Path("/sys/block").iterdir():
        serial_path = block / "serial"
        if serial_path.is_file() and serial_path.read_text(encoding="utf-8").strip() == serial:
            kernel = block.name
            break
    if kernel is None:
        raise SystemExit("série absente : {}".format(serial))
    for disk in inventory["disks"]:
        if disk.get("kernel") == kernel:
            return disk
    raise SystemExit("volume absent : {}".format(kernel))


def write_json(path, data, mode=0o644):
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(mode)


def main():
    cmdline = pathlib.Path("/proc/cmdline").read_text(encoding="utf-8")
    if "toram" in cmdline.split():
        raise SystemExit("toram est sur la ligne de commande")
    toram = json.loads(pathlib.Path("/run/san/toram.json").read_text(encoding="utf-8"))
    if toram.get("enabled"):
        raise SystemExit("toram activé")

    admin = iface_by_mac(ADMIN_MAC)
    storage = iface_by_mac(STORAGE_MAC)
    run(["ip", "link", "set", admin, "up"])
    run(["ip", "link", "set", storage, "up"])
    run(["ip", "addr", "replace", ADMIN_ADDR, "dev", admin])
    run(["ip", "addr", "replace", STORAGE_ADDR, "dev", storage])
    run([
        "san-tui", "network-set",
        "--admin-iface", admin, "--admin-address", ADMIN_ADDR,
        "--storage-iface", storage, "--storage-address", STORAGE_ADDR,
    ])

    conf = volume_by_serial("conf01")
    keep = volume_by_serial("keep01")
    raid_a = volume_by_serial("raidA")
    raid_b = volume_by_serial("raidB")
    foreign_a = volume_by_serial("foreignA")
    foreign_b = volume_by_serial("foreignB")
    lvm_ok = volume_by_serial("lvmOK")
    lvm_no = volume_by_serial("lvmNO")
    nvme = volume_by_serial("nvme01")
    ab = volume_by_serial("ab01")

    run(["san-tui", "persist-select", "--role", "conf", "--id", conf["id"], "--confirm", conf["id"]])
    mount = pathlib.Path("/var/lib/san/conf")
    mount.mkdir(parents=True, exist_ok=True)
    if not mount.is_mount():
        run(["mount", "-o", "noatime", "LABEL=SANCONF", str(mount)])
    subprocess.run(["cp", "-a", "/etc/san/chosen.json", str(mount / "chosen.json")], check=True)

    yes = "yes\n"
    run(["mdadm", "--create", "/dev/md/san0", "--level=1", "--raid-devices=2",
         "--metadata=1.2", "--force", "--run", raid_a["id"], raid_b["id"]], stdin=yes)
    run(["mdadm", "--stop", "/dev/md/san0"])
    run(["mdadm", "--create", "/dev/md/foreign", "--level=1", "--raid-devices=2",
         "--metadata=1.2", "--force", "--run", foreign_a["id"], foreign_b["id"]], stdin=yes)
    run(["mdadm", "--stop", "/dev/md/foreign"])
    write_json(mount / "raid.json", {
        "version": 1,
        "arrays": [{
            "name": "san0",
            "level": "raid1",
            "members": [raid_a["id"], raid_b["id"]],
        }],
    })

    run(["pvcreate", "-ff", "-y", lvm_ok["id"]])
    run(["vgcreate", "sanvg", lvm_ok["id"]])
    run(["lvcreate", "-y", "-n", "sanlv", "-L", "16M", "sanvg"])
    run(["vgchange", "-an", "sanvg"])
    run(["pvcreate", "-ff", "-y", lvm_no["id"]])
    run(["vgcreate", "othervg", lvm_no["id"]])
    run(["lvcreate", "-y", "-n", "otherlv", "-L", "16M", "othervg"])
    run(["vgchange", "-an", "othervg"])
    write_json(mount / "lvm.json", {
        "version": 1,
        "groups": [{"name": "sanvg", "pvs": [lvm_ok["id"]], "lvs": ["sanlv"]}],
    })

    write_json(mount / "exports.json", {
        "version": 1,
        "network": {
            "admin": {"iface": admin, "address": ADMIN_ADDR},
            "storage": {"iface": storage, "address": STORAGE_ADDR},
        },
        "targets": [],
    })
    write_json(mount / "secrets.json", {}, 0o600)
    write_json(mount / "nvme.json", {
        "version": 1,
        "network": {
            "admin": {"iface": admin, "address": ADMIN_ADDR},
            "storage": {"iface": storage, "address": STORAGE_ADDR},
        },
        "subsystems": [{
            "nqn": SUB_NQN,
            "host": HOST_NQN,
            "allow_any_host": False,
            "portal": {"address": "10.1.0.10", "port": 4420},
            "volume": {
                "id": nvme["id"],
                "wwn": nvme.get("wwn") or "",
                "serial": nvme.get("serial") or "",
                "size": nvme.get("size"),
                "block_size": nvme.get("block_size"),
            },
        }],
    })

    script = "label: gpt\n,16MiB,L\n,16MiB,L\n,,L\n"
    run(["sfdisk", "--force", ab["path"]], stdin=script)
    run(["partx", "-u", ab["path"]])
    run(["udevadm", "settle"])
    parts = sorted(pathlib.Path("/dev/disk/by-id").glob(ab["id"].split("/")[-1] + "-part*"))
    if len(parts) < 3:
        raise SystemExit("partitions A/B absentes : {}".format(parts))
    slot_b = parts[1]
    data = parts[2]
    pattern = (b"SAN-DATA-KEEP\n" * 80)[:1024]
    fd = os.open(str(data), os.O_WRONLY)
    try:
        os.write(fd, pattern)
        os.fsync(fd)
    finally:
        os.close(fd)
    digest = hashlib.sha256(pattern).hexdigest()
    incoming = mount / "incoming"
    incoming.mkdir(exist_ok=True)
    image = incoming / "root.img"
    image.write_bytes(PAYLOAD)
    image_digest = hashlib.sha256(PAYLOAD).hexdigest()
    write_json(mount / "ab.json", {
        "version": 1,
        "active": "a",
        "disk": ab["id"],
        "slots": {
            "a": {"id": str(parts[0])},
            "b": {"id": str(slot_b)},
        },
        "data": {"id": str(data)},
    })
    write_json(mount / "pending-update.json", {
        "version": 1,
        "slot": "b",
        "image": str(image),
        "sha256": image_digest,
    })
    run(["sync"])
    print("SAN-RESULT " + json.dumps({
        "toram": toram,
        "conf": conf["id"],
        "keep": keep["id"],
        "raid": [raid_a["id"], raid_b["id"]],
        "foreign": [foreign_a["id"], foreign_b["id"]],
        "lvm_ok": lvm_ok["id"],
        "lvm_no": lvm_no["id"],
        "nvme": nvme["id"],
        "data": str(data),
        "data_sha256": digest,
        "slot_b": str(slot_b),
        "image_sha256": image_digest,
        "cmdline_toram": "toram" in cmdline.split(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
