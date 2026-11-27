#!/usr/bin/env python3
"""Démarre l'ISO de l'appliance et relance les huit chemins dans cette image."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "iscsi_test", Path(__file__).resolve().parent / "iscsi-test.py",
)
iscsi_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(iscsi_test)


def prepare(work):
    specs = {
        "conf01": 600,
        "vgA": 2200,
        "vgB": 300,
        "memA": 64,
        "memB": 64,
        "spare": 64,
        "occupied": 32,
        "boot01": 32,
        "exported": 32,
    }
    disks = []
    for name, size in specs.items():
        path = work / "{}.raw".format(name)
        subprocess.run(["truncate", "-s", "{}M".format(size), str(path)], check=True)
        if name in {"vgA", "vgB"}:
            fd = os.open(path, os.O_WRONLY)
            try:
                os.write(fd, b"\xa5" * (1024 * 1024))
            finally:
                os.close(fd)
        disks.append((name, path))
    return disks


def main():
    require = iscsi_test.require
    ISO = iscsi_test.ISO
    require(ISO.is_file(), "ISO absente : {}".format(ISO))
    require(Path("/dev/kvm").exists(), "/dev/kvm absent")
    work = Path("/var/tmp/san-gap-iso")
    if work.exists():
        subprocess.run(["rm", "-rf", str(work)], check=True)
    work.mkdir(parents=True)
    disks = prepare(work)
    repo = Path(__file__).resolve().parents[1]
    args = iscsi_test.qemu_base(1536, 2)
    args.append("-no-reboot")
    args.extend([
        "-netdev", "user,id=admin,restrict=on,net=192.168.10.0/24",
        "-device", "virtio-net-pci,netdev=admin,mac=52:54:00:aa:00:01,addr=0x3",
        "-netdev", "user,id=stor,restrict=on,net=10.0.0.0/24",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:aa:00:02,addr=0x4",
        "-serial", "tcp:127.0.0.1:4731,server=on,wait=off",
    ])
    args.extend(iscsi_test.disk_args(disks))
    args.extend([
        "-fsdev", "local,id=repo,path={},security_model=none".format(repo),
        "-device", "virtio-9p-pci,fsdev=repo,mount_tag=repo,addr=0x1f",
    ])
    proc, handle = iscsi_test.start(args, work / "qemu.err")
    serial = None
    try:
        serial = iscsi_test.Serial(4731, work / "serial.log")
        iscsi_test.login_shell(serial)
        code, out = serial.run(
            "sudo -n mkdir -p /mnt/repo && sudo -n mount -t 9p "
            "-o trans=virtio,version=9p2000.L,msize=104857600 repo /mnt/repo",
            timeout=40,
        )
        if code != 0:
            serial.upload(repo / "live" / "qemu" / "gap-guest.py", "/tmp/gap-guest.py")
            command = "sudo -n python3 /tmp/gap-guest.py"
        else:
            command = "sudo -n python3 /mnt/repo/live/qemu/gap-guest.py"
        payload = serial.san_result(command, timeout=900)
        print(__import__("json").dumps(payload, indent=2, ensure_ascii=False))
    except iscsi_test.TestFail as exc:
        tail = ""
        log = work / "serial.log"
        if log.is_file():
            tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise SystemExit("{}\n{}".format(exc, tail))
    finally:
        if serial is not None:
            iscsi_test.poweroff(serial, proc)
        else:
            iscsi_test.stop(proc)
        handle.close()


if __name__ == "__main__":
    main()
