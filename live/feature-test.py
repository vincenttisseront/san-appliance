#!/usr/bin/python3
"""QEMU: RAID, LVM, NVMe-oF, A/B, SSH, toram, Ollama en échec."""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ISO = Path(os.environ.get("SAN_ISO", REPO / "dist" / "san-appliance-live-24.04-amd64.iso"))
GUEST = Path(__file__).resolve().parent / "qemu"


def load_iscsi():
    spec = importlib.util.spec_from_file_location("iscsi_test", Path(__file__).resolve().parent / "iscsi-test.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    iscsi = load_iscsi()
    iscsi.require(ISO.is_file(), "ISO absente")
    iscsi.require(Path("/dev/kvm").exists(), "/dev/kvm absent")
    work = Path("/var/tmp/san-feature-test")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    disks = {}
    for name, size in {
        "conf01": 32, "keep01": 32, "raidA": 32, "raidB": 32,
        "foreignA": 32, "foreignB": 32, "lvmOK": 64, "lvmNO": 64,
        "nvme01": 32, "ab01": 64,
    }.items():
        path = work / "{}.raw".format(name)
        path.write_bytes(b"\0" * (size * 1024 * 1024))
        disks[name] = path
    keep_before = os.urandom(32 * 1024 * 1024)
    disks["keep01"].write_bytes(keep_before)
    keep_hash = sha256_file(disks["keep01"])
    subprocess.run(
        ["mkfs.ext4", "-F", "-L", "SANCONF", "-q", str(disks["conf01"])],
        check=True, stdout=subprocess.DEVNULL,
    )
    spec = [(name, disks[name]) for name in (
        "conf01", "keep01", "raidA", "raidB", "foreignA", "foreignB",
        "lvmOK", "lvmNO", "nvme01", "ab01",
    )]
    args = [
        "qemu-system-x86_64", "-machine", "pc,accel=kvm:tcg", "-cpu", "max",
        "-m", "1536", "-smp", "2", "-display", "none", "-cdrom", str(ISO),
        "-netdev", "user,id=admin,restrict=on,net=192.168.10.0/24",
        "-device", "virtio-net-pci,netdev=admin,mac=52:54:00:aa:00:01,addr=0x3",
        "-netdev", "user,id=stor,restrict=on,net=10.1.0.0/24",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:aa:00:02,addr=0x4",
        "-serial", "tcp:127.0.0.1:4711,server=on,wait=off",
    ]
    args.extend(iscsi.disk_args(spec))
    report = {"iso_sha256": sha256_file(ISO), "steps": []}
    proc, handle = iscsi.start(args, work / "feature.err")
    try:
        app = iscsi.Serial(4711, work / "feature-serial.log")
        iscsi.login_shell(app)
        app.upload(GUEST / "feature-setup.py", "/tmp/feature-setup.py")
        prepared = app.san_result("sudo -n python3 /tmp/feature-setup.py", 240)
        iscsi.require(prepared.get("cmdline_toram") is False, "toram sur la ligne de commande")
        iscsi.require(prepared["toram"].get("enabled") is False, "toram activé")
        report["steps"].append("preparation: OK")
        start = len(app.buf)
        app.sock.sendall(b"sudo -n systemctl reboot\n")
        app.wait_after("san-live: mode=live", 240, start, poke=True)
        iscsi.login_shell(app, start)
        app.upload(GUEST / "feature-check.py", "/tmp/feature-check.py")
        checked = app.san_result("sudo -n python3 /tmp/feature-check.py", 180)
        iscsi.require(checked.get("medium", {}).get("ok") is True, "support refusé : {}".format(checked.get("medium")))
        names = checked.get("array_names") or []
        dev_md = checked.get("dev_md") or []
        detail = checked.get("md_detail") or ""
        registered = "san0" in names or "san0" in dev_md or "san0" in detail
        foreign = "foreign" in names or "foreign" in dev_md or "foreign" in detail
        iscsi.require(registered, "array enregistré absent : {} {} {}".format(names, dev_md, checked.get("mdstat")))
        iscsi.require(not foreign, "array étranger assemblé : {} {}".format(names, detail))
        lvs = checked.get("lvs") or ""
        active = []
        for line in lvs.splitlines():
            parts = line.split()
            if len(parts) < 3 or len(parts[2]) < 5:
                continue
            if parts[2][4] == "a":
                active.append(parts[0])
        iscsi.require("sanvg" in active, "LV choisi inactif : {}".format(lvs))
        iscsi.require("othervg" not in active, "VG non choisi actif : {}".format(lvs))
        iscsi.require(checked.get("nvme_config") is True, "NVMe-oF absent : {}".format(checked.get("earlier_alerts")))
        iscsi.require(checked.get("nvme_connect") is True, "connexion NVMe refusée : {}".format(checked.get("nvme_err")))
        iscsi.require(checked.get("data_sha256") == prepared["data_sha256"], "partition data modifiée")
        iscsi.require(checked.get("slot_prefix", "").startswith("SAN-SLOT-B"), "slot inactif non écrit")
        iscsi.require(checked.get("ab_active") == "b", "slot actif non basculé")
        joined = "\n".join(checked.get("ssh") or [])
        iscsi.require("192.168.10.10:22" in joined, "SSH n'écoute pas sur l'admin : {}".format(joined))
        iscsi.require("0.0.0.0:22" not in joined and "10.1.0.10:22" not in joined, "SSH hors de l'admin : {}".format(joined))
        iscsi.require(checked.get("ssh_password_rc") not in (0, None), "SSH par mot de passe accepté")
        iscsi.require(checked.get("ssh_root_rc") not in (0, None), "SSH root accepté")
        blob = json.dumps(checked.get("alerts"), ensure_ascii=False)
        iscsi.require("injoignable" in blob, "alerte Ollama absente : {}".format(blob))
        iscsi.require(checked.get("md_unchanged") is True, "Ollama a modifié le RAID")
        iscsi.require(sha256_file(disks["keep01"]) == keep_hash, "disque non sélectionné modifié")
        report["steps"].append("boot-applique: OK")
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        raise
    finally:
        iscsi.stop(proc)
        handle.close()
        (work / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("feature-test: {}".format(exc), file=sys.stderr)
        sys.exit(1)
