#!/usr/bin/python3
"""Boot the live ISO under QEMU and exercise LIO with a second open-iscsi VM."""

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

REPO = Path(__file__).resolve().parents[1]
ISO = Path(os.environ.get("SAN_ISO", REPO / "dist" / "san-appliance-live-24.04-amd64.iso"))
QEMU = "qemu-system-x86_64"
GUEST = Path(__file__).resolve().parent / "qemu"


class TestFail(Exception):
    pass


def sha256_file(path, limit=None):
    digest = hashlib.sha256()
    remaining = limit
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024 if remaining is None else min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            if remaining is not None:
                remaining -= len(chunk)
                if remaining <= 0:
                    break
    return digest.hexdigest()


def require(condition, message):
    if not condition:
        raise TestFail(message)


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
        with self.log_path.open("ab") as handle:
            handle.write(chunk)

    def wait_after(self, token, timeout, start, poke=False):
        needle = token.encode()
        deadline = time.time() + timeout
        next_poke = time.time() + 20
        while time.time() < deadline:
            if needle in self.buf[start:]:
                return self.buf[start:]
            if poke and time.time() >= next_poke:
                try:
                    self.sock.sendall(b"\n")
                except OSError:
                    pass
                next_poke = time.time() + 20
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

    def upload(self, local, remote):
        payload = Path(local).read_bytes()
        encoded = __import__("base64").b64encode(payload).decode()
        code, _ = self.run("rm -f {}.b64".format(remote), timeout=30)
        require(code == 0, "rm {}".format(remote))
        for offset in range(0, len(encoded), 96):
            chunk = encoded[offset:offset + 96]
            code, _ = self.run("printf '%s' '{}' >> {}.b64".format(chunk, remote), timeout=30)
            require(code == 0, "upload interrompu vers {}".format(remote))
        code, _ = self.run("base64 -d {}.b64 > {}".format(remote, remote), timeout=30)
        require(code == 0, "base64 -d {}".format(remote))

    def san_result(self, command, timeout):
        code, text = self.run(command, timeout=timeout)
        line = ""
        for item in text.splitlines():
            if "SAN-RESULT " in item:
                line = item[item.index("SAN-RESULT ") + len("SAN-RESULT "):]
        require(line, "SAN-RESULT absent\n" + text[-1500:])
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise TestFail("SAN-RESULT illisible : {}".format(exc)) from exc
        require(code == 0, "commande en échec {} : {}".format(code, json.dumps(payload, ensure_ascii=False)[:800]))
        return payload


def qemu_base(memory, smp):
    return [
        QEMU,
        "-machine", "pc,accel=kvm:tcg",
        "-cpu", "max",
        "-m", str(memory),
        "-smp", str(smp),
        "-display", "none",
        "-cdrom", str(ISO),
    ]


def disk_args(spec):
    args = []
    addr = 0x5
    for name, path in spec:
        args.extend([
            "-drive", "file={},if=none,id={},format=raw,cache=writethrough".format(path, name),
            "-device", "virtio-blk-pci,drive={},serial={},addr={:#x}".format(name, name, addr),
        ])
        addr += 1
    return args


