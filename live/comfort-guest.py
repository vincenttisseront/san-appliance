#!/usr/bin/python3
"""Mesures dans une VM. Chaque section dit ce qui a été observé."""

import hashlib
import json
import os
import pathlib
import subprocess
import time

from san_tui.health import health_check, smartctl_runner
from san_tui.link import CommandRunner, establish_bond, lookup_name, select_mtu
from san_tui.paths import SanPaths
from san_tui.portals import drop_portal_dirs, iser_module_present, modules_dir
from san_tui.raid import (
    REBUILD_CAP_KB,
    SCRUB_CAP_KB,
    apply_raid_comfort,
    apply_sync_cap,
    classify_disks,
    disk_is_blank,
    examine_device,
    request_scrub,
)
from san_tui.runtime import MdadmBackend


REPORT = {"steps": []}


def run(args, check=False, timeout=60, stdin=None):
    return subprocess.run(
        args, text=True, input=stdin, capture_output=True, check=check, timeout=timeout,
    )


def note(name, **fields):
    fields["name"] = name
    REPORT["steps"].append(fields)
    print("STEP " + json.dumps(fields, ensure_ascii=False), flush=True)


def kernel_name(serial):
    for block in pathlib.Path("/sys/block").iterdir():
        for relative in ("serial", "device/serial"):
            path = block / relative
            try:
                if path.is_file() and path.read_text(encoding="utf-8").strip() == serial:
                    return block.name
            except OSError:
                continue
    return ""


def by_id(kernel):
    root = pathlib.Path("/dev/disk/by-id")
    if not root.is_dir():
        return ""
    found = []
    for entry in root.iterdir():
        try:
            if entry.resolve().name == kernel and "-part" not in entry.name:
                found.append(str(entry))
        except OSError:
            continue
    found.sort()
    return found[0] if found else ""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def volume(serial):
    kernel = kernel_name(serial)
    path = "/dev/" + kernel if kernel else ""
    ident = by_id(kernel) if kernel else ""
    size = 0
    if kernel:
        raw = pathlib.Path("/sys/block") / kernel / "size"
        try:
            size = int(raw.read_text(encoding="utf-8").strip()) * 512
        except (OSError, ValueError):
            size = 0
    return {
        "id": ident,
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


class ListProbe:
    def __init__(self, volumes):
        self.volumes = volumes

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, stable_id):
        for item in self.volumes:
            if item["id"] == stable_id or item["id"].endswith("/" + str(stable_id).split("/")[-1]):
                return item
        return None


def md_dir():
    for entry in pathlib.Path("/sys/block").glob("md*"):
        if (entry / "md").is_dir():
            return entry / "md"
    return None


