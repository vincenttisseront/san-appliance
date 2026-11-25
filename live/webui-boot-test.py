#!/usr/bin/env python3
"""Boot the live ISO and check the WebUI answers on 127.0.0.1:3000 only."""

import hashlib
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
SECRET = re.compile(r"(NUXT_JWT_SECRET|NUXT_ENCRYPTION_KEY|Mot de passe|password=)\s*[:=]?\s*\S+")

REPO = Path(__file__).resolve().parents[1]
ISO = Path(os.environ.get("SAN_ISO", REPO / "dist" / "san-appliance-live-24.04-amd64.iso"))
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
        self.port = port
        self.log_path = Path(log_path)
        self.buf = b""
        self.sock = None
        self.connect(90)

    def connect(self, timeout):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
                self.sock.settimeout(1)
                return
            except OSError as exc:
                last = exc
                time.sleep(0.3)
        raise TestFail("série {} fermée ({})".format(self.port, last))

    def _log(self, chunk):
        text = SECRET.sub(r"\1=[redacted]", chunk.decode("utf-8", "replace"))
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def wait_after(self, token, timeout, start):
        needle = token.encode()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.buf[start:]:
                return self.buf[start:]
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                chunk = b""
            if not chunk:
                time.sleep(0.4)
                try:
                    self.connect(15)
                except TestFail:
                    continue
                continue
            self.buf += chunk
            self._log(chunk)
        raise TestFail("délai en attendant {}".format(token))

    def quiet(self):
        start = len(self.buf)
        self.sock.sendall(b"stty -echo; bind 'set enable-bracketed-paste off'\n")
        self.wait_after("sanadmin@", 60, start)

    def run(self, command, timeout=90):
        marker = "SANMARK" + uuid.uuid4().hex[:10]
        start = len(self.buf)
        line = "{}; printf '%s:%s\\n' '{}' $?\n".format(command, marker)
        self.sock.sendall(line.encode())
        self.wait_after(marker + ":", timeout, start)
        needle = (marker + ":").encode()
        index = self.buf.rfind(needle, start)
        if self.buf.find(b"\n", index) < 0:
            self.wait_after("\n", 15, index)
        text = ANSI.sub("", self.buf[start:].decode("utf-8", "replace")).replace("\r", "")
        match = re.search(re.escape(marker) + r":(\d+)", text)
        if match is None:
            raise TestFail("code de retour illisible pour {}".format(command[:80]))
        return int(match.group(1)), text[:match.start()]


def login_shell(serial, start=0):
    prefix = b"san-otp: user=sanadmin password="
    serial.wait_after(prefix.decode(), 220, start)
    index = serial.buf.rfind(prefix, start)
    serial.wait_after("\n", 30, index + len(prefix))
    text = ANSI.sub("", serial.buf[start:].decode("utf-8", "replace"))
    matches = list(re.finditer(r"san-otp: user=sanadmin password=([A-Za-z0-9]+)", text))
    require(matches, "mot de passe série absent")
    password = matches[-1].group(1)
    if b"login:" not in serial.buf[start:]:
        serial.wait_after("login:", 90, start)
    serial.sock.sendall(b"sanadmin\n")
    serial.wait_after("Password:", 40, len(serial.buf))
    serial.sock.sendall(password.encode() + b"\n")
    serial.wait_after("sanadmin@", 90, len(serial.buf))
    serial.quiet()
    code, out = serial.run("sudo -n true", timeout=30)
    require(code == 0, "sudo -n refusé\n" + out[-500:])


def main():
    require(ISO.is_file(), "ISO absente : {}".format(ISO))
    require(Path("/dev/kvm").exists(), "/dev/kvm absent")
    require(shutil.which(QEMU), "qemu-system-x86_64 absent")
    work = Path("/var/tmp/san-webui-boot")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    disk = work / "disk.raw"
    disk.write_bytes(b"\0" * (32 * 1024 * 1024))
    before = sha256_file(disk)
    err = open(work / "qemu.err", "w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            QEMU,
            "-machine", "pc,accel=kvm:tcg",
            "-cpu", "max",
            "-m", "2048",
            "-smp", "2",
            "-display", "none",
            "-no-reboot",
            "-cdrom", str(ISO),
            "-drive", "file={},format=raw,if=virtio".format(disk),
            "-netdev", "user,id=n0,restrict=on",
            "-device", "virtio-net-pci,netdev=n0",
            "-serial", "tcp:127.0.0.1:4595,server=on,wait=off",
        ],
        stdout=err,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    serial = None
    try:
        serial = Serial(4595, work / "serial.log")
        login_shell(serial)
        code, out = serial.run(
            "for i in $(seq 1 40); do systemctl is-active san-webui.service && break; sleep 2; done; systemctl is-active san-webui.service",
            timeout=120,
        )
        require(code == 0 and "active" in out, "san-webui inactif\n" + out[-800:])
        code, listeners = serial.run("ss -H -ltn 'sport = :3000'", timeout=30)
        require(code == 0, "ss a échoué\n" + listeners[-500:])
        rows = [line for line in listeners.splitlines() if ":3000" in line]
        require(rows, "aucun listener sur le port 3000\n" + listeners[-500:])
        for line in rows:
            fields = line.split()
            require(len(fields) >= 4, "ligne ss illisible: {}".format(line))
            require(fields[3] == "127.0.0.1:3000", "écoute hors loopback: {}".format(line))
        code, status = serial.run(
            "python3 -c \"import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:3000/', timeout=15); print(r.status)\"",
            timeout=40,
        )
        require(code == 0 and ("200" in status or "302" in status), "HTTP loopback en échec\n" + status[-500:])
        code, mode = serial.run("sudo -n stat -c %a /var/lib/san/webui/webui.env", timeout=20)
        require(code == 0 and "640" in mode, "webui.env mode inattendu\n" + mode[-200:])
        code, names = serial.run("sudo -n awk -F= 'NF {print $1}' /var/lib/san/webui/webui.env | sort", timeout=20)
        require(code == 0, "webui.env illisible\n" + names[-200:])
        require("NUXT_JWT_SECRET" in names and "NUXT_ENCRYPTION_KEY" in names, "clés absentes")
        print("webui-boot: 127.0.0.1:3000 answers, listeners={}".format(len(rows)))
        serial.sock.sendall(b"sudo -n poweroff\n")
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        err.close()
    after = sha256_file(disk)
    require(before == after, "le disque jetable a changé")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TestFail as exc:
        print("webui-boot: {}".format(exc), file=sys.stderr)
        sys.exit(1)
