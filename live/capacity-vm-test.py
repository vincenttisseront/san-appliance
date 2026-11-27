#!/usr/bin/env python3
"""Démarre une VM Ubuntu et lit le rapport de mesure."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORK = Path("/var/tmp/san-capacity")
REPORT = REPO / "live" / "work" / "capacity-report.json"
IMAGE = WORK / "noble.img"


def run(args, check=True):
    proc = subprocess.run(args, text=True, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.returncode


def main():
    if os.geteuid() != 0:
        raise SystemExit("capacity-vm-test: lancer en root")
    if not Path("/dev/kvm").exists():
        raise SystemExit("capacity-vm-test: /dev/kvm absent")
    if not IMAGE.is_file():
        raise SystemExit("capacity-vm-test: image cloud absente ({})".format(IMAGE))
    if not shutil.which("qemu-system-x86_64") or not shutil.which("cloud-localds"):
        raise SystemExit("capacity-vm-test: qemu ou cloud-localds absent")
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    if REPORT.exists():
        REPORT.unlink()
    disk = WORK / "disk.qcow2"
    if disk.exists():
        disk.unlink()
    seed = WORK / "seed.iso"
    user = WORK / "user-data"
    meta = WORK / "meta-data"
    user.write_text(
        "\n".join([
            "#cloud-config",
            "package_update: true",
            "package_upgrade: false",
            "packages:",
            "  - lvm2",
            "  - cryptsetup",
            "  - thin-provisioning-tools",
            "  - targetcli-fb",
            "  - python3-rtslib-fb",
            "  - open-iscsi",
            "  - sg3-utils",
            "  - fio",
            "  - iproute2",
            "runcmd:",
            "  - mkdir -p /repo /usr/share/san /var/lib/san/conf /etc/san",
            "  - mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600 repo /repo",
            "  - bash -c 'python3 /repo/live/qemu/capacity-guest.py > /repo/live/work/capacity-guest.out 2>&1; echo done > /repo/live/work/capacity-guest.done; poweroff'",
            "",
        ]),
        encoding="utf-8",
    )
    meta.write_text("instance-id: san-capacity\nlocal-hostname: san-capacity\n", encoding="utf-8")
    run(["cloud-localds", str(seed), str(user), str(meta)])
    run(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(IMAGE), str(disk), "8G"])
    for serial, size in (("conf01", 64), ("vgA", 1536), ("vgB", 256)):
        path = WORK / "{}.raw".format(serial)
        if path.exists():
            path.unlink()
        run(["truncate", "-s", "{}M".format(size), str(path)])
    serial_log = WORK / "serial.log"
    cmd = [
        "qemu-system-x86_64",
        "-machine", "pc,accel=kvm",
        "-cpu", "host",
        "-m", "2048",
        "-smp", "2",
        "-display", "none",
        "-no-reboot",
        "-serial", "file:{}".format(serial_log),
        "-drive", "file={},if=none,id=root,format=qcow2".format(disk),
        "-device", "ide-hd,drive=root,bootindex=0",
        "-drive", "file={},if=none,id=seed,format=raw".format(seed),
        "-device", "virtio-blk-pci,drive=seed,serial=cidata",
        "-virtfs", "local,path={},mount_tag=repo,security_model=none,id=repo".format(REPO),
        "-netdev", "user,id=n0",
        "-device", "virtio-net-pci,netdev=n0",
    ]
    for serial in ("conf01", "vgA", "vgB"):
        cmd.extend([
            "-drive", "file={},if=none,id={},format=raw".format(WORK / "{}.raw".format(serial), serial),
            "-device", "virtio-blk-pci,drive={},serial={}".format(serial, serial),
        ])
    proc = subprocess.Popen(cmd)
    deadline = time.time() + 1800
    while time.time() < deadline:
        if REPORT.is_file() and (REPO / "live" / "work" / "capacity-guest.done").is_file():
            break
        if proc.poll() is not None and REPORT.is_file():
            break
        if proc.poll() is not None and not REPORT.is_file():
            tail = ""
            if serial_log.is_file():
                tail = serial_log.read_text(encoding="utf-8", errors="replace")[-2000:]
            raise SystemExit("la VM s'est arrêtée sans rapport\n{}".format(tail))
        time.sleep(5)
    else:
        proc.terminate()
        raise SystemExit("délai dépassé, rapport absent")
    if proc.poll() is None:
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.terminate()
    print(REPORT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