def read_text(path):
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def measure_raid(paths):
    names = ("memA", "memB", "spare", "occupied", "boot01", "exported")
    vols = {serial: volume(serial) for serial in names}
    missing = [serial for serial, item in vols.items() if not item["path"] or not item["id"]]
    if missing:
        note("raid", status="skipped", reason="disques absents : {}".format(", ".join(missing)))
        return
    occupied_path = vols["occupied"]["path"]
    boot_path = vols["boot01"]["path"]
    exported_path = vols["exported"]["path"]
    for serial, path in (
        ("occupied", occupied_path),
        ("boot01", boot_path),
        ("exported", exported_path),
    ):
        fd = os.open(path, os.O_WRONLY)
        try:
            os.write(fd, ("SAN-" + serial).ljust(4096, "\0").encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    before = {
        "occupied": sha256(occupied_path),
        "boot": sha256(boot_path),
        "exported": sha256(exported_path),
    }
    paths.conf.mkdir(parents=True, exist_ok=True)
    (paths.conf / "exports.json").write_text(json.dumps({
        "version": 1,
        "targets": [{"volume": {"id": vols["exported"]["id"]}}],
    }), encoding="utf-8")
    created = run(
        ["mdadm", "--create", "/dev/md/san0", "--level=1", "--raid-devices=2",
         "--metadata=1.2", "--force", "--run", vols["memA"]["id"], vols["memB"]["id"]],
        stdin="yes\n", timeout=60,
    )
    if created.returncode != 0:
        note("raid", status="skipped", reason=(created.stderr or created.stdout or "mdadm")[-300:])
        return
    time.sleep(1)
    classified = classify_disks(
        list(vols.values()),
        {vols["exported"]["id"]},
        {vols["memA"]["id"], vols["memB"]["id"]},
        set(),
        examine_device,
        lambda item: disk_is_blank(item.get("path") or "", item.get("size")),
    )
    report = apply_raid_comfort(
        paths,
        ListProbe(list(vols.values())),
        MdadmBackend(),
        [{"name": "san0", "level": "raid1", "devices": [vols["memA"]["id"], vols["memB"]["id"]]}],
        examine=examine_device,
        blank_of=lambda item: disk_is_blank(item.get("path") or "", item.get("size")),
    )
    directory = md_dir()
    cap = read_text(directory / "sync_speed_max") if directory else ""
    states = []
    if directory:
        for state in directory.glob("dev-*/state"):
            states.append(read_text(state))
    after = {
        "occupied": sha256(occupied_path),
        "boot": sha256(boot_path),
        "exported": sha256(exported_path),
    }
    scrub = False
    action_after = ""
    samples = []
    if directory:
        apply_sync_cap(directory, 1024)
        global_cap = pathlib.Path("/proc/sys/dev/raid/speed_limit_max")
        if global_cap.is_file():
            global_cap.write_text("1024\n", encoding="utf-8")
        action = directory / "sync_action"
        if action.is_file() and read_text(action) == "idle":
            request_scrub(directory)
            apply_sync_cap(directory, 1024)
            action_after = read_text(action)
        for _ in range(8):
            time.sleep(0.4)
            samples.append(read_text(directory / "sync_speed"))
        scrub = action_after == "check"
        if action.is_file():
            action.write_text("idle\n", encoding="utf-8")
    note(
        "raid",
        status="measured",
        spare=report.get("spare"),
        reason=report.get("reason"),
        roles=[{"serial": item.get("serial"), "role": item.get("role")} for item in classified],
        rebuild_cap_read=cap,
        rebuild_cap_expected=str(REBUILD_CAP_KB),
        scrub_cap_constant=SCRUB_CAP_KB,
        member_states=states,
        untouched=before == after,
        check_seen=scrub,
        action_after=action_after,
        sync_speed_samples=samples,
        speed_limit_max=read_text("/proc/sys/dev/raid/speed_limit_max"),
    )


def measure_health(paths):
    canary = paths.conf / "exports.json"
    before = canary.read_bytes() if canary.is_file() else b""
    disk = volume("occupied")
    runner = smartctl_runner()
    if runner is None:
        report = health_check(paths, [disk], None)
        note("smart", status="skipped", reason="smartctl absent", exports_changed=report["exports_changed"])
        return
    raw = runner(disk)
    report = health_check(paths, [disk], runner)
    after = canary.read_bytes() if canary.is_file() else b""
    temp = None
    if isinstance(raw, dict):
        temp = (raw.get("temperature") or {}).get("current")
    note(
        "smart",
        status="measured" if temp is None else "measured",
        temperature=temp,
        smart_passed=(raw.get("smart_status") or {}).get("passed") if isinstance(raw, dict) else None,
        messages=((raw.get("smartctl") or {}).get("messages") if isinstance(raw, dict) else None),
        exports_changed=report["exports_changed"] or before != after,
        reason="capteur absent" if temp is None else "température lue",
    )


def extra_ifaces():
    default = run(["ip", "route", "show", "default"]).stdout.split()
    found = []
    for entry in sorted(pathlib.Path("/sys/class/net").iterdir()):
        if entry.name in {"lo"} or entry.name in default:
            continue
        found.append(entry.name)
    return found


def measure_portals():
    extras = extra_ifaces()[:2]
    if len(extras) < 2:
        note("portals", status="skipped", reason="deux interfaces supplémentaires absentes")
        return
    run(["ip", "link", "set", extras[0], "up"])
    run(["ip", "link", "set", extras[1], "up"])
    run(["ip", "addr", "replace", "10.8.0.10/24", "dev", extras[0]])
    run(["ip", "addr", "replace", "10.9.0.10/24", "dev", extras[1]])
    image = pathlib.Path("/var/tmp/comfort-lun.img")
    image.write_bytes(b"\0" * (32 * 1024 * 1024))
    iqn = "iqn.2026-11.local.san:comfort"
    commands = [
        ["targetcli", "/backstores/fileio", "create", "san", str(image), "32M"],
        ["targetcli", "/iscsi", "create", iqn],
        ["targetcli", "/iscsi/{}/tpg1/portals".format(iqn), "delete", "0.0.0.0", "3260"],
        ["targetcli", "/iscsi/{}/tpg1/portals".format(iqn), "create", "10.8.0.10", "3260"],
        ["targetcli", "/iscsi/{}/tpg1/portals".format(iqn), "create", "10.9.0.10", "3260"],
        ["targetcli", "/iscsi/{}/tpg1/luns".format(iqn), "create", "/backstores/fileio/san"],
        ["targetcli", "/iscsi/{}/tpg1".format(iqn), "enable"],
    ]
    for command in commands:
        proc = run(command, timeout=30)
        if proc.returncode != 0 and "already" not in (proc.stderr or ""):
            note("portals", status="skipped", reason=(proc.stderr or proc.stdout or "targetcli")[-240:])
            return
    config = pathlib.Path("/sys/kernel/config")
    enable = config / "target/iscsi" / iqn / "tpgt_1/enable"
    before = read_text(enable)
    removed = drop_portal_dirs(config, "10.9.0.10")
    kept = config / "target/iscsi" / iqn / "tpgt_1/np/10.8.0.10:3260"
    gone = config / "target/iscsi" / iqn / "tpgt_1/np/10.9.0.10:3260"
    listen = run(["ss", "-H", "-ltn"]).stdout
    refused = drop_portal_dirs(config, "10.8.0.10")
    modules = modules_dir()
    present = iser_module_present(modules)
    iser_detail = "module absent"
    if present:
        proc = run(["modprobe", "ib_isert"])
        iser_detail = "modprobe {}".format(proc.returncode)
        flag = kept / "iser"
        if flag.is_file():
            try:
                flag.write_text("1\n", encoding="utf-8")
                iser_detail = "attribut écrit"
            except OSError as exc:
                iser_detail = "attribut refusé : {}".format(exc.__class__.__name__)
    note(
        "portals",
        status="measured",
        dropped=bool(removed["dropped"]),
        other_remains=kept.is_dir() and not gone.exists(),
        enable_before=before,
        enable_after=read_text(enable),
        listen_keeps_3260=":3260" in listen,
        last_refused=bool(refused["refused"]) and kept.is_dir(),
        iser_present=present,
        iser=iser_detail,
        ifaces=extras,
    )
    return extras


def measure_dns():
    before = lookup_name("10.8.0.10")
    hosts = pathlib.Path("/etc/hosts")
    original = hosts.read_text(encoding="utf-8") if hosts.is_file() else ""
    try:
        with hosts.open("a", encoding="utf-8") as handle:
            handle.write("10.8.0.10 san-portal.example\n")
        named = lookup_name("10.8.0.10")
    finally:
        hosts.write_text(original, encoding="utf-8")
    from san_tui.link import attach_dns
    attached = attach_dns({"address": "10.8.0.10", "port": 3260}, lambda _ip: named)
    note(
        "dns",
        status="measured",
        without_name=before,
        with_name=named,
        address=attached["address"],
        dns=attached["dns"],
    )


def measure_mtu():
    # Le pair est dans un autre espace de noms : sinon l'adresse est locale
    # et le paquet ne traverse pas le MTU du lien.
    run(["ip", "netns", "del", "sanmtu"])
    run(["ip", "link", "del", "veth-a"])
    added = run(["ip", "netns", "add", "sanmtu"])
    if added.returncode != 0:
        note("mtu", status="skipped", reason=(added.stderr or "netns")[-200:])
        return
    linked = run(["ip", "link", "add", "veth-a", "type", "veth", "peer", "name", "veth-b"])
    if linked.returncode != 0:
        note("mtu", status="skipped", reason=(linked.stderr or "veth")[-200:])
        return
    run(["ip", "link", "set", "veth-b", "netns", "sanmtu"])
    run(["ip", "addr", "add", "192.0.2.1/30", "dev", "veth-a"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "addr", "add", "192.0.2.2/30", "dev", "veth-b"])
    run(["ip", "link", "set", "veth-a", "up"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "up"])
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "mtu", "9000"])
    runner = CommandRunner()
    ok = select_mtu("veth-a", "192.0.2.2", runner)
    read_ok = read_text("/sys/class/net/veth-a/mtu")
    run(["ip", "netns", "exec", "sanmtu", "ip", "link", "set", "veth-b", "mtu", "1500"])
    bad = select_mtu("veth-a", "192.0.2.2", runner)
    read_bad = read_text("/sys/class/net/veth-a/mtu")
    run(["ip", "netns", "del", "sanmtu"])
    run(["ip", "link", "del", "veth-a"])
    note(
        "mtu",
        status="measured",
        df_ok=ok,
        mtu_after_ok=read_ok,
        df_fail=bad,
        mtu_after_fail=read_bad,
    )


