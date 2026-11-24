#!/usr/bin/python3
"""Configure one LIO export on the live appliance. Does not format any disk."""

import json
import pathlib
import shutil
import subprocess
import sys

SECRET = "Init1SecretOK1"
TARGET = "iqn.2026-11.local.san:disk"
INITIATOR = "iqn.2026-11.local.client:host"
CHAP_USER = "client1"
ADMIN_MAC = "52:54:00:aa:00:01"
STORAGE_MAC = "52:54:00:aa:00:02"
ADMIN_ADDR = "192.168.10.10/24"
STORAGE_ADDR = "10.1.0.10/24"


def run(args):
    proc = subprocess.run(args, text=True, capture_output=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "")
        raise SystemExit("commande en échec : {}".format(" ".join(args)))
    return proc.stdout


def iface_by_mac(mac):
    want = mac.lower()
    for entry in pathlib.Path("/sys/class/net").iterdir():
        if entry.name == "lo":
            continue
        current = (entry / "address").read_text(encoding="utf-8").strip().lower()
        if current == want:
            return entry.name
    raise SystemExit("interface absente : {}".format(mac))


def volume_by_serial(serial):
    kernel = None
    for block in pathlib.Path("/sys/block").iterdir():
        serial_path = block / "serial"
        if serial_path.is_file() and serial_path.read_text(encoding="utf-8").strip() == serial:
            kernel = block.name
            break
    if kernel is None:
        raise SystemExit("série absente : {}".format(serial))
    inventory = json.loads(run(["san-tui", "inventory"]))
    for disk in inventory["disks"]:
        if disk.get("kernel") == kernel:
            return disk
    raise SystemExit("volume absent : {}".format(kernel))


def main():
    admin = iface_by_mac(ADMIN_MAC)
    storage = iface_by_mac(STORAGE_MAC)
    run(["ip", "link", "set", admin, "up"])
    run(["ip", "link", "set", storage, "up"])
    run(["ip", "addr", "replace", ADMIN_ADDR, "dev", admin])
    run(["ip", "addr", "replace", STORAGE_ADDR, "dev", storage])
    run([
        "san-tui", "network-set",
        "--admin-iface", admin,
        "--admin-address", ADMIN_ADDR,
        "--storage-iface", storage,
        "--storage-address", STORAGE_ADDR,
    ])

    conf = volume_by_serial("conf01")
    export = volume_by_serial("export01")
    other = volume_by_serial("other01")
    run([
        "san-tui", "persist-select",
        "--role", "conf",
        "--id", conf["id"],
        "--confirm", conf["id"],
    ])
    if not pathlib.Path("/etc/san/chosen.json").is_file():
        raise SystemExit("chosen.json absent après persist-select")

    pathlib.Path("/var/lib/san/conf").mkdir(parents=True, exist_ok=True)
    if not pathlib.Path("/var/lib/san/conf").is_mount():
        run(["mount", "-o", "noatime", "LABEL=SANCONF", "/var/lib/san/conf"])
    shutil.copy("/etc/san/chosen.json", "/var/lib/san/conf/chosen.json")

    secret_path = pathlib.Path("/run/san-chap")
    secret_path.write_text(SECRET + "\n", encoding="utf-8")
    secret_path.chmod(0o600)
    run([
        "san-tui", "export-add",
        "--iqn", TARGET,
        "--portal", "10.1.0.10",
        "--volume", export["id"],
        "--lun", "0",
        "--initiator", INITIATOR,
        "--chap-user", CHAP_USER,
        "--chap-secret-file", str(secret_path),
        "--confirm", export["id"],
    ])
    secret_path.unlink()
    restored = json.loads(run(["san-tui", "restore"]))
    run(["sync"])
    listen = subprocess.run(
        ["ss", "-H", "-ltn", "sport", "=", ":3260"],
        text=True,
        capture_output=True,
    )
    alerts = json.loads(run(["san-tui", "alerts"]))
    blob = json.dumps(alerts, ensure_ascii=False)
    if SECRET in blob:
        raise SystemExit("le secret CHAP est visible dans les alertes")
    print("SAN-RESULT " + json.dumps({
        "admin": admin,
        "storage": storage,
        "conf": conf["id"],
        "export": export["id"],
        "other": other["id"],
        "restored": restored,
        "listen": listen.stdout.strip(),
    }, ensure_ascii=False))
    if TARGET not in restored.get("exported", []):
        raise SystemExit("restore n'a pas exporté la cible")
    if ":3260" not in listen.stdout:
        raise SystemExit("le portail 3260 n'écoute pas")


if __name__ == "__main__":
    main()