def start(args, err_path):
    handle = open(err_path, "w", encoding="utf-8")
    proc = subprocess.Popen(args, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    return proc, handle


def stop(proc):
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def poweroff(serial, proc):
    try:
        serial.sock.sendall(b"sudo -n poweroff\n")
    except OSError:
        pass
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        stop(proc)


def prepare_disks(work):
    export = work / "export.raw"
    other = work / "other.raw"
    conf = work / "conf.raw"
    export.write_bytes(b"\0" * (64 * 1024 * 1024))
    blob = os.urandom(32 * 1024 * 1024)
    other.write_bytes(blob)
    conf.write_bytes(b"\0" * (32 * 1024 * 1024))
    subprocess.run(
        ["mkfs.ext4", "-F", "-L", "SANCONF", "-q", str(conf)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return export, other, conf


def boot_appliance(work, export, other, conf, include_export, rebootable):
    disks = [("other01", other), ("conf01", conf)]
    if include_export:
        disks.insert(0, ("export01", export))
    args = qemu_base(1280, 2)
    if not rebootable:
        args.append("-no-reboot")
    # NICs stay on the same PCI slots when a disk is removed.
    args.extend([
        "-netdev", "user,id=admin,restrict=on,net=192.168.10.0/24",
        "-device", "virtio-net-pci,netdev=admin,mac=52:54:00:aa:00:01,addr=0x3",
        "-netdev", "socket,id=stor,listen=127.0.0.1:4590",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:aa:00:02,addr=0x4",
        "-serial", "tcp:127.0.0.1:4591,server=on,wait=off",
    ])
    args.extend(disk_args(disks))
    return start(args, work / "appliance.err")


def boot_initiator(work):
    args = qemu_base(1024, 1)
    args.append("-no-reboot")
    args.extend([
        "-netdev", "socket,id=stor,connect=127.0.0.1:4590",
        "-device", "virtio-net-pci,netdev=stor,mac=52:54:00:bb:00:02",
        "-serial", "tcp:127.0.0.1:4592,server=on,wait=off",
    ])
    return start(args, work / "initiator.err")


def login_shell(serial, start=0):
    prefix = b"san-otp: user=sanadmin password="
    serial.wait_after(prefix.decode(), 220, start)
    index = serial.buf.rfind(prefix, start)
    # The password is written after this prefix. Wait for the end of the line
    # so a split TCP segment is not read as an empty password.
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
    iso_sha = sha256_file(ISO)
    work = Path("/var/tmp/san-iscsi-test")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    export, other, conf = prepare_disks(work)
    other_before = sha256_file(other)
    export_before = sha256_file(export)
    report = {
        "iso": str(ISO),
        "iso_sha256": iso_sha,
        "secure_boot": "unsupported",
        "steps": [],
    }
    procs = []
    handles = []
    try:
        if os.environ.get("SAN_SKIP_SHORT") == "1":
            report["steps"].append("boot-court: SKIP")
        else:
            proc, handle = boot_appliance(work, export, other, conf, True, False)
            procs.append(proc)
            handles.append(handle)
            app = Serial(4591, work / "short-serial.log")
            login_shell(app)
            marker = app.log_path.read_text(encoding="utf-8", errors="replace")
            require("san-live: mode=live" in marker, "marqueur live absent")
            require("export-module=absent" in marker, "target_core_mod déjà chargé au boot court")
            code, text = app.run("sudo -n ss -H -ltn sport = :3260", timeout=30)
            require(code == 0, "ss a échoué")
            visible = "\n".join(line for line in text.splitlines() if line.strip())
            require(visible == "", "3260 écoute au boot court : {}".format(visible))
            poweroff(app, proc)
            procs.remove(proc)
            require(sha256_file(other) == other_before, "disque non sélectionné modifié au boot court")
            require(sha256_file(export) == export_before, "disque d'export modifié au boot court")
            report["steps"].append("boot-court: OK")

        proc, handle = boot_appliance(work, export, other, conf, True, True)
        procs.append(proc)
        handles.append(handle)
        app = Serial(4591, work / "appliance-serial.log")
        login_shell(app)
        init_proc, init_handle = boot_initiator(work)
        procs.append(init_proc)
        handles.append(init_handle)
        init = Serial(4592, work / "initiator-serial.log")
        login_shell(init)
        app.upload(GUEST / "appliance-setup.py", "/tmp/appliance-setup.py")
        init.upload(GUEST / "initiator-io.py", "/tmp/initiator-io.py")
        configured = app.san_result("sudo -n python3 /tmp/appliance-setup.py", 180)
        report["configured"] = {
            "conf": configured.get("conf"),
            "export": configured.get("export"),
            "other": configured.get("other"),
            "listen": configured.get("listen"),
            "exported": (configured.get("restored") or {}).get("exported"),
        }
        denied = init.san_result("sudo -n python3 /tmp/initiator-io.py deny", 90)
        require(denied.get("login_rc") not in (0, None), "initiateur refusé a ouvert une session")
        require(not denied.get("session"), "session iSCSI après refus : {}".format(denied.get("session")))
        report["steps"].append("initiateur-refuse: OK")

        written = init.san_result("sudo -n python3 /tmp/initiator-io.py write", 120)
        prefix = sha256_file(export, 4 * 1024 * 1024)
        require(prefix == written["sha256"], "empreinte hôte {} != invité {}".format(prefix, written["sha256"]))
        before_id = written["identity"]
        report["steps"].append("ecriture: OK {}".format(written["sha256"]))

        start = len(app.buf)
        try:
            app.sock.sendall(b"sudo -n systemctl reboot\n")
        except OSError as exc:
            raise TestFail("reboot non envoyé : {}".format(exc)) from exc
        # The shell prints a prompt before the reboot actually happens.
        app.wait_after("san-live: mode=live", 220, start, poke=True)
        login_shell(app, start)
        read_back = init.san_result("sudo -n python3 /tmp/initiator-io.py read", 200)
        require(read_back["sha256"] == written["sha256"], "données différentes après reboot")
        require(read_back["identity"]["paths"] == before_id["paths"], "chemin de LUN différent après reboot")
        require(read_back["identity"]["targets"] == before_id["targets"], "cible iSCSI différente après reboot")
        require(sha256_file(export, 4 * 1024 * 1024) == written["sha256"], "disque jetable différent après reboot")
        report["steps"].append("reboot: OK LUN {} {}".format(before_id["paths"], written["sha256"]))
        require(sha256_file(other) == other_before, "disque non sélectionné modifié pendant l'export")

        poweroff(init, init_proc)
        procs.remove(init_proc)
        poweroff(app, proc)
        procs.remove(proc)

        proc, handle = boot_appliance(work, export, other, conf, False, False)
        procs.append(proc)
        handles.append(handle)
        app = Serial(4591, work / "missing-serial.log")
        login_shell(app)
        app.upload(GUEST / "appliance-check.py", "/tmp/appliance-check.py")
        checked = app.san_result("sudo -n python3 /tmp/appliance-check.py", 90)
        require(not checked.get("listen"), "3260 écoute sans le disque : {}".format(checked.get("listen")))
        messages = " ".join(item.get("message", "") for item in checked.get("alerts") or [])
        require("volume absent" in messages, "alerte volume absent manquante : {} {}".format(messages, checked.get("unit")))
        report["steps"].append("disque-absent: OK")
        poweroff(app, proc)
        procs.remove(proc)
        require(sha256_file(other) == other_before, "disque non sélectionné modifié au boot sans export")
        require(sha256_file(export, 4 * 1024 * 1024) == written["sha256"], "disque d'export modifié alors qu'il était débranché")
        report["steps"].append("disque-non-selectionne: OK {}".format(other_before))
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
        raise
    finally:
        for proc in procs:
            stop(proc)
        for handle in handles:
            handle.close()
        (work / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except TestFail as exc:
        print("iscsi-test: {}".format(exc), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print("iscsi-test: {}".format(exc), file=sys.stderr)
        sys.exit(1)
