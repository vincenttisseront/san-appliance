#!/usr/bin/env python3
"""Boot the live ISO and exercise the WebUI release update on a disposable disk."""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SECRET = re.compile(r"(password=|NUXT_JWT_SECRET|NUXT_ENCRYPTION_KEY)\S*")

REPO = Path(__file__).resolve().parents[1]
ISO = Path(os.environ.get("SAN_ISO", "/var/tmp/san-release-test.iso"))
GUEST = Path(__file__).resolve().parent / "qemu" / "release-guest.py"
QEMU = "qemu-system-x86_64"


class TestFail(Exception):
    pass


def require(condition, message):
    if not condition:
        raise TestFail(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Serial:
    def __init__(self, port, log_path):
        self.log_path = Path(log_path)
        self.buf = b""
        self.sock = None
        deadline = time.time() + 90
        last = None
        while time.time() < deadline and self.sock is None:
            try:
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=3)
                self.sock.settimeout(1)
            except OSError as exc:
                last = exc
                time.sleep(0.3)
        if self.sock is None:
            raise TestFail("série fermée ({})".format(last))

    def _log(self, chunk):
        text = SECRET.sub(r"\1[redacted]", chunk.decode("utf-8", "replace"))
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def wait_after(self, token, timeout, start):
        needle = token.encode()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.buf[start:]:
                return
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                time.sleep(0.3)
                continue
            self.buf += chunk
            self._log(chunk)
        raise TestFail("délai en attendant {}".format(token))

    def run(self, command, timeout=90):
        marker = "SANMARK" + uuid.uuid4().hex[:10]
        start = len(self.buf)
        self.sock.sendall("{}; printf '%s:%s\\n' '{}' $?\n".format(command, marker).encode())
        self.wait_after(marker + ":", timeout, start)
        index = self.buf.rfind((marker + ":").encode(), start)
        if self.buf.find(b"\n", index) < 0:
            self.wait_after("\n", 15, index)
        text = ANSI.sub("", self.buf[start:].decode("utf-8", "replace")).replace("\r", "")
        match = re.search(re.escape(marker) + r":(\d+)", text)
        if match is None:
            raise TestFail("code illisible")
        return int(match.group(1)), text[:match.start()]


def login(serial):
    prefix = b"san-otp: user=sanadmin password="
    serial.wait_after(prefix.decode(), 220, 0)
    index = serial.buf.rfind(prefix)
    serial.wait_after("\n", 30, index + len(prefix))
    text = ANSI.sub("", serial.buf.decode("utf-8", "replace"))
    matches = list(re.finditer(r"san-otp: user=sanadmin password=([A-Za-z0-9]+)", text))
    require(matches, "mot de passe série absent")
    if b"login:" not in serial.buf:
        serial.wait_after("login:", 90, 0)
    serial.sock.sendall(b"sanadmin\n")
    serial.wait_after("Password:", 40, len(serial.buf))
    serial.sock.sendall(matches[-1].group(1).encode() + b"\n")
    serial.wait_after("sanadmin@", 90, len(serial.buf))
    serial.sock.sendall(b"stty -echo; bind 'set enable-bracketed-paste off'\n")
    serial.wait_after("sanadmin@", 40, len(serial.buf))
    code, out = serial.run("sudo -n true", timeout=30)
    require(code == 0, "sudo refusé\n" + out[-400:])


def upload(serial, local, remote):
    payload = Path(local).read_bytes()
    encoded = __import__("base64").b64encode(payload).decode()
    code, _out = serial.run("rm -f {}.b64".format(remote), timeout=20)
    require(code == 0, "rm")
    for offset in range(0, len(encoded), 96):
        chunk = encoded[offset:offset + 96]
        code, _out = serial.run("printf '%s' '{}' >> {}.b64".format(chunk, remote), timeout=20)
        require(code == 0, "upload")
    code, _out = serial.run("base64 -d {}.b64 > {}".format(remote, remote), timeout=20)
    require(code == 0, "decode")


def main():
    require(ISO.is_file(), "ISO absente: {}".format(ISO))
    require(GUEST.is_file(), "script invité absent")
    require(Path("/dev/kvm").exists(), "kvm absent")
    work = Path("/var/tmp/san-release-boot")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    disk = work / "disk.raw"
    disk.write_bytes(b"\0" * (64 * 1024 * 1024))
    iso_sha = sha256_file(ISO)
    err = open(work / "qemu.err", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            QEMU, "-machine", "pc,accel=kvm:tcg", "-cpu", "max", "-m", "2048", "-smp", "2",
            "-display", "none", "-no-reboot", "-cdrom", str(ISO),
            "-drive", "file={},format=raw,if=virtio".format(disk),
            "-netdev", "user,id=n0,restrict=on",
            "-device", "virtio-net-pci,netdev=n0",
            "-serial", "tcp:127.0.0.1:4596,server=on,wait=off",
        ],
        stdout=err, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        serial = Serial(4596, work / "serial.log")
        login(serial)
        code, out = serial.run(
            "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do systemctl is-active san-webui.service san-release.service && break; sleep 2; done; systemctl is-active san-webui.service san-release.service",
            timeout=90,
        )
        require(code == 0 and out.count("active") >= 2, "services inactifs\n" + out[-500:])
        upload(serial, GUEST, "/tmp/release-guest.py")
        code, out = serial.run("sudo -n python3 /tmp/release-guest.py", timeout=180)
        require(code == 0 and "SAN-RESULT " in out, "essai invité en échec\n" + out[-1500:])
        line = [item for item in out.splitlines() if "SAN-RESULT " in item][-1]
        result = json.loads(line.split("SAN-RESULT ", 1)[1])
        require(result.get("bad_checksum") is True, result)
        require(result.get("rollback") is True, result)
        require(result.get("applied_slot") == "b", result)
        print("release-boot: iso_sha256={} result={}".format(iso_sha, json.dumps(result)))
        serial.sock.sendall(b"sudo -n poweroff\n")
        try:
            proc.wait(timeout=40)
        except subprocess.TimeoutExpired:
            proc.terminate()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        err.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TestFail as exc:
        print("release-boot: {}".format(exc), file=sys.stderr)
        sys.exit(1)