def measure_bond(ifaces):
    if not ifaces or len(ifaces) < 2:
        note("bond", status="skipped", reason="interfaces absentes")
        return
    for name in ifaces:
        run(["ip", "addr", "flush", "dev", name])
    runner = CommandRunner()
    if not runner.module_present():
        note("bond", status="skipped", reason="module bonding absent")
        return
    decision = establish_bond(ifaces, runner)
    text = read_text("/proc/net/bonding/{}".format(decision.get("iface") or "sanstor"))
    note(
        "bond",
        status="measured",
        decision=decision,
        proc=text.splitlines()[:8],
    )
    try:
        runner.delete("sanstor")
    except Exception:
        pass


def main():
    paths = SanPaths("/var/tmp/san-comfort-root")
    paths.ensure_runtime()
    paths.conf.mkdir(parents=True, exist_ok=True)
    try:
        measure_mtu()
    except Exception as exc:
        note("mtu", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:180])
    try:
        measure_raid(paths)
    except Exception as exc:
        note("raid", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:240])
    try:
        measure_health(paths)
    except Exception as exc:
        note("smart", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:180])
    extras = []
    try:
        extras = measure_portals() or []
    except Exception as exc:
        note("portals", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:240])
    try:
        measure_dns()
    except Exception as exc:
        note("dns", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:180])
    try:
        measure_bond(extras)
    except Exception as exc:
        note("bond", status="skipped", reason=exc.__class__.__name__ + " " + str(exc)[:240])
    out = pathlib.Path("/mnt/out/comfort-result.json")
    if not out.parent.is_dir():
        out = pathlib.Path("/var/tmp/comfort-result.json")
    out.write_text(json.dumps(REPORT, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("SAN-RESULT " + json.dumps(REPORT, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
