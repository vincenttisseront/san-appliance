#!/usr/bin/python3
"""Applique un seul réglage de banc, ou décrit le matériel virtuel."""

import json
import pathlib
import subprocess
import sys

STORAGE_MAC = "52:54:00:aa:00:02"


def storage_iface():
    for entry in pathlib.Path("/sys/class/net").iterdir():
        if entry.name == "lo":
            continue
        mac = (entry / "address").read_text(encoding="utf-8").strip().lower()
        if mac == STORAGE_MAC:
            return entry.name
    raise SystemExit("interface stockage absente")


def backstore():
    core = pathlib.Path("/sys/kernel/config/target/core")
    for udev in sorted(core.glob("*/*/udev_path")):
        dev = udev.read_text(encoding="utf-8").strip()
        if dev.startswith("/dev/"):
            return dev
    raise SystemExit("backstore absent")


def scheduler_path():
    name = pathlib.Path(backstore()).resolve().name
    return pathlib.Path("/sys/block") / name / "queue/scheduler"


def probe():
    iface = storage_iface()
    ethtool = subprocess.run(["ethtool", "-k", iface], text=True, capture_output=True)
    features = {}
    for line in (ethtool.stdout or "").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        features[key.strip()] = value.strip()
    governor = ""
    gov = pathlib.Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if gov.is_file():
        governor = gov.read_text(encoding="utf-8").strip()
    cache = ""
    name = pathlib.Path(backstore()).resolve().name
    cache_path = pathlib.Path("/sys/block") / name / "device/scsi_disk"
    for candidate in pathlib.Path("/sys/block").glob(name + "/queue/write_cache"):
        cache = candidate.read_text(encoding="utf-8").strip()
    uring = False
    try:
        import inspect
        from rtslib_fb.tcm import FileIOStorageObject
        names = set(inspect.signature(FileIOStorageObject.__init__).parameters)
        uring = "uring" in names or "io_uring" in names
    except Exception:
        uring = False
    print("SAN-RESULT " + json.dumps({
        "scheduler": scheduler_path().read_text(encoding="utf-8").strip(),
        "features": features,
        "governor": governor,
        "write_cache": cache,
        "io_uring": uring,
        "iface": iface,
        "cache_paths": [str(cache_path)],
    }, ensure_ascii=False))


def set_scheduler(name):
    if name not in {"none", "mq-deadline"}:
        raise SystemExit("scheduler refusé")
    path = scheduler_path()
    current = path.read_text(encoding="utf-8")
    if name not in current.replace("[", "").replace("]", "").split():
        raise SystemExit("scheduler indisponible : {}".format(current))
    path.write_text(name, encoding="utf-8")


def set_mtu(value):
    iface = storage_iface()
    proc = subprocess.run(["ip", "link", "set", iface, "mtu", str(value)], check=False)
    if proc.returncode != 0:
        raise SystemExit("mtu refusé")


def set_param(name, value):
    root = pathlib.Path("/sys/kernel/config/target/iscsi")
    found = list(root.glob("*/tpgt_*/param/{}".format(name)))
    if not found:
        raise SystemExit("paramètre absent : {}".format(name))
    for path in found:
        path.write_text(str(value), encoding="utf-8")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "probe":
        probe()
        return
    if action == "scheduler":
        set_scheduler(sys.argv[2])
    elif action == "mtu":
        set_mtu(sys.argv[2])
    elif action == "burst":
        set_param("ImmediateData", "Yes")
        set_param("FirstBurstLength", "65536")
    elif action == "digest":
        set_param("HeaderDigest", "CRC32C")
        set_param("DataDigest", "CRC32C")
    elif action == "restore-digest":
        set_param("HeaderDigest", "None")
        set_param("DataDigest", "None")
    else:
        raise SystemExit("action inconnue")
    print("SAN-RESULT " + json.dumps({"ok": True, "action": action}, ensure_ascii=False))


if __name__ == "__main__":
    main()
