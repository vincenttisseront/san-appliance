#!/usr/bin/python3
"""Mesure ALUA, UNMAP et le gel sur un fichier. Pas de disque physique."""

import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, "/mnt/payload/tui")

from san_tui.fabric import ConfigfsGate
from san_tui.iscsi import RtslibBackend, plan_export

IQN = "iqn.2026-11.local.san:fabric"
INITIATOR = "iqn.2026-11.local.san:init"
SECRET = "chap-secret-ok"
USER = "client1"
PORTAL = "127.0.0.1"
STANDBY = "127.0.0.2"
IMAGE = pathlib.Path("/var/tmp/san-fabric.img")
SEEK_HOLE = os.SEEK_HOLE


def run(args, check=True):
    proc = subprocess.run(args, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError("{}: {}".format(" ".join(args[:4]), (proc.stderr or proc.stdout)[-400:]))
    return proc


class Probe:
    def __init__(self, volume):
        self.volume = volume

    def get_volume(self, stable_id):
        if stable_id == self.volume["id"] or stable_id.endswith(self.volume["id"].split("/")[-1]):
            return self.volume
        return None

    def list_interfaces(self):
        return [{"name": "lo", "addresses": ["{}/8".format(PORTAL), "{}/8".format(STANDBY)]}]


def _alua_config():
    root = pathlib.Path("/sys/kernel/config/target/core")
    found = {}
    if not root.is_dir():
        return found
    for path in root.glob("*/*/alua/*/*"):
        if path.name not in {"alua_access_state", "preferred"} or not path.is_file():
            continue
        try:
            found[str(path.relative_to(root))] = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
    return found


def hole_at(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        end = os.lseek(fd, 0, os.SEEK_END)
        hole = os.lseek(fd, 0, SEEK_HOLE)
        return {"end": end, "hole": hole, "punched": hole < end}
    finally:
        os.close(fd)


def login():
    pathlib.Path("/etc/iscsi/initiatorname.iscsi").write_text(
        "InitiatorName={}\n".format(INITIATOR), encoding="utf-8",
    )
    run(["systemctl", "restart", "iscsid"])
    time.sleep(1)
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", PORTAL, "--logout"], check=False)
    run(["iscsiadm", "-m", "node", "-o", "new", "-T", IQN, "-p", PORTAL], check=False)
    base = ["iscsiadm", "-m", "node", "-T", IQN, "-p", PORTAL, "--op", "update"]
    run(base + ["-n", "node.startup", "-v", "manual"])
    run(base + ["-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(base + ["-n", "node.session.auth.username", "-v", USER])
    run(base + ["-n", "node.session.auth.password", "-v", SECRET])
    last = None
    deadline = time.time() + 30
    while time.time() < deadline:
        last = run(["iscsiadm", "-m", "node", "-T", IQN, "-p", PORTAL, "--login"], check=False)
        err = (last.stderr or "") + (last.stdout or "")
        if last.returncode == 0 or "already present" in err:
            return
        time.sleep(1)
    raise RuntimeError("login refusé: {}".format((last.stderr if last else "")[-300:]))


def device():
    root = pathlib.Path("/dev/disk/by-path")
    deadline = time.time() + 20
    while time.time() < deadline:
        if root.is_dir():
            for entry in sorted(root.iterdir()):
                if "iscsi-{}".format(IQN) in entry.name and entry.name.endswith("-lun-0"):
                    return str(entry)
        time.sleep(0.5)
    raise RuntimeError("LUN absente")


def cleanup(loop):
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", PORTAL, "--logout"], check=False)
    run(["iscsiadm", "-m", "node", "-o", "delete", "-T", IQN, "-p", PORTAL], check=False)
    try:
        from rtslib_fb import RTSRoot
        for target in list(RTSRoot().targets):
            if IQN in target.wwn:
                target.delete()
        for storage in list(RTSRoot().storage_objects):
            storage.delete()
    except Exception:
        pass
    if loop:
        run(["losetup", "-d", loop], check=False)


def main():
    report = {"alua": False, "unmap": False, "freeze": False}
    loop = ""
    try:
        run(["systemctl", "stop", "rtslib-fb-targetctl.service", "target.service"], check=False)
        run(["modprobe", "configfs"])
        run(["modprobe", "target_core_mod"])
        run(["modprobe", "iscsi_target_mod"])
        if not os.path.ismount("/sys/kernel/config"):
            run(["mount", "-t", "configfs", "none", "/sys/kernel/config"])
        run(["ip", "addr", "replace", "{}/8".format(STANDBY), "dev", "lo"])
        IMAGE.write_bytes(b"\x00" * (8 * 1024 * 1024))
        loop = run(["losetup", "--find", "--show", str(IMAGE)]).stdout.strip()
        size = IMAGE.stat().st_size
        volume = {
            "id": "/dev/disk/by-id/scsi-san-fabric",
            "path": loop,
            "wwn": "naa.5000sanfabric",
            "serial": "SANFABRIC",
            "size": size,
            "block_size": 512,
            "kind": "block",
            "mounts": [],
            "holders": [],
            "active": False,
            "boot": False,
        }
        target = plan_export(
            volume, {"address": PORTAL, "port": 3260}, IQN, INITIATOR, USER, 0,
        )
        RtslibBackend().apply(target, SECRET, Probe(volume))
        login()
        dev = device()
        rtpg_proc = run(["sg_rtpg", dev], check=False)
        rtpg = ((rtpg_proc.stdout or "") + (rtpg_proc.stderr or "")).lower()
        report["rtpg_text"] = rtpg[:1500]
        report["alua_config"] = _alua_config()
        # sg_rtpg imprime les états SPC (0x00 actif optimisé, 0x02 standby), pas les mots.
        report["rtpg"] = (
            "pref=1" in rtpg
            and "asymmetric access state : 0x00" in rtpg
            and "pref=0" in rtpg
            and "asymmetric access state : 0x02" in rtpg
        )
        report["alua"] = report["rtpg"]
        run(["dd", "if=/dev/zero", "of={}".format(dev), "bs=1M", "count=4", "conv=fsync"], check=True)
        unmap = run(["sg_unmap", "--lba=2048", "--num=2048", dev], check=False)
        report["unmap_status"] = unmap.returncode
        run(["blockdev", "--flushbufs", dev], check=False)
        time.sleep(1)
        holes = hole_at(IMAGE)
        report["hole"] = holes
        report["unmap"] = unmap.returncode == 0 and holes["punched"]
        from rtslib_fb import RTSRoot
        enabled_before = []
        for item in RTSRoot().targets:
            for tpg in item.tpgs:
                if tpg.enable:
                    enabled_before.append(tpg.tag)
        gate = ConfigfsGate()
        gate.freeze()
        frozen = all(not tpg.enable for item in RTSRoot().targets for tpg in item.tpgs)
        gate.resume()
        enabled_after = [tpg.tag for item in RTSRoot().targets for tpg in item.tpgs if tpg.enable]
        report["freeze"] = frozen and sorted(enabled_after) == sorted(enabled_before) and bool(enabled_before)
        report["enabled_before"] = enabled_before
        report["enabled_after"] = enabled_after
    except Exception as exc:
        report["error"] = "{}: {}".format(exc.__class__.__name__, exc)
    finally:
        cleanup(loop)
        try:
            IMAGE.unlink()
        except OSError:
            pass
    print("SAN-MEASURE " + json.dumps(report, sort_keys=True))
    return 0 if report.get("alua") and report.get("unmap") and report.get("freeze") else 1


if __name__ == "__main__":
    sys.exit(main())
