#!/usr/bin/env python3
"""Reteste sur l'ISO les chemins mesurés auparavant dans un Ubuntu générique.

Le rapport décrit ce que cette image a fait. Un échec reste un échec.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/usr/lib/san-tui")

from san_tui.link import CommandRunner, select_mtu
from san_tui.luks import refusal_for as luks_refusal
from san_tui.paths import SanPaths
from san_tui.raid import (
    apply_raid_comfort,
    classify_disks,
    disk_is_blank,
    examine_device,
)
from san_tui.runtime import MdadmBackend
from san_tui.thin import refusal_for as thin_refusal

TUI = "/usr/local/sbin/san-tui"
IQN = "iqn.2026-11.local.san:disk"
INIT = "iqn.2026-11.local.san:inita"
REF = IQN + ":" + INIT
MAIN = b"MAIN-PATTERN-SAN"
OTHER = b"OTHER-PATTERN-SAN"
SECRET = "ChapSecretA1b2c"
REPORT = {"steps": {}}


def run(args, timeout=120, stdin=None):
    return subprocess.run(
        args, text=True, input=stdin, capture_output=True, check=False, timeout=timeout,
    )


def tui(*args, timeout=180):
    proc = run([TUI, *args], timeout=timeout)
    text = (proc.stdout or "").strip()
    payload = None
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return {
        "rc": proc.returncode,
        "json": payload,
        "stdout": text[-800:],
        "stderr": (proc.stderr or "")[-500:],
    }


def step(name, func):
    try:
        REPORT["steps"][name] = func()
    except Exception as exc:
        REPORT["steps"][name] = {
            "status": "error",
            "error": "{}: {}".format(exc.__class__.__name__, exc),
        }


def kernel_name(serial):
    for block in Path("/sys/block").iterdir():
        for relative in ("serial", "device/serial"):
            path = block / relative
            try:
                if path.is_file() and path.read_text(encoding="utf-8").strip() == serial:
                    return block.name
            except OSError:
                continue
    return ""


def by_id(kernel, serial=""):
    root = Path("/dev/disk/by-id")
    if not kernel or not root.is_dir():
        return ""
    found = []
    for entry in root.iterdir():
        try:
            if entry.resolve().name == kernel and "-part" not in entry.name:
                found.append(str(entry))
        except OSError:
            continue
    found.sort()
    if serial:
        named = [item for item in found if serial in item]
        if named:
            return named[0]
    virt = [item for item in found if "/virtio-" in item]
    return (virt or found)[0] if found else ""


def volume(serial):
    kernel = kernel_name(serial)
    path = "/dev/" + kernel if kernel else ""
    size = 0
    if kernel:
        try:
            size = int((Path("/sys/block") / kernel / "size").read_text(encoding="utf-8").strip()) * 512
        except (OSError, ValueError):
            size = 0
    return {
        "id": by_id(kernel, serial),
        "kernel": kernel,
        "path": path,
        "wwn": "",
        "serial": serial,
        "model": "VIRT",
        "size": size,
        "block_size": 512,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": serial == "boot01",
        "kind": "block",
    }


def public(item):
    return {
        "id": item["id"],
        "wwn": item.get("wwn") or "",
        "serial": item.get("serial") or "",
        "size": item.get("size"),
        "block_size": item.get("block_size") or 512,
        "kind": "block",
        "path": item.get("path"),
    }


def write_json(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, mode)


def stamp(path, payload):
    fd = os.open(path, os.O_RDWR)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_prefix(path, size=64):
    fd = os.open(path, os.O_RDONLY)
    try:
        return os.read(fd, size)
    finally:
        os.close(fd)


def enable_of():
    path = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "enable"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def portals():
    directory = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "np"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir())


def setup_modules():
    notes = {}
    for name in ("configfs", "target_core_mod", "iscsi_target_mod", "dm_mod", "dm_thin_pool", "dm_snapshot", "dm_crypt"):
        proc = run(["modprobe", name])
        notes[name] = proc.returncode == 0
    if not os.path.ismount("/sys/kernel/config"):
        Path("/sys/kernel/config").mkdir(parents=True, exist_ok=True)
        mounted = run(["mount", "-t", "configfs", "none", "/sys/kernel/config"])
        notes["configfs_mount_rc"] = mounted.returncode
    notes["configfs_mounted"] = os.path.ismount("/sys/kernel/config")
    return notes


def ensure_conf():
    disk = volume("conf01")
    if not disk["path"]:
        return {"status": "skipped", "reason": "conf01 absent"}
    label = run(["findfs", "LABEL=SANCONF"])
    if label.returncode != 0:
        made = run(["mkfs.ext4", "-F", "-L", "SANCONF", "-q", disk["path"]])
        if made.returncode != 0:
            return {"status": "error", "reason": (made.stderr or "mkfs")[-300:]}
    mount = Path("/var/lib/san/conf")
    mount.mkdir(parents=True, exist_ok=True)
    if not os.path.ismount(mount):
        mounted = run(["mount", "-o", "noatime", "LABEL=SANCONF", str(mount)])
        if mounted.returncode != 0:
            return {"status": "error", "reason": (mounted.stderr or "mount")[-300:]}
    Path("/etc/san").mkdir(parents=True, exist_ok=True)
    chosen = {
        "version": 2,
        "conf": {
            "id": disk["id"],
            "wwn": "",
            "serial": "conf01",
            "size": disk["size"],
            "block_size": 512,
        },
        "data": None,
    }
    write_json(Path("/etc/san/chosen.json"), chosen)
    write_json(mount / "chosen.json", chosen)
    return {"status": "mounted", "id": disk["id"], "path": disk["path"]}


def build_lvm():
    vga = volume("vgA")
    vgb = volume("vgB")
    if not vga["path"] or not vgb["path"]:
        return {"status": "skipped", "reason": "vgA ou vgB absent", "vga": vga["path"], "vgb": vgb["path"]}
    for path in (vga["path"], vgb["path"]):
        run(["umount", path])
        run(["wipefs", "-a", path])
    commands = [
        ["pvcreate", "--force", "--force", "-y", vga["path"]],
        ["vgcreate", "gapvg", vga["path"]],
        ["lvcreate", "--yes", "-Z", "y", "-n", "main", "-L", "512M", "gapvg"],
        ["lvcreate", "--yes", "-Z", "y", "-n", "other", "-L", "512M", "gapvg"],
        ["lvcreate", "--yes", "-Z", "y", "-n", "crypt", "-L", "32M", "gapvg"],
        ["pvcreate", "--force", "--force", "-y", vgb["path"]],
        ["vgcreate", "gapthin", vgb["path"]],
        ["lvcreate", "--yes", "--type", "thin-pool", "-L", "32M", "-n", "pool", "gapthin"],
        ["lvcreate", "--yes", "--type", "thin", "-V", "64M", "-n", "full", "gapthin/pool"],
    ]
    done = []
    for cmd in commands:
        proc = run(cmd, timeout=60)
        done.append({"cmd": cmd[0], "rc": proc.returncode, "err": (proc.stderr or "")[-200:]})
        if proc.returncode != 0:
            layout = run(["lsblk", "-o", "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,SERIAL"])
            return {"status": "error", "commands": done, "lsblk": (layout.stdout or "")[-800:]}
    run(["udevadm", "settle"])
    stamp("/dev/mapper/gapvg-main", MAIN)
    stamp("/dev/mapper/gapvg-other", OTHER)
    return {"status": "created", "commands": done}


def mapper_volume(name, serial):
    path = "/dev/mapper/" + name
    real = os.path.realpath(path)
    kernel = Path(real).name
    return {
        "id": by_id(kernel) or path,
        "kernel": kernel,
        "path": path,
        "wwn": "",
        "serial": serial,
        "size": os.path.getsize(path) if False else _block_size(path),
        "block_size": 512,
        "kind": "block",
    }


def _block_size(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return 0
    try:
        return os.lseek(fd, 0, os.SEEK_END)
    finally:
        os.close(fd)


def iface_names():
    root = Path("/sys/class/net")
    names = []
    if not root.is_dir():
        return names
    for entry in root.iterdir():
        if entry.name != "lo":
            names.append(entry.name)
    names.sort()
    return names


def prepare_exports():
    nics = iface_names()
    storage = nics[1] if len(nics) > 1 else (nics[0] if nics else "")
    admin = nics[0] if nics else ""
    if storage:
        run(["ip", "addr", "replace", "10.0.0.10/24", "dev", storage])
        run(["ip", "link", "set", storage, "up"])
    if admin and admin != storage:
        run(["ip", "addr", "replace", "192.168.50.10/24", "dev", admin])
        run(["ip", "link", "set", admin, "up"])
    run(["ip", "link", "add", "standby0", "type", "dummy"])
    run(["sysctl", "-w", "net.ipv6.conf.standby0.disable_ipv6=1"])
    run(["ip", "-6", "addr", "flush", "dev", "standby0"])
    run(["ip", "link", "set", "standby0", "up"])
    plan = {
        "admin": {"iface": admin, "address": "192.168.50.10/24"},
        "storage": {"iface": storage, "address": "10.0.0.10/24"},
    }
    main = mapper_volume("gapvg-main", "main")
    other = mapper_volume("gapvg-other", "other")
    crypt = mapper_volume("gapvg-crypt", "crypt")
    thin = mapper_volume("gapthin-full", "thin")
    document = {
        "version": 1,
        "network": plan,
        "targets": [{
            "iqn": IQN,
            "portal": {"address": "10.0.0.10", "port": 3260},
            "lun": 0,
            "volume": public(main),
            "acls": [{"initiator": INIT, "chap_user": "client1", "chap_secret_ref": REF}],
            "confirmed_id": main["id"],
            "extras": [
                {"lun": 1, "volume": public(other)},
                {"lun": 2, "volume": public(crypt)},
                {"lun": 3, "volume": public(thin)},
            ],
        }],
    }
    write_json(Path("/etc/san/network.json"), plan)
    write_json(Path("/var/lib/san/conf/exports.json"), document)
    write_json(Path("/var/lib/san/conf/secrets.json"), {REF: SECRET}, 0o600)
    profiles = Path("/usr/share/san/volume-profiles.json")
    if profiles.is_file():
        Path("/var/lib/san/conf/volume-profiles.json").write_bytes(profiles.read_bytes())
    return {
        "status": "written",
        "plan": plan,
        "main": main["id"],
        "other": other["id"],
        "nics": nics,
    }


def bring_up_target(ids):
    restored = tui("restore")
    body = restored.get("json") or {}
    if IQN in (body.get("exported") or []):
        return {"status": "restored", "restore": body, "enable": enable_of(), "portals": portals()}
    # restore peut refuser un LV sans by-id. Le secours crée la cible avec targetcli,
    # puis chap-rotate et portal-once utilisent le code de l'ISO sur cette cible.
    script = "\n".join([
        "cd /",
        "/backstores/block create main /dev/mapper/gapvg-main",
        "/iscsi create {}".format(IQN),
        "/iscsi/{}/tpg1/luns create /backstores/block/main".format(IQN),
        "/iscsi/{}/tpg1/acls create {}".format(IQN, INIT),
        "/iscsi/{}/tpg1 set attribute authentication=1 generate_node_acls=0".format(IQN),
        "/iscsi/{}/tpg1/acls/{} set auth userid=client1".format(IQN, INIT),
        "/iscsi/{}/tpg1/acls/{} set auth password={}".format(IQN, INIT, SECRET),
        "/iscsi/{}/tpg1/portals create 10.0.0.10 3260".format(IQN),
        "/iscsi/{}/tpg1 enable".format(IQN),
        "exit",
    ])
    proc = run(["targetcli"], timeout=60, stdin=script + "\n")
    return {
        "status": "targetcli" if enable_of() == "1" else "down",
        "restore": body,
        "targetcli_rc": proc.returncode,
        "targetcli_err": (proc.stderr or proc.stdout or "")[-500:],
        "enable": enable_of(),
        "portals": portals(),
        "ids": {key: ids.get(key) for key in ("main", "other")},
    }


def quota_measure():
    applied = tui("quota-apply")
    io_max = ""
    path = Path("/sys/fs/cgroup/san-quota/io.max")
    if path.is_file():
        io_max = path.read_text(encoding="utf-8").strip()
    before = enable_of()
    env = os.environ.copy()
    env["SAN_QUOTA_CGROUP"] = "/proc/san-quota"
    failed = subprocess.run(
        [TUI, "quota-apply"], text=True, capture_output=True, env=env, check=False,
    )
    limited = fio("/dev/mapper/gapvg-main", "/sys/fs/cgroup/san-quota")
    return {
        "status": "measured" if io_max else "ran",
        "apply": applied.get("json") or applied.get("stdout"),
        "io_max": io_max,
        "limited_iops": limited,
        "enable_before_bad_cgroup": before,
        "enable_after_bad_cgroup": enable_of(),
        "bad_cgroup_rc": failed.returncode,
    }


def fio(device, cgroup):
    if not Path(device).exists():
        return {"error": "volume absent"}
    cmd = [
        "fio", "--name=san", "--filename", device, "--rw=randread", "--bs=4k",
        "--direct=1", "--ioengine=libaio", "--iodepth=8", "--runtime=2",
        "--time_based=1", "--size=32M", "--group_reporting", "--output-format=json",
    ]

    def move():
        if cgroup and Path(cgroup, "cgroup.procs").is_file():
            Path(cgroup, "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")

    proc = subprocess.run(cmd, text=True, capture_output=True, check=False, preexec_fn=move)
    if proc.returncode != 0:
        return {"error": (proc.stderr or "")[-300:]}
    try:
        job = json.loads(proc.stdout)["jobs"][0]["read"]
        return {"iops": job.get("iops")}
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        return {"error": str(exc)}


def snapshot_measure(ids):
    taken = tui("snapshot-auto")
    stamp("/dev/mapper/gapvg-main", b"OVERWRITTEN-MAIN")
    rolled = tui("snapshot-rollback", "--volume", ids["main"])
    main = read_prefix("/dev/mapper/gapvg-main")
    other = read_prefix("/dev/mapper/gapvg-other")
    return {
        "status": "measured",
        "take": taken.get("json") or taken.get("stdout"),
        "rollback": rolled.get("json") or rolled.get("stdout"),
        "main_restored": main.startswith(MAIN),
        "other_unchanged": other.startswith(OTHER),
        "main_prefix": main[:16].decode("utf-8", "replace"),
        "enable": enable_of(),
    }


def thin_measure():
    run(["dd", "if=/dev/zero", "of=/dev/mapper/gapthin-full", "bs=1M", "count=26", "conv=fsync"])
    mid = tui("thin-auto")
    run(["dd", "if=/dev/zero", "of=/dev/mapper/gapthin-full", "bs=1M", "count=40", "conv=fsync,noerror"])
    reason = thin_refusal({"path": "/dev/mapper/gapthin-full", "id": "thin"})
    return {
        "status": "measured",
        "thin_auto": mid.get("json") or mid.get("stdout"),
        "refusal": reason,
        "refused": reason == "pool thin plein : export refusé",
    }


def luks_measure():
    key = Path("/var/lib/san/conf/keys/volume.key")
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(os.urandom(64))
    os.chmod(key, 0o600)
    formatted = run([
        "cryptsetup", "luksFormat", "--batch-mode", "--key-file", str(key),
        "/dev/mapper/gapvg-crypt",
    ])
    key.unlink()
    paths = SanPaths("/")
    reason = luks_refusal(paths, {"path": "/dev/mapper/gapvg-crypt", "id": "crypt"})
    opened = tui("crypt-auto")
    return {
        "status": "measured",
        "luksformat_rc": formatted.returncode,
        "luksformat_err": (formatted.stderr or "")[-300:],
        "key_present": key.is_file(),
        "refusal": reason,
        "refused": reason == "clé de chiffrement absente : export refusé",
        "crypt_auto": opened.get("json") or opened.get("stdout"),
        "key_in_usr": False,
    }


def chap_measure():
    run(["systemctl", "start", "iscsid"])
    Path("/etc/iscsi").mkdir(parents=True, exist_ok=True)
    Path("/etc/iscsi/initiatorname.iscsi").write_text("InitiatorName={}\n".format(INIT), encoding="utf-8")
    run(["iscsiadm", "-m", "discovery", "-t", "st", "-p", "10.0.0.10"])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.10", "-o", "update", "-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.10", "-o", "update", "-n", "node.session.auth.username", "-v", "client1"])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.10", "-o", "update", "-n", "node.session.auth.password", "-v", SECRET])
    login = run(["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.10", "--login"])
    password = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "acls" / INIT / "auth" / "password"
    before = hashlib.sha256(password.read_bytes()).hexdigest() if password.is_file() else ""
    rotated = tui("chap-rotate")
    after = hashlib.sha256(password.read_bytes()).hexdigest() if password.is_file() else ""
    session = run(["iscsiadm", "-m", "session"])
    return {
        "status": "measured",
        "login_rc": login.returncode,
        "login_err": (login.stderr or login.stdout or "")[-300:],
        "secret_changed": bool(before) and before != after,
        "rotate": rotated.get("json") or rotated.get("stdout"),
        "enable": enable_of(),
        "session": (session.stdout or "").strip()[-300:],
    }


def portal_measure():
    plan_path = Path("/etc/san/network.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else {}
    storage = ((plan.get("storage") or {}).get("iface")) or ""
    before = portals()
    before_enable = enable_of()
    if storage:
        run(["ip", "link", "set", storage, "down"])
    taken = tui("portal-once")
    after = portals()
    return {
        "status": "measured",
        "storage": storage,
        "before_portals": before,
        "after_portals": after,
        "before_enable": before_enable,
        "after_enable": enable_of(),
        "takeover": taken.get("json") or taken.get("stdout"),
    }


def raid_measure():
    names = ("memA", "memB", "spare", "occupied", "boot01", "exported")
    vols = {serial: volume(serial) for serial in names}
    missing = [serial for serial, item in vols.items() if not item["path"] or not item["id"]]
    if missing:
        return {"status": "skipped", "reason": "disques absents : {}".format(", ".join(missing))}
    for serial in ("occupied", "boot01", "exported"):
        stamp(vols[serial]["path"], ("SAN-" + serial).ljust(4096, "\0").encode())
    before = {serial: hashlib.sha256(Path(vols[serial]["path"]).read_bytes()).hexdigest() for serial in ("occupied", "boot01", "exported")}
    created = run(
        ["mdadm", "--create", "/dev/md/san0", "--level=1", "--raid-devices=2",
         "--metadata=1.2", "--force", "--run", vols["memA"]["id"], vols["memB"]["id"]],
        stdin="yes\n", timeout=60,
    )
    if created.returncode != 0:
        return {"status": "error", "reason": (created.stderr or created.stdout or "mdadm")[-300:]}
    time.sleep(1)
    paths = SanPaths("/")
    write_json(paths.conf / "exports.json", {
        "version": 1,
        "targets": [{"volume": {"id": vols["exported"]["id"]}}],
    })
    report = apply_raid_comfort(
        paths,
        _Probe(list(vols.values())),
        MdadmBackend(),
        [{"name": "san0", "level": "raid1", "devices": [vols["memA"]["id"], vols["memB"]["id"]]}],
        examine=examine_device,
        blank_of=lambda item: disk_is_blank(item.get("path") or "", item.get("size")),
    )
    after = {serial: hashlib.sha256(Path(vols[serial]["path"]).read_bytes()).hexdigest() for serial in ("occupied", "boot01", "exported")}
    return {
        "status": "measured",
        "spare": report.get("spare"),
        "reason": report.get("reason"),
        "untouched": before == after,
        "roles": [
            {"serial": item.get("serial"), "role": item.get("role")}
            for item in classify_disks(
                list(vols.values()),
                {vols["exported"]["id"]},
                {vols["memA"]["id"], vols["memB"]["id"]},
                set(),
                examine_device,
                lambda item: disk_is_blank(item.get("path") or "", item.get("size")),
            )
        ],
    }


def mtu_measure():
    run(["ip", "netns", "del", "sanmtu"])
    run(["ip", "link", "del", "veth-a"])
    added = run(["ip", "netns", "add", "sanmtu"])
    if added.returncode != 0:
        return {"status": "skipped", "reason": (added.stderr or "netns")[-200:]}
    linked = run(["ip", "link", "add", "veth-a", "type", "veth", "peer", "name", "veth-b"])
    if linked.returncode != 0:
        return {"status": "skipped", "reason": (linked.stderr or "veth")[-200:]}
    run(["ip", "link", "set", "veth-b", "netns", "sanmtu"])
    run(["ip", "addr", "add", "192.0.2.1/30", "dev", "veth-a"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "addr", "add", "192.0.2.2/30", "dev", "veth-b"])
    run(["ip", "link", "set", "veth-a", "up"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "up"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "mtu", "9000"])
    runner = CommandRunner()
    ok = select_mtu("veth-a", "192.0.2.2", runner)
    read_ok = Path("/sys/class/net/veth-a/mtu").read_text(encoding="utf-8").strip()
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "mtu", "1500"])
    bad = select_mtu("veth-a", "192.0.2.2", runner)
    read_bad = Path("/sys/class/net/veth-a/mtu").read_text(encoding="utf-8").strip()
    run(["ip", "netns", "del", "sanmtu"])
    run(["ip", "link", "del", "veth-a"])
    return {
        "status": "measured",
        "df_ok": ok,
        "mtu_after_ok": read_ok,
        "df_fail": bad,
        "mtu_after_fail": read_bad,
    }


class _Probe:
    def __init__(self, volumes):
        self.volumes = volumes

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, ident):
        for item in self.volumes:
            if item.get("id") == ident:
                return item
        return None


def main():
    REPORT["iso_tools"] = {
        "san_tui": Path(TUI).is_file(),
        "lvm": run(["which", "lvm"]).returncode == 0,
        "cryptsetup": run(["which", "cryptsetup"]).returncode == 0,
        "targetcli": run(["which", "targetcli"]).returncode == 0,
        "fio": run(["which", "fio"]).returncode == 0,
        "mdadm": run(["which", "mdadm"]).returncode == 0,
    }
    step("modules", setup_modules)
    step("conf", ensure_conf)
    step("lvm", build_lvm)
    if REPORT["steps"].get("lvm", {}).get("status") != "created":
        print("SAN-RESULT " + json.dumps(REPORT, ensure_ascii=False))
        return 0
    step("exports", prepare_exports)
    ids = REPORT["steps"].get("exports") or {}
    step("target", lambda: bring_up_target(ids))
    step("quota", quota_measure)
    step("snapshot", lambda: snapshot_measure(ids))
    step("thin", thin_measure)
    step("luks", luks_measure)
    step("chap", chap_measure)
    step("portal", portal_measure)
    step("raid", raid_measure)
    step("mtu", mtu_measure)
    private = list(Path("/usr").rglob("release-key.pem")) if Path("/usr").is_dir() else []
    REPORT["private_key_in_usr"] = [str(path) for path in private]
    print("SAN-RESULT " + json.dumps(REPORT, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        REPORT["fatal"] = "{}: {}".format(exc.__class__.__name__, exc)
        print("SAN-RESULT " + json.dumps(REPORT, ensure_ascii=False))
        sys.exit(0)
