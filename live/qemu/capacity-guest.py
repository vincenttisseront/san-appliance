#!/usr/bin/env python3
"""Mesures des dix fonctions dans une VM. Le rapport dit ce qui a été exécuté."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/repo/live24/tui")

REPORT_PATH = Path("/repo/live/work/capacity-report.json")
IQN = "iqn.2026-11.local.san:disk"
INIT_A = "iqn.2026-11.local.san:inita"
INIT_B = "iqn.2026-11.local.san:initb"
REF = IQN + ":" + INIT_A
TUI = "/repo/live24/tui/bin/san-tui"
MAIN = b"MAIN-PATTERN-SAN"
OTHER = b"OTHER-PATTERN-SAN"


def run(args, check=False, timeout=120):
    proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError("{}: {}".format(" ".join(args[:6]), (proc.stderr or proc.stdout)[-400:]))
    return proc


def tui(*args, timeout=180):
    proc = run([TUI, *args], timeout=timeout)
    text = proc.stdout.strip()
    payload = None
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    return {"rc": proc.returncode, "json": payload, "stderr": (proc.stderr or "")[-500:]}


def enable_of():
    path = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "enable"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def lun_list():
    directory = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "lun"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir())


def portals():
    directory = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "np"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir())


def disk_by_serial(serial):
    for block in Path("/sys/block").iterdir():
        serial_path = block / "serial"
        if serial_path.is_file() and serial_path.read_text(encoding="utf-8").strip() == serial:
            return "/dev/" + block.name
    return None


def inventory():
    proc = run([TUI, "inventory"], timeout=60)
    return json.loads(proc.stdout)


def volume_for_mapper(name):
    real = os.path.realpath("/dev/mapper/" + name)
    for disk in inventory()["disks"]:
        try:
            if os.path.realpath(disk.get("path") or "") == real:
                return disk
        except OSError:
            continue
    return None


def public(volume):
    return {
        "id": volume["id"],
        "wwn": volume.get("wwn") or "",
        "serial": volume.get("serial") or "",
        "size": volume.get("size"),
        "block_size": volume.get("block_size"),
        "kind": volume.get("kind") or "block",
        "path": volume.get("path"),
    }


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


def write_json(path, data, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, mode)


def lvs_text():
    return run(["lvs", "--noheadings", "-o", "lv_name,vg_name,data_percent,lv_attr"]).stdout


def step(report, name, func):
    try:
        report["steps"][name] = func()
    except Exception as exc:
        report["steps"][name] = {"ok": False, "error": "{}: {}".format(exc.__class__.__name__, exc)}
    report["steps"][name]["iscsi_enable"] = enable_of()


def setup_modules():
    notes = {}
    for name in ("configfs", "target_core_mod", "iscsi_target_mod", "dm_mod", "dm_thin_pool", "dm_snapshot", "dm_crypt", "bonding", "br_netfilter"):
        proc = run(["modprobe", name])
        notes[name] = proc.returncode == 0
    if not os.path.ismount("/sys/kernel/config"):
        Path("/sys/kernel/config").mkdir(parents=True, exist_ok=True)
        run(["mount", "-t", "configfs", "none", "/sys/kernel/config"])
    notes["configfs_mounted"] = os.path.ismount("/sys/kernel/config")
    run(["systemctl", "stop", "target.service", "rtslib-fb-targetctl.service"])
    return notes


def cleanup_lacp():
    run(["ip", "link", "delete", "san-stor"])
    run(["ip", "netns", "delete", "peer"])
    for name in ("lacp1", "lacp2", "admin0"):
        run(["ip", "link", "delete", name])


def lacp_measure():
    cleanup_lacp()
    run(["ip", "link", "add", "admin0", "type", "dummy"], check=True)
    run(["ip", "link", "set", "admin0", "up"])
    run(["ip", "addr", "replace", "192.168.50.10/24", "dev", "admin0"])
    run(["ip", "link", "add", "lacp1", "type", "veth", "peer", "name", "sw1"], check=True)
    run(["ip", "link", "add", "lacp2", "type", "veth", "peer", "name", "sw2"], check=True)
    run(["ip", "link", "set", "lacp1", "up"])
    run(["ip", "link", "set", "lacp2", "up"])
    run(["ip", "addr", "replace", "10.9.0.10/24", "dev", "lacp1"])
    plan = {
        "admin": {"iface": "admin0", "address": "192.168.50.10/24"},
        "storage": {"iface": "lacp1", "address": "10.9.0.10/24"},
    }
    write_json(Path("/etc/san/network.json"), plan)
    write_json(Path("/var/lib/san/conf/exports.json"), {"version": 1, "network": plan, "targets": []})
    write_json(Path("/var/lib/san/conf/secrets.json"), {}, 0o600)
    quiet = tui("lacp-auto")
    quiet_bond = Path("/sys/class/net/san-stor").exists()
    run(["ip", "netns", "add", "peer"], check=True)
    run(["ip", "link", "set", "sw1", "netns", "peer"])
    run(["ip", "link", "set", "sw2", "netns", "peer"])
    for args in (
        ["ip", "netns", "exec", "peer", "ip", "link", "add", "san-sw", "type", "bond", "mode", "802.3ad", "lacp_rate", "fast"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw1", "down"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw2", "down"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw1", "master", "san-sw"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw2", "master", "san-sw"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw1", "up"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "sw2", "up"],
        ["ip", "netns", "exec", "peer", "ip", "link", "set", "san-sw", "up"],
        ["ip", "netns", "exec", "peer", "ip", "addr", "add", "10.9.0.1/24", "dev", "san-sw"],
    ):
        proc = run(args)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr[-300:])
    time.sleep(2)
    advertised = tui("lacp-auto")
    run(["sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"])
    run(["sysctl", "-w", "net.ipv4.conf.san-stor.rp_filter=0"])
    time.sleep(1)
    ping = run(["ping", "-c", "3", "-W", "2", "-I", "san-stor", "10.9.0.1"])
    addr = run(["ip", "-4", "addr", "show", "dev", "san-stor"]).stdout.strip()
    slaves = ""
    slave_path = Path("/sys/class/net/san-stor/bonding/slaves")
    if slave_path.is_file():
        slaves = slave_path.read_text(encoding="utf-8").strip()
    bonding = ""
    bond_file = Path("/proc/net/bonding/san-stor")
    if bond_file.is_file():
        bonding = bond_file.read_text(encoding="utf-8", errors="replace")[:800]
    undo = tui("lacp-undo")
    result = {
        "ok": True,
        "quiet_when_switch_is_silent": quiet.get("json"),
        "bond_created_without_advertisement": quiet_bond,
        "with_advertisement": advertised.get("json"),
        "slaves": slaves,
        "admin_enslaved": "admin0" in slaves.split(),
        "ping_received": ping.stdout.count("bytes from"),
        "ping_err": (ping.stderr or "")[-200:],
        "bond_addr": addr,
        "bonding_excerpt": bonding,
        "undo": undo.get("json"),
        "bond_after_undo": Path("/sys/class/net/san-stor").exists(),
    }
    cleanup_lacp()
    return result


def build_net():
    run(["ip", "link", "add", "admin0", "type", "dummy"])
    run(["ip", "link", "set", "admin0", "up"])
    run(["ip", "addr", "replace", "192.168.50.10/24", "dev", "admin0"])
    run(["ip", "link", "add", "br0", "type", "bridge"], check=True)
    run(["ip", "link", "set", "br0", "up"])
    run(["ip", "link", "add", "stor", "type", "veth", "peer", "name", "stor-br"], check=True)
    run(["ip", "link", "add", "a-stand", "type", "veth", "peer", "name", "stand-br"], check=True)
    run(["ip", "link", "set", "stor-br", "master", "br0"])
    run(["ip", "link", "set", "stand-br", "master", "br0"])
    run(["ip", "link", "set", "stor", "up"])
    run(["ip", "link", "set", "stor-br", "up"])
    run(["ip", "link", "set", "a-stand", "up"])
    run(["ip", "link", "set", "stand-br", "up"])
    run(["ip", "addr", "replace", "10.0.0.10/24", "dev", "stor"])
    run(["ip", "addr", "replace", "10.0.0.11/24", "dev", "a-stand"])
    return {
        "admin": {"iface": "admin0", "address": "192.168.50.10/24"},
        "storage": {"iface": "stor", "address": "10.0.0.10/24"},
    }


def build_lvm():
    vga = disk_by_serial("vgA")
    vgb = disk_by_serial("vgB")
    conf = disk_by_serial("conf01")
    if not vga or not vgb or not conf:
        raise RuntimeError("disques absents {} {} {}".format(vga, vgb, conf))
    run(["pvcreate", "-ff", "-y", vga], check=True)
    run(["vgcreate", "sanvg", vga], check=True)
    for name, size in (("main", "64M"), ("other", "64M"), ("blank", "64M"), ("crypt", "32M"), ("nvme", "32M")):
        run(["lvcreate", "--yes", "-Z", "y", "-n", name, "-L", size, "sanvg"], check=True)
    run(["pvcreate", "-ff", "-y", vgb], check=True)
    run(["vgcreate", "thing", vgb], check=True)
    run(["lvcreate", "--yes", "--type", "thin-pool", "-L", "32M", "-n", "pool", "thing"], check=True)
    run(["lvcreate", "--yes", "--type", "thin", "-V", "64M", "-n", "full", "thing/pool"], check=True)
    run(["udevadm", "settle"])
    stamp("/dev/mapper/sanvg-main", MAIN)
    stamp("/dev/mapper/sanvg-other", OTHER)
    stamp("/dev/mapper/thing-full", b"T")
    return {"vga": vga, "vgb": vgb, "conf": conf}


def prepare_exports(plan):
    run(["udevadm", "settle"])
    time.sleep(1)
    disks = {disk.get("kernel"): disk for disk in inventory()["disks"]}
    conf = None
    for disk in inventory()["disks"]:
        if disk.get("serial") == "conf01" or (disk.get("path") and disk["path"] == disk_by_serial("conf01")):
            conf = disk
            break
    if conf is None:
        # Le série virtio est sur le disque, l'identifiant by-id porte virtio-conf01.
        for disk in inventory()["disks"]:
            if "conf01" in (disk.get("id") or ""):
                conf = disk
                break
    if conf is None:
        raise RuntimeError("support conf introuvable: {}".format([d.get("id") for d in inventory()["disks"]]))
    selected = tui("persist-select", "--role", "conf", "--id", conf["id"], "--confirm", conf["id"])
    main = volume_for_mapper("sanvg-main")
    other = volume_for_mapper("sanvg-other")
    crypt = volume_for_mapper("sanvg-crypt")
    thin = volume_for_mapper("thing-full")
    nvme = volume_for_mapper("sanvg-nvme")
    missing = [name for name, item in (("main", main), ("other", other), ("crypt", crypt), ("thin", thin), ("nvme", nvme)) if not item]
    if missing:
        raise RuntimeError("LV sans by-id: {} {}".format(missing, [d.get("id") for d in inventory()["disks"]]))
    secret = "ChapSecret" + "A1b2"
    document = {
        "version": 1,
        "network": plan,
        "targets": [{
            "iqn": IQN,
            "portal": {"address": "10.0.0.10", "port": 3260},
            "lun": 0,
            "volume": public(main),
            "acls": [
                {"initiator": INIT_A, "chap_user": "client1", "chap_secret_ref": REF},
                {"initiator": INIT_B, "chap_user": "client1", "chap_secret_ref": REF},
            ],
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
    write_json(Path("/var/lib/san/conf/secrets.json"), {REF: secret}, 0o600)
    write_json(Path("/var/lib/san/conf/lvm.json"), {
        "version": 1,
        "groups": [
            {"name": "sanvg", "pvs": [disk_id_for(disk_by_serial("vgA"))], "lvs": ["main"]},
            {"name": "thing", "pvs": [disk_id_for(disk_by_serial("vgB"))], "lvs": ["full"]},
        ],
    })
    return {
        "persist": selected.get("json"),
        "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
        "main": main["id"],
        "other": other["id"],
        "crypt": crypt["id"],
        "thin": thin["id"],
        "nvme": nvme["id"],
        "nvme_volume": public(nvme),
    }


def disk_id_for(path):
    real = os.path.realpath(path)
    for disk in inventory()["disks"]:
        try:
            if os.path.realpath(disk.get("path") or "") == real:
                return disk["id"]
        except OSError:
            continue
    return path


def crypt_with_key():
    opened = tui("crypt-auto")
    key = Path("/var/lib/san/conf/keys/volume.key")
    mapper = None
    for entry in Path("/dev/mapper").iterdir() if Path("/dev/mapper").is_dir() else []:
        if entry.name.startswith("san-crypt-"):
            mapper = str(entry)
    is_luks = run(["cryptsetup", "isLuks", "/dev/mapper/sanvg-crypt"]).returncode == 0
    return {
        "prepare": opened.get("json"),
        "key_exists": key.is_file(),
        "key_mode": oct(key.stat().st_mode & 0o777) if key.is_file() else None,
        "key_path": str(key) if key.is_file() else None,
        "key_under_usr": str(key).startswith("/usr") if key.is_file() else None,
        "is_luks": is_luks,
        "mapper": mapper,
        "mapper_is_block": bool(mapper) and stat_block(mapper),
    }


def stat_block(path):
    try:
        return stat_mode_is_block(os.stat(path).st_mode)
    except OSError:
        return False


def stat_mode_is_block(mode):
    import stat
    return stat.S_ISBLK(mode)


def thin_alert_then_full(ids):
    run(["dd", "if=/dev/zero", "of=/dev/mapper/thing-full", "bs=1M", "count=26", "conv=fsync"])
    mid = tui("thin-auto")
    mid_lvs = lvs_text()
    run(["dd", "if=/dev/zero", "of=/dev/mapper/thing-full", "bs=1M", "count=40", "conv=fsync,noerror"])
    from san_tui.thin import refusal_for
    reason = refusal_for({"path": "/dev/mapper/thing-full", "id": ids["thin"]})
    return {"after_26m": mid.get("json"), "lvs_mid": mid_lvs, "lvs_full": lvs_text(), "refusal": reason}


def drop_key_and_restore():
    for entry in Path("/dev/mapper").iterdir():
        if entry.name.startswith("san-crypt-"):
            run(["cryptsetup", "close", entry.name])
    key = Path("/var/lib/san/conf/keys/volume.key")
    if key.is_file():
        key.unlink()
    restored = tui("restore")
    extras = {}
    extra_path = Path("/run/san/lun-extras.json")
    if extra_path.is_file():
        extras = json.loads(extra_path.read_text(encoding="utf-8"))
    return {
        "restore": restored.get("json"),
        "extras": extras,
        "luns": lun_list(),
        "enable": enable_of(),
        "key_present": key.is_file(),
    }


def initiator_login(name, portal, username_file):
    secret = Path("/var/lib/san/conf/secrets.json").read_text(encoding="utf-8")
    secret = json.loads(secret)[REF]
    Path("/etc/iscsi").mkdir(parents=True, exist_ok=True)
    Path("/etc/iscsi/initiatorname.iscsi").write_text("InitiatorName={}\n".format(name), encoding="utf-8")
    run(["iscsiadm", "-m", "discovery", "-t", "st", "-p", portal])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-o", "update", "-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-o", "update", "-n", "node.session.auth.username", "-v", "client1"])
    proc = subprocess.run(
        ["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-o", "update", "-n", "node.session.auth.password", "-v", secret],
        text=True, capture_output=True, check=False,
    )
    login = run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "--login"])
    session = run(["iscsiadm", "-m", "session"])
    err = (login.stderr or "").replace(secret, "[redacted]")
    return {"login_rc": login.returncode, "login_err": err[-300:], "session": session.stdout.strip(), "update_rc": proc.returncode}


def quota_measure():
    applied = tui("quota-apply")
    io_max = ""
    path = Path("/sys/fs/cgroup/san-quota/io.max")
    if path.is_file():
        io_max = path.read_text(encoding="utf-8").strip()
    comms = []
    procs = Path("/sys/fs/cgroup/san-quota/cgroup.procs")
    if procs.is_file():
        for pid in procs.read_text(encoding="utf-8").split():
            comm = Path("/proc") / pid / "comm"
            if comm.is_file():
                comms.append(comm.read_text(encoding="utf-8").strip())
    device = "/dev/mapper/sanvg-main"
    baseline = fio(device, None)
    limited = fio(device, "/sys/fs/cgroup/san-quota")
    return {"apply": applied.get("json"), "io_max": io_max, "cgroup_comms": comms, "baseline_iops": baseline, "limited_iops": limited}


def fio(device, cgroup):
    cmd = [
        "fio", "--name=san", "--filename", device, "--rw=randread", "--bs=4k",
        "--direct=1", "--ioengine=libaio", "--iodepth=16", "--runtime=3",
        "--time_based=1", "--size=32M", "--group_reporting", "--output-format=json",
    ]

    def move():
        if cgroup:
            Path(cgroup, "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")

    proc = subprocess.run(cmd, text=True, capture_output=True, check=False, preexec_fn=move if cgroup else None)
    if proc.returncode != 0:
        return {"error": (proc.stderr or "")[-300:]}
    try:
        job = json.loads(proc.stdout)["jobs"][0]["read"]
        return {"iops": job.get("iops"), "bw_bytes": job.get("bw_bytes")}
    except (KeyError, json.JSONDecodeError, IndexError) as exc:
        return {"error": str(exc)}


def quota_failure_leaves_iscsi():
    before = enable_of()
    env = os.environ.copy()
    env["SAN_QUOTA_CGROUP"] = "/proc/san-quota"
    proc = subprocess.run([TUI, "quota-apply"], text=True, capture_output=True, env=env, check=False)
    return {"before": before, "after": enable_of(), "rc": proc.returncode, "body": (proc.stdout or "")[-400:]}


def snapshot_and_rollback(ids):
    taken = tui("snapshot-auto")
    before_other = read_prefix("/dev/mapper/sanvg-other")
    stamp("/dev/mapper/sanvg-main", b"OVERWRITTEN-MAIN")
    rolled = tui("snapshot-rollback", "--volume", ids["main"])
    return {
        "take": taken.get("json"),
        "rollback": rolled.get("json"),
        "main_prefix": read_prefix("/dev/mapper/sanvg-main")[:16].decode("utf-8", "replace"),
        "other_prefix": read_prefix("/dev/mapper/sanvg-other")[:18].decode("utf-8", "replace"),
        "other_unchanged": read_prefix("/dev/mapper/sanvg-other").startswith(OTHER),
        "main_restored": read_prefix("/dev/mapper/sanvg-main").startswith(MAIN),
        "luns": lun_list(),
        "enable": enable_of(),
        "other_was": before_other[:18].decode("utf-8", "replace"),
    }


def clone_measure():
    result = tui("clone-auto")
    body = result.get("json") or {}
    wwn_main = ""
    wwn_clone = body.get("wwn") or ""
    from san_tui.lio import wwn_of
    try:
        wwn_main = wwn_of("/dev/mapper/sanvg-main")
    except Exception as exc:
        wwn_main = str(exc)
    clone_prefix = ""
    record = {}
    path = Path("/var/lib/san/conf/clone.json")
    if path.is_file():
        record = json.loads(path.read_text(encoding="utf-8"))
        dest = "/dev/mapper/sanvg-blank"
        if Path(dest).exists():
            clone_prefix = read_prefix(dest)[:16].decode("utf-8", "replace")
    return {
        "clone": body,
        "source_wwn": wwn_main,
        "clone_wwn": wwn_clone,
        "wwn_differs": bool(wwn_clone) and wwn_clone != wwn_main,
        "clone_prefix": clone_prefix,
        "luns": lun_list(),
        "enable": enable_of(),
        "record": {"volume": record.get("volume"), "wwn": record.get("wwn")},
    }


def chap_measure():
    login = initiator_login(INIT_A, "10.0.0.10", None)
    before = run(["iscsiadm", "-m", "session"]).stdout
    password = Path("/sys/kernel/config/target/iscsi") / IQN / "tpgt_1" / "acls" / INIT_A / "auth" / "password"
    before_hash = hashlib.sha256(password.read_bytes()).hexdigest() if password.is_file() else ""
    read_ok = True
    try:
        read_prefix("/dev/mapper/sanvg-main")
    except OSError:
        read_ok = False
    rotated = tui("chap-rotate")
    after = run(["iscsiadm", "-m", "session"]).stdout
    after_hash = hashlib.sha256(password.read_bytes()).hexdigest() if password.is_file() else ""
    read_after = True
    try:
        data = read_prefix(_iscsi_device() or "/dev/mapper/sanvg-main")
        read_after = data.startswith(MAIN) or len(data) > 0
    except OSError:
        read_after = False
    return {
        "login": login,
        "sessions_before": [line for line in before.splitlines() if "iqn." in line],
        "sessions_after": [line for line in after.splitlines() if "iqn." in line],
        "secret_changed": bool(before_hash) and before_hash != after_hash,
        "rotate": rotated.get("json"),
        "enable": enable_of(),
        "read_after": read_after,
        "local_read_before": read_ok,
    }


def _iscsi_device():
    by_path = Path("/dev/disk/by-path")
    if not by_path.is_dir():
        return None
    for entry in by_path.iterdir():
        if IQN in entry.name and entry.name.endswith("-lun-0"):
            return str(entry)
    return None


def portal_measure():
    before_enable = enable_of()
    before_portals = portals()
    run(["ip", "link", "set", "stor", "down"])
    taken = tui("portal-once")
    after = portals()
    # Le lien mort ne garde pas l'ancienne session TCP. Le portail de secours doit accepter un login.
    login = initiator_login(INIT_A, "10.0.0.11", None)
    return {
        "before_enable": before_enable,
        "after_enable": enable_of(),
        "before_portals": before_portals,
        "after_portals": after,
        "takeover": taken.get("json"),
        "login_standby": {"rc": login["login_rc"], "session": login["session"], "err": login["login_err"]},
    }


def pr_measure():
    """Deux initiateurs, deux iface open-iscsi, un seul iscsid."""
    run(["iscsiadm", "-m", "node", "-U", "all"])
    run(["iscsiadm", "-m", "node", "-o", "delete"])
    secret = json.loads(Path("/var/lib/san/conf/secrets.json").read_text(encoding="utf-8"))[REF]
    run(["ip", "link", "set", "stor", "up"])
    run(["ip", "addr", "replace", "10.0.0.10/24", "dev", "stor"])
    results = {}
    for name, iqn in (("inita", INIT_A), ("initb", INIT_B)):
        results[name] = _iface_login(name, iqn, secret)
    dev_a = results["inita"].get("device")
    dev_b = results["initb"].get("device")
    reserve = {}
    if dev_a and dev_b and dev_a != dev_b and _not_root_disk(dev_a) and _not_root_disk(dev_b):
        reserve["register_a"] = _sg(dev_a, ["--out", "--register", "--param-sark=0x11"])
        reserve["reserve_a"] = _sg(dev_a, ["--out", "--reserve", "--param-rk=0x11", "--prout-type=1"])
        reserve["read_res"] = _sg(dev_a, ["--in", "--read-reservation"])
        write_b = run(["dd", "if=/dev/zero", "of=" + dev_b, "bs=512", "count=1", "oflag=direct"])
        reserve["write_b_rc"] = write_b.returncode
        reserve["write_b_err"] = (write_b.stderr or "")[-200:]
        reserve["release_a"] = _sg(dev_a, ["--out", "--release", "--param-rk=0x11", "--prout-type=1"])
        write_b2 = run(["dd", "if=/dev/zero", "of=" + dev_b, "bs=512", "count=1", "oflag=direct"])
        reserve["write_b_after_release_rc"] = write_b2.returncode
        run(["iscsiadm", "-m", "node", "-T", IQN, "-p", "10.0.0.10", "-I", "inita", "--logout"])
        time.sleep(1)
        reserve["read_after_logout"] = _sg(dev_b, ["--in", "--read-reservation"])
    else:
        reserve["skipped"] = "deux disques initiateurs non distincts"
        reserve["devices"] = {"a": dev_a, "b": dev_b}
    return {"initiators": results, "reservation": reserve}


def _not_root_disk(path):
    real = os.path.realpath(path)
    for line in Path("/proc/mounts").read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "/":
            continue
        mounted = os.path.realpath(parts[0])
        if mounted == real or mounted.startswith(real):
            return False
    return True


def _iface_login(name, iqn, secret):
    created = run(["iscsiadm", "-m", "iface", "-I", name, "--op=new"])
    renamed = run(["iscsiadm", "-m", "iface", "-I", name, "-o", "update", "-n", "iface.initiatorname", "-v", iqn])
    discovery = run(["iscsiadm", "-m", "discovery", "-t", "st", "-p", "10.0.0.10", "-I", name])
    portal = "10.0.0.10:3260"
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-I", name, "-o", "update", "-n", "node.session.auth.authmethod", "-v", "CHAP"])
    run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-I", name, "-o", "update", "-n", "node.session.auth.username", "-v", "client1"])
    subprocess.run(
        ["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-I", name, "-o", "update", "-n", "node.session.auth.password", "-v", secret],
        text=True, capture_output=True, check=False,
    )
    login = run(["iscsiadm", "-m", "node", "-T", IQN, "-p", portal, "-I", name, "--login"])
    session = run(["iscsiadm", "-m", "session"])
    time.sleep(2)
    run(["udevadm", "settle"])
    disks = devices_by_initiator()
    device = disks.get(iqn) or block_for_initiator(iqn)
    err = (login.stderr or "").replace(secret, "[redacted]")
    detail = run(["iscsiadm", "-m", "session", "-P", "3"])
    interesting = "\n".join(
        line.strip() for line in detail.stdout.splitlines()
        if "Initiatorname" in line or "Attached scsi" in line or "Iface Name" in line
    ).replace(secret, "[redacted]")
    return {
        "iface_rc": created.returncode,
        "initiatorname_rc": renamed.returncode,
        "initiatorname_err": (renamed.stderr or "")[-200:],
        "discovery_rc": discovery.returncode,
        "discovery_err": (discovery.stderr or discovery.stdout or "")[-300:],
        "login_rc": login.returncode,
        "login_err": err[-300:],
        "session": session.stdout.strip(),
        "device": device,
        "disks": disks,
        "session_detail": interesting[-500:],
    }


def _sg(device, args):
    proc = run(["sg_persist", *args, "-d", device])
    return {"rc": proc.returncode, "out": (proc.stdout or "")[-300:], "err": (proc.stderr or "")[-300:]}


def devices_by_initiator():
    """Disques vus par iscsiadm. Le secret n'est pas recopié."""
    proc = run(["iscsiadm", "-m", "session", "-P", "3"])
    current = None
    found = {}
    for line in proc.stdout.splitlines():
        if "Iface Initiatorname:" in line:
            current = line.split(":", 1)[1].strip()
        elif "Initiatorname:" in line and "Iface" not in line:
            current = line.split(":", 1)[1].strip()
        elif "Attached scsi disk" in line and current:
            parts = line.split()
            if "disk" in parts:
                index = parts.index("disk") + 1
                if index < len(parts):
                    found[current] = "/dev/" + parts[index]
    return found


