#!/usr/bin/python3
"""CD et USB virtuels, SeaBIOS et OVMF. Secure Boot est essayé pour de vrai."""

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ISO = Path(os.environ.get("SAN_ISO", REPO / "dist" / "san-appliance-live-24.04-amd64.iso"))
CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")
SEC_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.secboot.fd")
SEC_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.ms.fd")


def wait_marker(port, timeout):
    deadline = time.time() + timeout
    buf = b""
    sock = None
    while time.time() < deadline and sock is None:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=2)
            sock.settimeout(1)
        except OSError:
            time.sleep(0.3)
    if sock is None:
        return False, "série fermée"
    while time.time() < deadline:
        if b"san-live: mode=live" in buf:
            sock.close()
            return True, "san-live"
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            time.sleep(0.3)
            continue
        buf += chunk
    sock.close()
    return False, "marqueur absent"


def run_case(name, extra, port, timeout):
    work = Path("/var/tmp/san-boot-matrix") / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    args = [
        "qemu-system-x86_64", "-machine", "pc,accel=kvm:tcg", "-cpu", "max",
        "-m", "1536", "-smp", "1", "-display", "none", "-no-reboot",
        "-serial", "tcp:127.0.0.1:{},server=on,wait=off".format(port),
    ]
    args.extend(extra)
    handle = open(work / "qemu.err", "w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        ok, detail = wait_marker(port, timeout)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        handle.close()
    return {"name": name, "reached_userspace": ok, "detail": detail}


def firmware(code, vars_src, dest):
    shutil.copy(vars_src, dest)
    return [
        "-drive", "if=pflash,format=raw,readonly=on,file={}".format(code),
        "-drive", "if=pflash,format=raw,file={}".format(dest),
    ]


def main():
    report = {"iso": str(ISO), "cases": [], "secure_boot": "not-run"}
    if not ISO.is_file():
        raise SystemExit("ISO absente")
    if not Path("/dev/kvm").exists():
        raise SystemExit("/dev/kvm absent")
    usb = [
        "-drive", "if=none,id=usb,format=raw,file={},readonly=on".format(ISO),
        "-device", "qemu-xhci,id=xhci",
        "-device", "usb-storage,bus=xhci.0,drive=usb",
    ]
    cd = ["-cdrom", str(ISO)]
    work = Path("/var/tmp/san-boot-matrix")
    work.mkdir(parents=True, exist_ok=True)
    cases = [
        ("cd-seabios", cd, 4721),
        ("usb-seabios", usb, 4722),
    ]
    if CODE.is_file() and VARS.is_file():
        cases.append(("cd-ovmf", firmware(CODE, VARS, work / "ovmf-cd.vars") + cd, 4723))
        cases.append(("usb-ovmf", firmware(CODE, VARS, work / "ovmf-usb.vars") + usb, 4724))
    else:
        report["ovmf"] = "firmware absente, OVMF non exécuté"
    for name, extra, port in cases:
        report["cases"].append(run_case(name, extra, port, 180))
    if SEC_CODE.is_file() and SEC_VARS.is_file():
        sec = run_case(
            "secure-boot",
            firmware(SEC_CODE, SEC_VARS, work / "secboot.vars") + cd,
            4725,
            90,
        )
        report["cases"].append(sec)
        if sec["reached_userspace"]:
            report["secure_boot"] = "booted-unexpectedly"
        else:
            report["secure_boot"] = "unsupported"
    else:
        report["secure_boot"] = "not-run"
        report["secure_boot_reason"] = "firmware Secure Boot absente"
    failed = [item["name"] for item in report["cases"] if item["name"] != "secure-boot" and not item["reached_userspace"]]
    report["ok"] = not failed and report["secure_boot"] == "unsupported"
    report["failed"] = failed
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if not report["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
