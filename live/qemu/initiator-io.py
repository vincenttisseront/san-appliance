#!/usr/bin/python3
"""open-iscsi client for the disposable LIO test. Modes: deny, write, read."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time

TARGET = "iqn.2026-11.local.san:disk"
PORTAL = "10.1.0.10"
ALLOWED = "iqn.2026-11.local.client:host"
DENIED = "iqn.2026-11.local.client:denied"
CHAP_USER = "client1"
SECRET = "Init1SecretOK1"
NIC_MAC = "52:54:00:bb:00:02"
PATTERN = 4 * 1024 * 1024


def run(args, check=True):
    proc = subprocess.run(args, text=True, capture_output=True)
    if check and proc.returncode != 0:
        sys.stderr.write(proc.stderr or proc.stdout or "")
        raise SystemExit("commande en échec : {}".format(" ".join(args[:4])))
    return proc


def result(payload, code=0):
    print("SAN-RESULT " + json.dumps(payload, ensure_ascii=False))
    raise SystemExit(code)


def iface():
    want = NIC_MAC.lower()
    for entry in pathlib.Path("/sys/class/net").iterdir():
        if entry.name == "lo":
            continue
        current = (entry / "address").read_text(encoding="utf-8").strip().lower()
        if current == want:
            return entry.name
    raise SystemExit("interface initiateur absente")


def prepare_net():
    name = iface()
    run(["ip", "link", "set", name, "up"])
    run(["ip", "addr", "replace", "10.1.0.20/24", "dev", name])
    return name


def set_name(iqn):
    path = pathlib.Path("/etc/iscsi/initiatorname.iscsi")
    path.write_text("InitiatorName={}\n".format(iqn), encoding="utf-8")
    run(["systemctl", "restart", "iscsid"])
    time.sleep(1)


def node():
    run([
        "iscsiadm", "-m", "node", "-o", "new",
        "-T", TARGET, "-p", PORTAL,
    ], check=False)
    base = ["iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--op", "update"]
    run(base + ["-n", "node.startup", "-v", "manual"])
    run(base + ["-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(base + ["-n", "node.session.auth.username", "-v", CHAP_USER])
    run(base + ["-n", "node.session.auth.password", "-v", SECRET])


def login():
    return run(
        ["timeout", "15", "iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--login"],
        check=False,
    )


def logout():
    run(["iscsiadm", "-m", "node", "-T", TARGET, "-p", PORTAL, "--logout"], check=False)


def sessions():
    proc = run(["iscsiadm", "-m", "session"], check=False)
    return proc.stdout.strip()


def lun_paths():
    root = pathlib.Path("/dev/disk/by-path")
    if not root.is_dir():
        return []
    found = []
    for entry in sorted(root.iterdir()):
        name = entry.name
        if "iscsi-{}".format(TARGET) in name and name.endswith("-lun-0"):
            found.append(name)
    return found


def wait_lun():
    deadline = time.time() + 20
    while time.time() < deadline:
        found = lun_paths()
        if found:
            return found
        time.sleep(1)
    return []


def digest_of(path):
    proc = run([
        "dd", "if=" + path, "of=/tmp/san-readback",
        "bs=1M", "count=4", "iflag=direct", "status=none",
    ])
    data = pathlib.Path("/tmp/san-readback").read_bytes()
    if len(data) != PATTERN:
        raise SystemExit("lecture LUN trop courte : {}".format(len(data)))
    return hashlib.sha256(data).hexdigest()


def identity():
    paths = wait_lun()
    targets = []
    root = pathlib.Path("/sys/class/iscsi_session")
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            name = entry / "targetname"
            if name.is_file():
                targets.append(name.read_text(encoding="utf-8").strip())
    return {"paths": paths, "targets": targets, "session": sessions()}


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    prepare_net()
    if mode == "deny":
        logout()
        set_name(DENIED)
        node()
        denied = login()
        payload = {
            "mode": "deny",
            "login_rc": denied.returncode,
            "session": sessions(),
            "stderr": (denied.stderr or "").splitlines()[-4:],
        }
        if denied.returncode == 0 or sessions():
            result(payload, 1)
        result(payload)
    if mode == "write":
        logout()
        set_name(ALLOWED)
        node()
        allowed = login()
        if allowed.returncode != 0:
            result({
                "mode": "write",
                "login_rc": allowed.returncode,
                "stderr": (allowed.stderr or "").splitlines()[-6:],
            }, 1)
        ident = identity()
        if not ident["paths"]:
            result({"mode": "write", "error": "LUN absente", "identity": ident}, 1)
        dev = "/dev/disk/by-path/" + ident["paths"][0]
        pattern = os.urandom(PATTERN)
        pathlib.Path("/tmp/san-pattern").write_bytes(pattern)
        digest = hashlib.sha256(pattern).hexdigest()
        run([
            "dd", "if=/tmp/san-pattern", "of=" + dev,
            "bs=1M", "oflag=direct", "conv=fsync", "status=none",
        ])
        read_back = digest_of(dev)
        if read_back != digest:
            result({"mode": "write", "error": "relecture différente", "identity": ident}, 1)
        result({"mode": "write", "sha256": digest, "identity": ident})
    if mode == "down":
        set_name(ALLOWED)
        node()
        denied = login()
        payload = {
            "mode": "down",
            "login_rc": denied.returncode,
            "session": sessions(),
        }
        if denied.returncode == 0 or sessions():
            result(payload, 1)
        result(payload)
    if mode == "read":
        logout()
        set_name(ALLOWED)
        node()
        deadline = time.time() + 180
        allowed = None
        while time.time() < deadline:
            allowed = login()
            if allowed.returncode == 0:
                break
            time.sleep(3)
        if allowed is None or allowed.returncode != 0:
            result({
                "mode": "read",
                "login_rc": None if allowed is None else allowed.returncode,
                "stderr": [] if allowed is None else (allowed.stderr or "").splitlines()[-6:],
            }, 1)
        ident = identity()
        if not ident["paths"]:
            result({"mode": "read", "error": "LUN absente", "identity": ident}, 1)
        dev = "/dev/disk/by-path/" + ident["paths"][0]
        result({"mode": "read", "sha256": digest_of(dev), "identity": ident})
    raise SystemExit("mode inconnu")


if __name__ == "__main__":
    main()