def block_for_initiator(initiator):
    root = Path("/sys/class/iscsi_session")
    if not root.is_dir():
        return None
    for session in root.iterdir():
        name_file = session / "initiatorname"
        if not name_file.is_file():
            continue
        if name_file.read_text(encoding="utf-8", errors="replace").strip() != initiator:
            continue
        for block in session.rglob("block"):
            if not block.is_dir():
                continue
            for child in block.iterdir():
                return "/dev/" + child.name
    return None


def nvme_measure(ids):
    proc = run(["modprobe", "nvmet"])
    tcp = run(["modprobe", "nvmet-tcp"])
    apt = None
    if proc.returncode != 0 or tcp.returncode != 0:
        apt = run(["apt-get", "install", "-y", "linux-modules-extra-" + os.uname().release], timeout=300)
        proc = run(["modprobe", "nvmet"])
        tcp = run(["modprobe", "nvmet-tcp"])
    if proc.returncode != 0 or tcp.returncode != 0:
        return {
            "skipped": True,
            "reason": "modules nvmet absents",
            "nvmet": (proc.stderr or "")[-200:],
            "nvmet_tcp": (tcp.stderr or "")[-200:],
            "apt_rc": None if apt is None else apt.returncode,
            "apt_err": "" if apt is None else (apt.stderr or apt.stdout or "")[-300:],
        }
    write_json(Path("/run/san/medium-state.json"), {"ok": True, "reason": "mesure"})
    plan = json.loads(Path("/etc/san/network.json").read_text(encoding="utf-8"))
    # Le portail stockage peut être down après le test de bascule. L'adresse est reposée.
    run(["ip", "link", "set", "stor", "up"])
    run(["ip", "addr", "replace", "10.0.0.10/24", "dev", "stor"])
    write_json(Path("/var/lib/san/conf/nvme.json"), {
        "version": 1,
        "network": plan,
        "subsystems": [{
            "nqn": "nqn.2026-11.local.san:nvme",
            "host": "nqn.2026-11.local.client:nvme",
            "allow_any_host": False,
            "portal": {"address": "10.0.0.10", "port": 4420},
            "volume": ids["nvme_volume"],
        }],
    })
    restored = tui("nvme-restore")
    subs = Path("/sys/kernel/config/nvmet/subsystems/nqn.2026-11.local.san:nvme")
    names = sorted(entry.name for entry in subs.iterdir()) if subs.is_dir() else []
    queues = {}
    qpath = Path("/run/san/nvme-queues.json")
    if qpath.is_file():
        queues = json.loads(qpath.read_text(encoding="utf-8"))
    return {
        "restore": restored.get("json"),
        "attributes": names,
        "queues": queues,
        "kernel": os.uname().release,
        "cores": os.cpu_count(),
    }


def image_has_no_key():
    hits = []
    for root in (Path("/repo/live/rootfs"), Path("/repo/image")):
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.name in {"volume.key", "secrets.json"}:
                hits.append(str(path))
    return {"baked_keys": hits}


def main():
    os.makedirs("/var/lib/san/conf", exist_ok=True)
    os.makedirs("/etc/san", exist_ok=True)
    os.makedirs("/usr/share/san", exist_ok=True)
    profile = Path("/repo/live/rootfs/usr/share/san/volume-profiles.json")
    if profile.is_file():
        shutil.copy(profile, "/usr/share/san/volume-profiles.json")
    report = {
        "kernel": os.uname().release,
        "steps": {},
        "image_keys": image_has_no_key(),
    }
    try:
        step(report, "modules", setup_modules)
        step(report, "lacp", lacp_measure)
        plan = build_net()
        build_lvm()
        ids = prepare_exports(plan)
        report["ids"] = {key: value for key, value in ids.items() if key != "secret_sha256"}
        report["ids"]["secret_sha256"] = ids["secret_sha256"]

        def crypt_step():
            return crypt_with_key()

        step(report, "crypt_with_key", crypt_step)
        step(report, "thin", lambda: thin_alert_then_full(ids))
        step(report, "restore_without_key", drop_key_and_restore)
        step(report, "quota", quota_measure)
        step(report, "quota_failure", quota_failure_leaves_iscsi)
        step(report, "snapshot", lambda: snapshot_and_rollback(ids))
        step(report, "clone", clone_measure)
        step(report, "chap", chap_measure)
        step(report, "portal", portal_measure)
        step(report, "pr", pr_measure)
        step(report, "nvme", lambda: nvme_measure(ids))
    except Exception as exc:
        report["error"] = "{}: {}".format(exc.__class__.__name__, exc)
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.sync()
    try:
        fd = os.open("/dev/ttyS0", os.O_WRONLY)
        os.write(fd, b"CAPACITY-DONE\n")
        os.close(fd)
    except OSError:
        pass


if __name__ == "__main__":
    main()
