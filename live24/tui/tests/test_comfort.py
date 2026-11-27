"""Choix automatiques. Aucun disque physique ni LIO du noyau hôte."""

import json
import tempfile
import unittest
from pathlib import Path

from san_tui.comfort import drop_absent_portals, run_health, undo_spare
from san_tui.health import DISK_TEMP_ALERT_C, health_check, judge_disk
from san_tui.link import (
    DF_PAYLOAD,
    attach_dns,
    bond_groups,
    choose_bond_mode,
    configure_links,
    establish_bond,
    lacp_partner_seen,
    name_from_hosts,
    peer_from_route,
    select_mtu,
)
from san_tui.paths import SanPaths
from san_tui.portals import (
    bind_address,
    drop_portal_dirs,
    iser_module_present,
    plan_drop,
    serving_portals,
)
from san_tui.raid import (
    REBUILD_CAP_KB,
    SCRUB_CAP_KB,
    RecordingMd,
    apply_raid_comfort,
    choose_spare,
    classify_disks,
    disk_is_blank,
    examine_text,
    pace_cap,
    request_scrub,
    role_of,
)
from san_tui.ui import screen_lines


def link_volume(name="wwn-free", **overrides):
    data = {
        "id": "/dev/disk/by-id/" + name,
        "kernel": name,
        "path": "/dev/" + name,
        "wwn": "naa." + name,
        "serial": name,
        "model": "TEST",
        "size": 8 * 1024 * 1024,
        "block_size": 512,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": False,
        "kind": "block",
    }
    data.update(overrides)
    return data


class LinkProbe:
    def __init__(self, volumes, addresses=None):
        self.volumes = list(volumes)
        self.addresses = addresses or {"enp2s0": ["10.0.0.10/24"], "enp1s0": ["192.168.1.10/24"]}

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, stable_id):
        needle = str(stable_id).split("/")[-1]
        for item in self.volumes:
            if item["id"] == stable_id or item["id"].endswith("/" + needle):
                return item
        return None

    def list_interfaces(self):
        return [
            {"name": name, "addresses": addrs, "operstate": "up"}
            for name, addrs in self.addresses.items()
        ]


PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}


class FakeRunner:
    def __init__(self, ping_ok=False, set_ok=True, partner="00:00:00:00:00:00", present=True, peer="10.0.0.1"):
        self.ping_ok = ping_ok
        self.set_ok = set_ok
        self.partner = partner
        self.present = present
        self.peer = peer
        self.mtus = []
        self.pings = []
        self.mode = ""
        self.slaves = []
        self.link_is_up = False
        self.created = ""
        self.deleted = False
        self.addrs = []
        self.fail_create = False

    def set_mtu(self, iface, mtu):
        self.mtus.append((iface, int(mtu)))
        if int(mtu) == 9000 and not self.set_ok:
            return False
        return True

    def ping_df(self, iface, peer, size):
        self.pings.append((iface, peer, int(size)))
        return self.ping_ok

    def module_present(self):
        return self.present

    def create(self, name):
        if self.fail_create:
            raise RuntimeError("bond refusé")
        self.created = name

    def set_mode(self, name, mode):
        if self.slaves and self.link_is_up:
            raise RuntimeError("mode refusé")
        self.mode = mode

    def enslave(self, bond, iface):
        self.slaves.append(iface)

    def release_all(self, bond):
        self.slaves = []

    def up(self, name):
        self.link_is_up = True

    def down(self, name):
        self.link_is_up = False

    def link_up(self, name):
        self.link_is_up = True

    def status(self, name):
        return "Bonding Mode: {}\nPartner Mac Address: {}\n".format(self.mode, self.partner)

    def wait_lacp(self):
        return None

    def delete(self, name):
        self.deleted = True
        self.slaves = []
        self.link_is_up = False

    def add_addr(self, iface, address):
        self.addrs.append((iface, address))

    def route_peer(self, iface):
        return self.peer


class ClassTests(unittest.TestCase):
    def test_spare_comes_only_from_a_disk_already_free(self):
        boot = link_volume("boot", boot=True, size=9)
        exported = link_volume("exported", size=9)
        member = link_volume("member", size=9)
        occupied = link_volume("occupied", size=9)
        small = link_volume("small", size=4)
        blank = link_volume("blank", size=9)
        foreign = link_volume("foreign", size=9)
        classified = classify_disks(
            [boot, exported, member, occupied, small, blank, foreign],
            exported={exported["id"]},
            members={member["id"]},
            support=set(),
            examine=lambda item: "foreign" if item["id"] == foreign["id"] else "plain",
            blank_of=lambda item: item["id"] == blank["id"],
        )
        roles = {item["id"]: item["role"] for item in classified}
        self.assertEqual(roles[boot["id"]], "boot")
        self.assertEqual(roles[exported["id"]], "exported")
        self.assertEqual(roles[member["id"]], "member")
        self.assertEqual(roles[occupied["id"]], "occupied")
        self.assertEqual(roles[foreign["id"]], "foreign")
        self.assertEqual(roles[blank["id"]], "free")
        chosen = choose_spare(classified, {exported["id"]}, {member["id"]}, min_size=8)
        self.assertEqual(chosen["id"], blank["id"])
        lied = [dict(boot, role="free"), dict(exported, role="free"), blank]
        self.assertIsNone(choose_spare(lied, {exported["id"]}, set(), min_size=1))

    def test_comfort_adds_the_free_disk_and_caps_rebuild(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        (paths.conf).mkdir(parents=True)
        first = link_volume("a", kernel="sdb", path="/dev/sdb")
        second = link_volume("b", kernel="sdc", path="/dev/sdc", serial="B", wwn="naa.b")
        spare = link_volume("c", kernel="sdd", path="/dev/sdd", serial="C", wwn="naa.c", size=first["size"])
        boot = link_volume("boot", boot=True, size=first["size"] * 2)
        exported = link_volume("exp", size=first["size"] * 2)
        (paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{"volume": {"id": exported["id"]}}],
        }), encoding="utf-8")
        probe = LinkProbe([first, second, spare, boot, exported])
        backend = RecordingMd()
        report = apply_raid_comfort(
            paths, probe, backend,
            [{"name": "san0", "level": "raid1", "devices": [first["id"], second["id"]]}],
            examine=lambda item: "plain",
            blank_of=lambda item: item["id"] == spare["id"],
        )
        self.assertEqual(backend.spares, [("san0", spare["id"])])
        self.assertEqual(backend.speeds, [("san0", REBUILD_CAP_KB)])
        self.assertEqual(report["spare"]["id"], spare["id"])
        self.assertNotIn(boot["id"], [item[1] for item in backend.spares])
        self.assertNotIn(exported["id"], [item[1] for item in backend.spares])
        undone = undo_spare(paths, backend)
        self.assertTrue(undone["removed"])
        self.assertEqual(backend.removed, [("san0", spare["id"])])

    def test_blank_and_examine_text(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "disk"
        path.write_bytes(b"\0" * 128)
        self.assertTrue(disk_is_blank(str(path), 128))
        path.write_bytes(b"\0" * 32 + b"x" + b"\0" * 32)
        self.assertFalse(disk_is_blank(str(path), path.stat().st_size))
        self.assertEqual(examine_text(1, "mdadm: No md superblock detected"), "plain")
        self.assertEqual(examine_text(0, ""), "plain")
        self.assertEqual(examine_text(0, "ARRAY /dev/md/foreign"), "foreign")
        self.assertEqual(examine_text(1, "mdadm: cannot open /dev/gone: No such file"), "unknown")
        self.assertEqual(role_of(link_volume(), set(), set(), set(), "plain", True), "free")


class PaceTests(unittest.TestCase):
    def test_scrub_cap_is_lower_and_does_not_invent_an_array(self):
        self.assertEqual(pace_cap("check"), SCRUB_CAP_KB)
        self.assertEqual(pace_cap("recover"), REBUILD_CAP_KB)
        self.assertLess(SCRUB_CAP_KB, REBUILD_CAP_KB)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        missing = Path(tmp.name) / "md0" / "md"
        self.assertFalse(request_scrub(missing))
        self.assertFalse(missing.exists())
        md_dir = Path(tmp.name) / "block" / "md0" / "md"
        md_dir.mkdir(parents=True)
        (md_dir / "sync_action").write_text("idle\n", encoding="utf-8")
        (md_dir / "sync_speed_max").write_text("200000\n", encoding="utf-8")
        self.assertTrue(request_scrub(md_dir))
        self.assertEqual((md_dir / "sync_action").read_text(encoding="utf-8").strip(), "check")
        self.assertEqual(int((md_dir / "sync_speed_max").read_text(encoding="utf-8")), SCRUB_CAP_KB)


class HealthTests(unittest.TestCase):
    def test_temperature_alerts_without_touching_exports(self):
        self.assertEqual(DISK_TEMP_ALERT_C, 55)
        cool = judge_disk({"smart_status": {"passed": True}, "temperature": {"current": 55}}, "disk")
        self.assertEqual(cool["alerts"], [])
        self.assertFalse(cool["exports_changed"])
        hot = judge_disk({"temperature": {"current": 56}}, "disk")
        self.assertIn("56°C", hot["alerts"][0][1])
        failed = judge_disk({"smart_status": {"passed": False}}, "disk")
        self.assertIn("SMART en échec", failed["alerts"][0][1])
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        canary = paths.conf / "exports.json"
        canary.write_text('{"version":1,"targets":[]}\n', encoding="utf-8")
        before = canary.read_bytes()

        def run(_volume):
            return {"smart_status": {"passed": False}, "temperature": {"current": 70}}

        report = health_check(paths, [link_volume()], run)
        self.assertFalse(report["exports_changed"])
        self.assertEqual(canary.read_bytes(), before)
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("SMART en échec", text)
        self.assertIn("70°C", text)
        skipped = health_check(paths, [link_volume()], None)
        self.assertEqual(skipped["skipped"], "smartctl absent")
        self.assertFalse(skipped["exports_changed"])
        self.assertEqual(canary.read_bytes(), before)

    def test_screen_shows_smart_and_keeps_the_ip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.ensure_runtime()
        disk = link_volume()
        (paths.run / "health.json").write_text(json.dumps({
            "exports_changed": False,
            "disks": {disk["id"]: {"passed": False, "temperature": 61}},
        }), encoding="utf-8")
        (paths.conf).mkdir(parents=True)
        (paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{
                "iqn": "iqn.2026-11.local:disk",
                "lun": 0,
                "portal": {"address": "10.0.0.10", "port": 3260},
                "volume": {"id": disk["id"]},
                "acls": [{"initiator": "iqn.2026-11.local:init", "chap_user": "client"}],
            }],
        }), encoding="utf-8")
        (paths.run / "portals.json").write_text(json.dumps({
            "iqn.2026-11.local:disk": [{"address": "10.0.0.10", "port": 3260, "dns": "san.example"}],
        }), encoding="utf-8")
        disks = "\n".join(screen_lines("Disques", {"disks": [disk], "interfaces": []}, paths))
        self.assertIn("SMART échec", disks)
        self.assertIn("61°C", disks)
        exports = "\n".join(screen_lines("Exports iSCSI", {"disks": [], "interfaces": []}, paths))
        self.assertIn("10.0.0.10:3260 (san.example)", exports)
        self.assertNotIn("portail san.example", exports)


class PortalTests(unittest.TestCase):
    def test_drop_one_portal_keeps_the_other_and_the_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        enable = root / "target/iscsi/iqn.2026-11.local:disk/tpgt_1/enable"
        first = enable.parent / "np/10.0.0.10:3260"
        second = enable.parent / "np/10.2.0.10:3260"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        enable.write_text("1\n", encoding="utf-8")
        plan = plan_drop(
            [{"address": "10.0.0.10"}, {"address": "10.2.0.10"}],
            ["10.2.0.10"],
        )
        self.assertEqual([item["address"] for item in plan["keep"]], ["10.0.0.10"])
        removed = drop_portal_dirs(root, "10.2.0.10")
        self.assertTrue(removed["dropped"])
        self.assertFalse(second.exists())
        self.assertTrue(first.is_dir())
        self.assertEqual(enable.read_text(encoding="utf-8").strip(), "1")
        refused = drop_portal_dirs(root, "10.0.0.10")
        self.assertTrue(refused["refused"])
        self.assertTrue(first.is_dir())
        self.assertEqual(enable.read_text(encoding="utf-8").strip(), "1")
        last = plan_drop([{"address": "10.0.0.10"}], ["10.0.0.10"])
        self.assertEqual(last["drop"], [])
        self.assertEqual(last["reason"], "dernier portail conservé")

    def test_absent_address_is_removed_and_admin_is_not_a_portal(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        (paths.conf / "exports.json").write_text(json.dumps({"network": PLAN}), encoding="utf-8")
        root = Path(tmp.name) / "config"
        enable = root / "target/iscsi/iqn.2026-11.local:disk/tpgt_1/enable"
        (enable.parent / "np/10.0.0.10:3260").mkdir(parents=True)
        (enable.parent / "np/10.2.0.10:3260").mkdir(parents=True)
        enable.write_text("1\n", encoding="utf-8")
        probe = LinkProbe([], {
            "enp1s0": ["192.168.1.10/24"],
            "enp2s0": ["10.0.0.10/24"],
        })
        portals = serving_portals({"portal": {"port": 3260}}, PLAN, probe, extras=True)
        self.assertEqual([item["address"] for item in portals], ["10.0.0.10"])
        self.assertNotIn("192.168.1.10", [item["address"] for item in portals])
        probe.addresses["enp3s0"] = ["10.2.0.10/24"]
        report = drop_absent_portals(paths, probe, root)
        self.assertEqual(report["dropped"], [])
        probe.addresses.pop("enp3s0")
        report = drop_absent_portals(paths, probe, root)
        self.assertTrue(report["dropped"])
        self.assertFalse((enable.parent / "np/10.2.0.10:3260").exists())
        self.assertTrue((enable.parent / "np/10.0.0.10:3260").is_dir())
        self.assertEqual(enable.read_text(encoding="utf-8").strip(), "1")

    def test_iser_requires_the_module_file_and_dns_keeps_the_ip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.assertFalse(iser_module_present(root))
        module = root / "kernel/drivers/infiniband/ib_isert.ko.zst"
        module.parent.mkdir(parents=True)
        module.write_bytes(b"")
        self.assertTrue(iser_module_present(root))
        self.assertEqual(bind_address({"address": "10.0.0.10", "dns": "san.example"}), "10.0.0.10")
        with self.assertRaises(ValueError):
            bind_address({"address": "san.example", "dns": "san.example"})
        named = attach_dns({"address": "10.0.0.10", "port": 3260}, lambda ip: "san.example")
        self.assertEqual(named["address"], "10.0.0.10")
        self.assertEqual(named["dns"], "san.example")
        silent = attach_dns({"address": "10.0.0.10"}, lambda ip: "")
        self.assertEqual(silent["address"], "10.0.0.10")
        self.assertEqual(silent["dns"], "")
        failed = attach_dns({"address": "10.0.0.10"}, lambda ip: (_ for _ in ()).throw(OSError("dns")))
        self.assertEqual(failed["dns"], "")
        self.assertEqual(failed["address"], "10.0.0.10")
        self.assertEqual(
            name_from_hosts("10.8.0.10", "10.8.0.10 comfort\n", {"comfort"}),
            "",
        )
        self.assertEqual(
            name_from_hosts("10.8.0.10", "10.8.0.10 san-portal.example\n", {"comfort"}),
            "san-portal.example",
        )


class LinkTests(unittest.TestCase):
    def test_jumbo_stays_at_1500_when_the_df_packet_fails(self):
        runner = FakeRunner(ping_ok=False, peer="10.0.0.1")
        result = select_mtu("enp2s0", "10.0.0.1", runner)
        self.assertEqual(result["mtu"], 1500)
        self.assertEqual(runner.pings, [("enp2s0", "10.0.0.1", DF_PAYLOAD)])
        self.assertEqual(runner.mtus[0], ("enp2s0", 9000))
        self.assertEqual(runner.mtus[-1], ("enp2s0", 1500))
        refused = FakeRunner(set_ok=False)
        self.assertEqual(select_mtu("enp2s0", "10.0.0.1", refused)["mtu"], 1500)
        self.assertNotIn(("enp2s0", 9000), [item for item in refused.mtus if item[1] == 9000 and refused.set_ok])
        alone = FakeRunner()
        self.assertEqual(select_mtu("enp2s0", "", alone)["mtu"], 1500)
        self.assertFalse(alone.pings)
        self.assertNotIn(9000, [mtu for _iface, mtu in alone.mtus])
        self.assertIn("10.0.0.1", peer_from_route("default via 10.0.0.1 dev enp2s0", "enp2s0"))
        self.assertEqual(peer_from_route("10.0.0.0/24 dev enp2s0", "enp2s0"), "")

    def test_no_lacp_partner_selects_active_backup(self):
        self.assertFalse(lacp_partner_seen("Partner Mac Address: 00:00:00:00:00:00\n"))
        self.assertTrue(lacp_partner_seen("Partner Mac Address: 52:54:00:aa:00:02\n"))
        self.assertEqual(choose_bond_mode(False), "active-backup")
        self.assertEqual(choose_bond_mode(True), "802.3ad")
        quiet = FakeRunner(partner="00:00:00:00:00:00")
        decision = establish_bond(["enp2s0", "enp3s0"], quiet)
        self.assertEqual(decision["mode"], "active-backup")
        self.assertEqual(quiet.mode, "active-backup")
        self.assertEqual(quiet.slaves, ["enp2s0", "enp3s0"])
        partner = FakeRunner(partner="52:54:00:aa:00:02")
        self.assertEqual(establish_bond(["enp2s0", "enp3s0"], partner)["mode"], "802.3ad")
        self.assertEqual(partner.mode, "802.3ad")
        missing = FakeRunner(present=False)
        skipped = establish_bond(["enp2s0", "enp3s0"], missing)
        self.assertEqual(skipped["iface"], "")
        self.assertIn("absent", skipped["reason"])
        self.assertEqual(missing.created, "")
        broken = FakeRunner()
        broken.fail_create = True
        failed = establish_bond(["enp2s0", "enp3s0"], broken)
        self.assertEqual(failed["iface"], "")
        self.assertTrue(broken.deleted)

    def test_address_is_still_assigned_when_bonding_is_absent(self):
        runner = FakeRunner(present=False, peer="")
        interfaces = [
            {"name": "enp1s0", "addresses": []},
            {"name": "enp2s0", "addresses": []},
            {"name": "enp3s0", "addresses": []},
        ]
        groups = bond_groups(interfaces, PLAN)
        self.assertEqual(groups["bond"], ["enp2s0", "enp3s0"])
        self.assertNotIn("enp1s0", groups["bond"])
        result = configure_links(PLAN, runner, interfaces, bond=True, jumbo=True)
        self.assertEqual(result["storage"], "enp2s0")
        self.assertEqual(result["mtu"], 1500)
        self.assertIn(("enp2s0", "10.0.0.10/24"), runner.addrs)
        self.assertIn(("enp1s0", "192.168.1.10/24"), runner.addrs)

    def test_health_command_wrapper_does_not_need_a_backend(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        report = run_health(paths, LinkProbe([link_volume()]))
        self.assertFalse(report["exports_changed"])


"""Exécute les dix comportements. Aucun disque physique, aucun module du noyau."""

import hashlib
import hmac
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path

from san_tui.audit import canonical, prepare
from san_tui.cli import main
from san_tui.configio import (
    factory_reset,
    import_config,
    read_builtin_export,
    unseal,
)
from san_tui.comfort import run
from san_tui.groupsnap import snapshot_groups
from san_tui.inventory_api import InventoryError, bind_server
from san_tui.iscsi import add_export, plan_export, portal_guards, restore
from san_tui.latency import check, check_from_disk, threshold_us
from san_tui.metrics import MetricsError, _bind, metrics_host, open_metrics
from san_tui.paths import SanPaths
from san_tui.persist import PersistError, load_exports, save_exports, select_support
from san_tui.probe import SysfsProbe
from san_tui.qos import apply_profiles, plan_profile, queue_depth
from san_tui.replicate import device_read, open_socket, replicate_once, serve_once
from san_tui.retire import classify, linux_retire, retire_auto, undo
from san_tui.ui import SCREENS
from san_tui.witness import decide, evaluate


SECRET = "S3cret-CHAP-991"
INITIATOR = "iqn.2026-11.local.client:host"
PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}


def volume(name="wwn-0x5000", **overrides):
    data = {
        "id": "/dev/disk/by-id/" + name,
        "kernel": "sdb",
        "path": "/dev/sdb",
        "wwn": "naa.5000cca",
        "serial": "SER123",
        "model": "TEST",
        "size": 4096,
        "block_size": 512,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": False,
        "kind": "block",
    }
    data.update(overrides)
    return data


class Probe:
    def __init__(self, volumes, addresses=None):
        self.volumes = list(volumes)
        self.addresses = addresses or {"enp2s0": ["10.0.0.10/24"], "enp1s0": ["192.168.1.10/24"]}

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, stable_id):
        if not stable_id:
            return None
        needle = str(stable_id).split("/")[-1]
        for item in self.volumes:
            if item["id"] == stable_id or item["id"].endswith("/" + needle):
                return item
        return None

    def list_interfaces(self):
        return [{"name": name, "addresses": addrs} for name, addrs in self.addresses.items()]

    def interface_has_address(self, iface, address):
        return address in [item.split("/", 1)[0] for item in self.addresses.get(iface, [])]


class Store:
    def __init__(self):
        self.applied = []

    def apply(self, target, secret, probe):
        self.applied.append((target, secret))


def root():
    tmp = tempfile.TemporaryDirectory()
    paths = SanPaths(tmp.name)
    return tmp, paths


class ReplicationTests(unittest.TestCase):
    def test_absent_peer_does_not_open_a_link(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        opened = []
        report = replicate_once(paths, device_read, lambda peer, lun: opened.append(peer), [])
        self.assertEqual(report["reason"], "aucun pair")
        self.assertEqual(opened, [])
        _write(paths.conf / "peer.json", {"version": 1, "address": "0.0.0.0", "port": 9730})
        report = replicate_once(paths, device_read, lambda peer, lun: opened.append(peer), [volume()])
        self.assertEqual(report["reason"], "aucun pair")
        self.assertEqual(opened, [])

    def test_resume_after_the_link_drops(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        payload = b"0123456789" * 50
        lun_path = Path(tmp.name) / "lun.bin"
        lun_path.write_bytes(payload)
        store = {}
        box = {}
        port = []
        ready = threading.Event()

        def peer():
            try:
                server = socket.socket()
                server.settimeout(3)
                server.bind(("127.0.0.1", 0))
                server.listen(2)
                port.append(server.getsockname()[1])
                ready.set()
                first, _addr = server.accept()
                try:
                    header = b""
                    while b"\n" not in header:
                        piece = first.recv(1)
                        if not piece:
                            break
                        header += piece
                    length = int(header.decode().split(" ", 3)[2])
                    got = b""
                    while len(got) < length:
                        piece = first.recv(length - len(got))
                        if not piece:
                            break
                        got += piece
                finally:
                    first.close()
                second, _addr = server.accept()
                try:
                    serve_once(second, store)
                finally:
                    second.close()
                server.close()
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=peer)
        thread.start()
        self.assertTrue(ready.wait(2))
        _write(paths.conf / "peer.json", {"version": 1, "address": "127.0.0.1", "port": port[0]})
        lun = {"id": "lun-1", "path": str(lun_path)}
        first = replicate_once(paths, device_read, open_socket, [lun])
        second = replicate_once(paths, device_read, open_socket, [lun])
        thread.join(3)
        self.assertNotIn("error", box)
        self.assertEqual(first["luns"][0]["action"], "resume")
        self.assertEqual(first["luns"][0]["offset"], 0)
        self.assertEqual(second["luns"][0]["action"], "synced")
        self.assertEqual(second["luns"][0]["offset"], len(payload))
        self.assertEqual(store["data"], payload)
        state = json.loads((paths.run / "replicate-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["lun-1"]["offset"], len(payload))

    def test_hold_and_boot_disk_are_not_sent(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        _write(paths.conf / "peer.json", {"version": 1, "address": "127.0.0.1", "port": 9})
        (paths.run).mkdir(parents=True, exist_ok=True)
        (paths.run / "replicate.hold").write_text("suspendu\n", encoding="utf-8")
        opened = []
        held = replicate_once(paths, device_read, lambda peer, lun: opened.append(1), [volume()])
        self.assertEqual(held["reason"], "réplication suspendue")
        (paths.run / "replicate.hold").unlink()
        boot = replicate_once(
            paths, device_read, lambda peer, lun: opened.append(1), [volume(boot=True)],
        )
        self.assertEqual(boot["reason"], "aucun LUN")
        self.assertEqual(opened, [])


class WitnessTests(unittest.TestCase):
    def test_decision_uses_only_the_witness_word(self):
        self.assertEqual(decide("a", None)["action"], "stay")
        self.assertEqual(decide("a", None)["slot"], "a")
        self.assertEqual(decide("a", "local")["slot"], "a")
        self.assertEqual(decide("a", "quorum")["action"], "stay")
        moved = decide("a", "other")
        self.assertEqual(moved["slot"], "b")
        self.assertEqual(moved["local"], "a")

    def test_silence_keeps_the_local_slot(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        _write(paths.conf / "ab.json", {"version": 1, "active": "a", "slots": {"a": {}, "b": {}}})
        (paths.conf / "boot-slot").write_text("b\n", encoding="utf-8")
        port = self._serve(b"", delay=1)
        _write(paths.conf / "witness.json", {"version": 1, "address": "127.0.0.1", "port": port})
        decision = evaluate(paths, timeout=0.2)
        self.assertEqual(decision["action"], "stay")
        self.assertEqual(decision["slot"], "a")
        self.assertEqual(decision["reason"], "témoin muet")
        active = json.loads((paths.conf / "ab.json").read_text(encoding="utf-8"))["active"]
        self.assertEqual(active, "a")
        self.assertEqual((paths.conf / "boot-slot").read_text(encoding="utf-8").strip(), "a")
        self.assertIn("slot local", (paths.run / "alerts.json").read_text(encoding="utf-8"))

    def test_other_moves_only_when_the_slot_exists(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        _write(paths.conf / "ab.json", {
            "version": 1,
            "active": "a",
            "slots": {"a": {"id": "slot-a"}, "b": {"id": "slot-b"}},
        })
        port = self._serve(b"other\n")
        _write(paths.conf / "witness.json", {"version": 1, "address": "127.0.0.1", "port": port})
        decision = evaluate(paths, timeout=1)
        self.assertEqual(decision["action"], "failover")
        self.assertEqual(decision["slot"], "b")
        active = json.loads((paths.conf / "ab.json").read_text(encoding="utf-8"))["active"]
        self.assertEqual(active, "b")
        self.assertEqual((paths.conf / "boot-slot").read_text(encoding="utf-8").strip(), "b")

    def test_missing_pair_does_not_invent_a_slot(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        _write(paths.conf / "ab.json", {"version": 1, "active": "a"})
        port = self._serve(b"other\n")
        _write(paths.conf / "witness.json", {"version": 1, "address": "127.0.0.1", "port": port})
        decision = evaluate(paths, timeout=1)
        self.assertEqual(decision["action"], "stay")
        self.assertEqual(decision["reason"], "slot pair absent")
        self.assertEqual(json.loads((paths.conf / "ab.json").read_text(encoding="utf-8"))["active"], "a")

    def test_no_witness_file_stays_local(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        paths.conf.mkdir(parents=True)
        decision = evaluate(paths)
        self.assertEqual(decision["reason"], "aucun témoin")
        self.assertEqual(decision["slot"], "a")

    def _serve(self, reply, delay=0):
        port = []
        ready = threading.Event()

        def run():
            server = socket.socket()
            server.settimeout(3)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port.append(server.getsockname()[1])
            ready.set()
            try:
                conn, _addr = server.accept()
            except socket.timeout:
                server.close()
                return
            try:
                if delay:
                    import time
                    time.sleep(delay)
                else:
                    data = b""
                    while b"\n" not in data:
                        piece = conn.recv(1)
                        if not piece:
                            break
                        data += piece
                    if reply:
                        conn.sendall(reply)
            finally:
                conn.close()
                server.close()

        thread = threading.Thread(target=run)
        thread.start()
        self.addCleanup(lambda: thread.join(3))
        self.assertTrue(ready.wait(2))
        return port[0]


class GroupTests(unittest.TestCase):
    def test_one_instant_for_the_shared_group_only(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        first = Path(tmp.name) / "a.bin"
        second = Path(tmp.name) / "b.bin"
        lone = Path(tmp.name) / "c.bin"
        first.write_bytes(b"alpha")
        second.write_bytes(b"beta")
        lone.write_bytes(b"lone")
        calls = {"n": 0}

        def now():
            calls["n"] += 1
            return "T{}".format(calls["n"])

        volumes = [
            {"id": "id-a", "path": str(first), "group_id": "db"},
            {"id": "id-b", "path": str(second), "group_id": "db"},
            {"id": "id-c", "path": str(lone), "group_id": "other"},
            {"id": "id-boot", "path": str(first), "group_id": "db", "boot": True},
        ]
        before = b""
        report = snapshot_groups(paths, volumes, now=now)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(report["groups"][0]["instant"], "T1")
        self.assertEqual(report["groups"][0]["members"], ["id-a", "id-b"])
        copies = list((paths.run / "snapshots" / "db" / "T1").iterdir())
        self.assertEqual({item.read_bytes() for item in copies}, {b"alpha", b"beta"})
        self.assertFalse((paths.run / "snapshots" / "other").exists())
        self.assertEqual(before, b"")

    def test_group_file_on_disk_and_a_failed_member_is_discarded(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        first = Path(tmp.name) / "a.bin"
        second = Path(tmp.name) / "b.bin"
        first.write_bytes(b"alpha")
        second.write_bytes(b"beta")
        volumes = [
            {"id": "id-a", "path": str(first)},
            {"id": "id-b", "path": str(second)},
        ]
        _write(paths.conf / "groups.json", {
            "version": 1,
            "members": [
                {"id": "id-a", "group": "pay"},
                {"id": "id-b", "group": "pay"},
            ],
        })

        def take(member, instant, staging):
            from san_tui.groupsnap import file_take
            file_take(member, instant, staging)
            if member["id"] == "id-b":
                raise RuntimeError("coupé")

        report = snapshot_groups(paths, volumes, take=take, now=lambda: "T9")
        self.assertEqual(report["action"], "skip")
        self.assertFalse((paths.run / "snapshots" / "pay" / "T9").exists())


class QosTests(unittest.TestCase):
    def test_initiator_cap_is_clamped_to_the_lun_quota(self):
        profile = {
            "quota": {"iops": 1000, "bps": 100},
            "initiators": {INITIATOR: {"iops": 5000, "bps": 50}},
        }
        plan = plan_profile(profile)
        self.assertEqual(plan["quota"], {"iops": 1000, "bps": 100})
        self.assertEqual(plan["initiators"][INITIATOR], {"iops": 1000, "bps": 50})
        self.assertEqual(queue_depth(1000), 7)
        self.assertIsNone(plan_profile({}))

    def test_profile_writes_queue_depth_and_adds_no_screen(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        disk = volume()
        _write(paths.conf / "volume-profile.json", {
            "version": 1,
            "volumes": {disk["id"]: {
                "quota": {"iops": 1000, "bps": 100},
                "initiators": {INITIATOR: {"iops": 5000, "bps": 50}},
            }},
        })
        sysfs = Path(tmp.name) / "sys"
        queue = sysfs / "block" / "sdb" / "queue"
        queue.mkdir(parents=True)
        (queue / "nr_requests").write_text("128\n", encoding="utf-8")
        report = apply_profiles(paths, [disk], sysfs)
        self.assertEqual(report["action"], "applied")
        written = report["volumes"][0]
        self.assertEqual(written["initiators"][INITIATOR]["iops"], 1000)
        self.assertTrue(written["queue_written"])
        self.assertEqual((queue / "nr_requests").read_text(encoding="utf-8").strip(), "7")
        self.assertEqual(SCREENS, (
            "Disques", "Interfaces", "Réseau admin", "Réseau stockage", "Exports iSCSI", "Alertes",
        ))
        extra = tempfile.TemporaryDirectory()
        self.addCleanup(extra.cleanup)
        self.assertEqual(apply_profiles(SanPaths(extra.name), [disk])["action"], "skip")


class MaskTests(unittest.TestCase):
    def test_portal_stays_closed_without_an_acl(self):
        guards = portal_guards()
        self.assertEqual(guards["generate_node_acls"], 0)
        self.assertEqual(guards["authentication"], 1)
        self.assertEqual(guards["demo_mode_write_protect"], 1)
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        disk = volume()
        probe = Probe([disk])
        select_support(paths, "conf", disk, disk["id"])
        target = {
            "iqn": "iqn.2026-11.local.san:disk",
            "portal": {"address": "10.0.0.10", "port": 3260},
            "lun": 0,
            "volume": {
                "id": disk["id"],
                "wwn": disk["wwn"],
                "serial": disk["serial"],
                "size": disk["size"],
                "block_size": disk["block_size"],
                "kind": "block",
            },
            "acls": [{"initiator": "*", "chap_user": "client1", "chap_secret_ref": "x"}],
        }
        save_exports(paths, {"version": 1, "network": PLAN, "targets": [target]}, {"x": SECRET})
        backend = Store()
        report = restore(paths, probe, backend)
        self.assertEqual(backend.applied, [])
        self.assertEqual(report["skipped"][0]["reason"], "aucune ACL")

    def test_empty_lun_reuses_the_first_initiator(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        first = volume()
        second = volume("wwn-0x6000", kernel="sdc", path="/dev/sdc", wwn="naa.6000", serial="OTHER")
        probe = Probe([first, second])
        select_support(paths, "conf", first, first["id"])
        planned = plan_export(
            first, {"address": "10.0.0.10"}, "iqn.2026-11.local.san:one", INITIATOR, "client1",
        )
        add_export(paths, PLAN, planned, SECRET, first["id"])
        extra = plan_export(
            second, {"address": "10.0.0.10", "port": 3261},
            "iqn.2026-11.local.san:two", INITIATOR, "client1",
        )
        extra["acls"] = []
        document, secrets = load_exports(paths)
        document["targets"].append(extra)
        save_exports(paths, document, secrets)
        before = (paths.conf / "exports.json").read_bytes()
        backend = Store()
        report = restore(paths, probe, backend)
        self.assertEqual([item[0]["iqn"] for item in backend.applied], [
            "iqn.2026-11.local.san:one",
            "iqn.2026-11.local.san:two",
        ])
        self.assertEqual(backend.applied[1][0]["acls"][0]["initiator"], INITIATOR)
        self.assertEqual(backend.applied[1][1], SECRET)
        self.assertEqual(report["skipped"], [])
        self.assertEqual((paths.conf / "exports.json").read_bytes(), before)


class AuditTests(unittest.TestCase):
    def test_signed_file_has_no_secret_and_a_closed_key_is_refused(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        paths.conf.mkdir(parents=True)
        (paths.conf / "secrets.json").write_text(
            json.dumps({"iqn:init": SECRET}), encoding="utf-8",
        )
        os.chmod(paths.conf / "secrets.json", 0o600)
        (paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{"iqn": "iqn.2026-11.local.san:disk", "chap_password": SECRET}],
        }), encoding="utf-8")
        from san_tui.alerts import add_alert
        add_alert(paths, "error", "fuite {}".format(SECRET))
        report = prepare(paths, Probe([volume()]))
        self.assertEqual(report["action"], "ready")
        text = Path(report["path"]).read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        key = (paths.conf / "audit.key").read_bytes()
        self.assertNotIn(key.hex(), text)
        self.assertEqual(stat.S_IMODE((paths.conf / "audit.key").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(Path(report["path"]).stat().st_mode), 0o644)
        body = json.loads(text)
        payload = canonical({key: value for key, value in body.items() if key != "signature"})
        digest = hmac.new(key, payload, hashlib.sha256).hexdigest()
        self.assertEqual(digest, body["signature"]["mac"])
        self.assertEqual(body["signature"]["key_id"], hashlib.sha256(key).hexdigest()[:16])
        os.chmod(paths.conf / "audit.key", 0o644)
        Path(report["path"]).unlink()
        refused = prepare(paths, Probe([volume()]))
        self.assertEqual(refused["reason"], "clé absente")
        self.assertFalse(Path(report["path"]).exists())

    def test_download_function_reads_the_prepared_file(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node absent")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        prepared = Path(tmp.name) / "audit-export.json"
        prepared.write_text('{"version":1,"signature":{"mac":"abc"}}\n', encoding="utf-8")
        script = Path(__file__).resolve().parents[3] / "live" / "webui"
        front = (script / "front.mjs").read_text(encoding="utf-8")
        self.assertIn("auditResponse(", front)
        self.assertIn("/appliance-audit", front)
        self.assertIn("Télécharger l'audit", front)
        proc = subprocess.run(
            [node, "--input-type=module", "-e", (
                "import { auditResponse } from './audit-route.mjs';"
                "const found = auditResponse(process.argv[1]);"
                "const missing = auditResponse(process.argv[1] + '.absent');"
                "process.stdout.write(JSON.stringify({"
                "status: found.status, body: found.body.toString('utf8'), missing: missing.status}));"
            ), str(prepared)],
            cwd=script,
            check=True,
            capture_output=True,
            text=True,
        )
        answer = json.loads(proc.stdout)
        self.assertEqual(answer["status"], 200)
        self.assertIn("abc", answer["body"])
        self.assertEqual(answer["missing"], 404)
        self.assertFalse(Path(str(prepared) + ".absent").exists())


class InventoryTests(unittest.TestCase):
    def test_loopback_inventory_and_refusal_of_other_addresses(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        paths.conf.mkdir(parents=True)
        (paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{"iqn": "iqn.2026-11.local.san:disk", "lun": 0, "chap_password": SECRET}],
        }), encoding="utf-8")
        (paths.conf / "secrets.json").write_text(json.dumps({"k": SECRET}), encoding="utf-8")
        with self.assertRaises(InventoryError):
            bind_server(paths, Probe([volume()]), "0.0.0.0", 39114)
        with self.assertRaises(InventoryError):
            bind_server(paths, Probe([volume()]), "10.1.0.10", 39114)
        sock = socket.socket()
        sock.settimeout(0.2)
        try:
            self.assertNotEqual(sock.connect_ex(("127.0.0.1", 39114)), 0)
        finally:
            sock.close()
        server = bind_server(paths, Probe([volume(serial="SER123")]), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")
        url = "http://127.0.0.1:{}/api/inventory".format(server.server_address[1])
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertIn("SER123", body)
        self.assertIn("iqn.2026-11.local.san:disk", body)
        self.assertNotIn(SECRET, body)


class MetricsTests(unittest.TestCase):
    def test_admin_only_and_storage_is_refused(self):
        admin = {
            "admin": {"iface": "enp1s0", "address": "127.0.0.1/8"},
            "storage": {"iface": "enp2s0", "address": "10.1.0.10/24"},
        }
        self.assertEqual(metrics_host(admin), "127.0.0.1")
        self.assertIsNone(metrics_host({
            "admin": {"iface": "enp1s0", "address": "10.1.0.10/24"},
            "storage": {"iface": "enp2s0", "address": "10.1.0.10/24"},
        }))
        self.assertIsNone(metrics_host({
            "admin": {"iface": "enp1s0", "address": "0.0.0.0/0"},
            "storage": {"iface": "enp2s0", "address": "10.1.0.10/24"},
        }))
        self.assertIsNone(metrics_host({
            "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
            "storage": {"iface": "enp1s0", "address": "10.0.0.10/24"},
        }))
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(MetricsError):
            _bind(paths, admin, "10.1.0.10", 0)
        far = {
            "admin": {"iface": "enp1s0", "address": "203.0.113.5/24"},
            "storage": {"iface": "enp2s0", "address": "198.51.100.5/24"},
        }
        server, report = open_metrics(paths, Probe([]), far, port=0)
        self.assertIsNone(server)
        self.assertEqual(report["action"], "retry")
        from san_tui.alerts import add_alert
        add_alert(paths, "error", "fuite {}".format(SECRET))
        _write(paths.run / "lun-p99.json", {"samples": [{"iqn": "iqn.2026-11.local.san:disk", "p99_us": 900}]})
        live, info = open_metrics(paths, Probe([]), admin, port=0)
        self.assertEqual(info["host"], "127.0.0.1")
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(live.shutdown)
        self.addCleanup(live.server_close)
        url = "http://127.0.0.1:{}/metrics".format(live.server_address[1])
        with urllib.request.urlopen(url, timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertIn("san_up 1", body)
        self.assertIn('san_lun_p99_us{iqn="iqn.2026-11.local.san:disk"} 900', body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("10.1.0.10", body)


class LatencyTests(unittest.TestCase):
    def test_alert_does_not_change_the_export(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        bench = paths.root / "usr" / "share" / "san" / "perf-results.json"
        _write(bench, {
            "status": "measured",
            "keep": [{"name": "scheduler-none", "change": {"scheduler": "none"}, "p99_us": 831.488}],
        })
        exports = paths.conf / "exports.json"
        _write(exports, {"version": 1, "targets": [{"iqn": "iqn.2026-11.local.san:disk"}]})
        before = exports.read_bytes()
        self.assertEqual(threshold_us(json.loads(bench.read_text(encoding="utf-8"))), 831.488)
        quiet = check(paths, [{"iqn": "iqn.2026-11.local.san:disk", "p99_us": 100}], 831.488)
        self.assertEqual(quiet["alerted"], [])
        self.assertFalse(quiet["exports_changed"])
        _write(paths.run / "lun-p99.json", {
            "samples": [{"iqn": "iqn.2026-11.local.san:disk", "p99_us": 900}],
        })
        report = check_from_disk(paths)
        self.assertEqual(report["alerted"], ["iqn.2026-11.local.san:disk"])
        self.assertFalse(report["exports_changed"])
        self.assertEqual(exports.read_bytes(), before)
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("p99", text)
        self.assertIn("900", text)


class RetireTests(unittest.TestCase):
    def test_failed_data_disk_is_retired_and_boot_is_not(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        boot = volume("wwn-boot", kernel="sda", path="/dev/sda", boot=True, state="failed", holders=["md0"])
        member = volume(state="failed", holders=["md0"], path="/dev/sdb")
        failed = volume(state="failed", path="/dev/sdb")
        errors = volume("wwn-0x6000", kernel="sdc", path="/dev/sdc", serial="ERR", wwn="naa.err", io_errors=2)
        healthy = volume("wwn-0x7000", kernel="sdd", path="/dev/sdd", serial="OK", wwn="naa.ok", state="running")
        calls = []
        self.assertFalse(linux_retire(boot, paths, run=calls.append)["retired"])
        self.assertEqual(calls, [])
        outcome = linux_retire(member, paths, run=calls.append)
        self.assertTrue(outcome["retired"])
        self.assertEqual(calls[0][:3], ["mdadm", "--manage", "/dev/md0"])
        self.assertIn("/dev/sdb", calls[0])
        self.assertNotIn("/dev/sda", str(calls))
        self.assertEqual(classify(boot)["reason"], "disque de boot")
        self.assertTrue(classify(errors)["retire"])
        self.assertFalse(classify(healthy)["retire"])
        probe = Probe([boot, failed, errors, healthy])
        report = retire_auto(paths, probe, run=lambda cmd: None)
        self.assertEqual(report["retired"], [failed["id"], errors["id"]])
        self.assertNotIn(boot["id"], report["retired"])
        select_support(paths, "conf", healthy, healthy["id"])
        keep = plan_export(
            healthy, {"address": "10.0.0.10"}, "iqn.2026-11.local.san:keep", INITIATOR, "client1",
        )
        gone = plan_export(
            failed, {"address": "10.0.0.10", "port": 3261},
            "iqn.2026-11.local.san:gone", INITIATOR, "client1",
        )
        add_export(paths, PLAN, keep, SECRET, healthy["id"])
        document, secrets = load_exports(paths)
        document["targets"].append(gone)
        # Le second export a sa propre référence de secret.
        ref = gone["acls"][0]["chap_secret_ref"]
        secrets[ref] = SECRET
        save_exports(paths, document, secrets)
        # Le masque aligne l'ACL, puis le retrait saute le disque.
        backend = Store()
        restored = restore(paths, probe, backend)
        self.assertEqual([item[0]["iqn"] for item in backend.applied], ["iqn.2026-11.local.san:keep"])
        self.assertEqual(restored["skipped"][0]["reason"], "disque retiré")
        undo(paths)
        self.assertEqual(restore(paths, probe, Store())["exported"], [
            "iqn.2026-11.local.san:keep",
            "iqn.2026-11.local.san:gone",
        ])

    def test_probe_reads_group_and_failure_from_sysfs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root_path = Path(tmp.name)
        self._disk(root_path, "sda", "wwn-0xaaa", state="failed")
        self._disk(root_path, "sdb", "wwn-0xbbb", state="failed", group="payroll")
        self._disk(root_path, "sdc", "wwn-0xccc", state="running", errors="0x2")
        mountinfo = root_path / "proc" / "self" / "mountinfo"
        mountinfo.parent.mkdir(parents=True, exist_ok=True)
        mountinfo.write_text("36 35 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n", encoding="utf-8")
        probe = SysfsProbe(root_path / "sys", root_path / "dev", root_path / "proc")
        found = {item["kernel"]: item for item in probe.list_volumes()}
        self.assertTrue(found["sda"]["boot"])
        self.assertEqual(found["sda"]["state"], "failed")
        self.assertFalse(found["sdb"]["boot"])
        self.assertEqual(found["sdb"]["group_id"], "payroll")
        self.assertEqual(found["sdc"]["io_errors"], 2)
        paths = SanPaths(tmp.name)
        report = retire_auto(paths, probe, run=lambda cmd: None)
        self.assertIn(found["sdb"]["id"], report["retired"])
        self.assertIn(found["sdc"]["id"], report["retired"])
        self.assertNotIn(found["sda"]["id"], report["retired"])

    def _disk(self, root_path, kernel, ident, state, group="", errors="0"):
        device = root_path / "dev" / kernel
        device.parent.mkdir(parents=True, exist_ok=True)
        device.write_bytes(b"")
        by_id = root_path / "dev" / "disk" / "by-id"
        by_id.mkdir(parents=True, exist_ok=True)
        (by_id / ident).symlink_to(device)
        block = root_path / "sys" / "class" / "block" / kernel
        (block / "device").mkdir(parents=True, exist_ok=True)
        (block / "size").write_text("8\n", encoding="utf-8")
        (block / "device" / "state").write_text(state + "\n", encoding="utf-8")
        (block / "device" / "ioerr_cnt").write_text(errors + "\n", encoding="utf-8")
        if group:
            (block / "san_group").write_text(group + "\n", encoding="utf-8")


class ComfortTests(unittest.TestCase):
    def test_missing_pieces_skip_and_a_failure_does_not_stop_the_rest(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        exports = paths.conf / "exports.json"
        paths.conf.mkdir(parents=True)
        exports.write_text('{"version":1,"targets":[]}\n', encoding="utf-8")
        before = exports.read_bytes()
        report = run(paths, Probe([]))
        self.assertEqual(report["iscsi"], "unchanged")
        self.assertEqual(report["replicate"]["reason"], "aucun pair")
        self.assertEqual(report["witness"]["reason"], "aucun témoin")
        self.assertEqual(report["snapshot"]["reason"], "aucun groupe")
        self.assertEqual(report["qos"]["reason"], "aucun profil")
        self.assertEqual(report["audit"]["action"], "ready")
        self.assertEqual(exports.read_bytes(), before)
        seen = []

        def boom():
            raise RuntimeError("coupé")

        def witness():
            seen.append("witness")
            return {"action": "stay"}

        isolated = run(paths, Probe([]), hooks={"replicate": boom, "witness": witness})
        self.assertEqual(isolated["replicate"]["reason"], "RuntimeError")
        self.assertEqual(seen, ["witness"])
        self.assertEqual(isolated["iscsi"], "unchanged")

    def test_diagnostic_does_not_retire(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        paths.run.mkdir(parents=True)
        (paths.run / "mode").write_text("diagnostic\n", encoding="utf-8")
        called = []
        report = run(paths, Probe([volume(state="failed")]), hooks={"retire": lambda: called.append(1)})
        self.assertEqual(called, [])
        self.assertEqual(report["retire"]["reason"], "diagnostic")
        self.assertEqual(report["replicate"]["reason"], "diagnostic")

    def test_stop_command_needs_no_argument(self):
        tmp, paths = root()
        self.addCleanup(tmp.cleanup)
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main(["--root", tmp.name, "replicate-stop"])
        self.assertEqual(code, 0)
        self.assertTrue((paths.run / "replicate.hold").is_file())
        self.assertIn("suspendue", buf.getvalue())


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")



CONFIG_SECRET = "chap-secret-ok"
OTHER = "chap-other-ok1"
REPO = Path(__file__).resolve().parents[3]

def config_volume(name, wwn, serial="SER"):
    return {
        "id": "/dev/disk/by-id/" + name,
        "kernel": "sdb",
        "path": "/dev/sdb",
        "wwn": wwn,
        "serial": serial,
        "model": "TEST",
        "size": 4096,
        "block_size": 512,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": False,
        "kind": "block",
    }


class ConfigProbe:
    def __init__(self, volumes):
        self.volumes = list(volumes)
        self.addresses = {"enp2s0": ["10.0.0.10/24"], "enp1s0": ["192.168.1.10/24"]}

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, stable_id):
        if not stable_id:
            return None
        needle = str(stable_id).split("/")[-1]
        for item in self.volumes:
            if item["id"] == stable_id or item["id"].endswith("/" + needle):
                return item
        return None

    def list_interfaces(self):
        return [{"name": name, "addresses": addrs} for name, addrs in self.addresses.items()]

    def interface_has_address(self, iface, address):
        return address in [item.split("/", 1)[0] for item in self.addresses.get(iface, [])]


class Recording:
    def __init__(self):
        self.applied = []

    def apply(self, target, secret, probe):
        self.applied.append((target["iqn"], secret))


class ConfigExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)

    def test_export_file_is_already_built_and_hides_chap(self):
        conf = config_volume("wwn-conf", "naa.conf")
        lun = config_volume("wwn-old", "naa.lun", "LUN")
        missing = config_volume("wwn-missing", "naa.missing", "GONE")
        select_support(self.paths, "conf", conf, conf["id"])
        first = plan_export(
            lun, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:keep", "iqn.2026-11.local.san:init", "client1",
        )
        second = plan_export(
            missing, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:skip", "iqn.2026-11.local.san:init", "client1",
        )
        from san_tui.iscsi import add_export
        add_export(self.paths, PLAN, first, CONFIG_SECRET, lun["id"])
        add_export(self.paths, PLAN, second, OTHER, missing["id"])
        built = (self.paths.conf / "san-config.json").read_bytes()
        self.assertNotIn(CONFIG_SECRET.encode(), built)
        self.assertNotIn(OTHER.encode(), built)
        self.assertNotIn(base64_of(CONFIG_SECRET), built)
        document = json.loads(built.decode())
        self.assertEqual(document["version"], 2)
        self.assertNotIn("id", document["targets"][0]["volume"])
        replaced = b'{"version":2,"product":"san-appliance","ready":true}\n'
        (self.paths.conf / "san-config.json").write_bytes(replaced)
        self.assertEqual(read_builtin_export(self.paths), replaced)

    def test_import_reattaches_by_wwn_and_skips_a_missing_one(self):
        source = SanPaths(self.tmp.name + "/source")
        conf = config_volume("wwn-conf", "naa.conf")
        lun = config_volume("wwn-old", "naa.lun", "LUN")
        missing = config_volume("wwn-missing", "naa.missing", "GONE")
        select_support(source, "conf", conf, conf["id"])
        from san_tui.iscsi import add_export
        add_export(source, PLAN, plan_export(
            lun, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:keep", "iqn.2026-11.local.san:init", "client1",
        ), CONFIG_SECRET, lun["id"])
        add_export(source, PLAN, plan_export(
            missing, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:skip", "iqn.2026-11.local.san:init", "client1",
        ), OTHER, missing["id"])
        blob = (source.conf / "san-config.json").read_bytes()
        fresh_conf = config_volume("wwn-conf-new", "naa.conf", "CONF2")
        fresh_lun = config_volume("wwn-new", "naa.lun", "LUN2")
        probe = ConfigProbe([fresh_conf, fresh_lun])
        select_support(self.paths, "conf", fresh_conf, fresh_conf["id"])
        report = import_config(self.paths, blob, probe)
        self.assertEqual(report["imported"], [{
            "iqn": "iqn.2026-11.local.san:keep",
            "lun": 0,
            "wwn": "naa.lun",
            "id": fresh_lun["id"],
        }])
        self.assertEqual(len(report["skipped"]), 1)
        self.assertIn("naa.missing", report["skipped"][0]["reason"])
        self.assertIn("absent", report["skipped"][0]["reason"])
        saved, secrets = load_exports(self.paths)
        self.assertEqual(saved["targets"][0]["volume"]["id"], fresh_lun["id"])
        self.assertEqual(saved["targets"][0]["volume"]["wwn"], "naa.lun")
        self.assertNotIn("iqn.2026-11.local.san:skip", [item["iqn"] for item in saved["targets"]])
        self.assertEqual(secrets["iqn.2026-11.local.san:keep:iqn.2026-11.local.san:init"], CONFIG_SECRET)
        exported = (self.paths.conf / "san-config.json").read_bytes()
        self.assertNotIn(CONFIG_SECRET.encode(), exported)
        alerts = (self.paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("naa.missing", alerts)
        self.assertNotIn(CONFIG_SECRET, alerts)
        self.assertNotIn(OTHER, alerts)
        backend = Recording()
        restored = restore(self.paths, probe, backend)
        self.assertEqual(restored["exported"], ["iqn.2026-11.local.san:keep"])
        self.assertEqual(backend.applied, [("iqn.2026-11.local.san:keep", CONFIG_SECRET)])
        self.assertTrue(any(item["iqn"].endswith(":skip") for item in restored["skipped"]) is False)

    def test_restore_still_exports_when_the_webui_session_is_dead(self):
        conf = config_volume("wwn-conf", "naa.conf")
        lun = config_volume("wwn-lun", "naa.lun", "LUN")
        probe = ConfigProbe([conf, lun])
        select_support(self.paths, "conf", conf, conf["id"])
        from san_tui.iscsi import add_export
        add_export(self.paths, PLAN, plan_export(
            lun, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:keep", "iqn.2026-11.local.san:init", "client1",
        ), CONFIG_SECRET, lun["id"])
        webui = self.paths.root / "var" / "lib" / "san" / "webui"
        webui.mkdir(parents=True)
        (webui / "session.json").write_text(json.dumps({"id": "dead", "expires": 0}), encoding="utf-8")
        (webui / "writing").write_text("1\n", encoding="utf-8")
        backend = Recording()
        report = restore(self.paths, probe, backend)
        self.assertEqual(report["exported"], ["iqn.2026-11.local.san:keep"])
        self.assertEqual(backend.applied[0][0], "iqn.2026-11.local.san:keep")

    def test_factory_refuses_without_the_marker_and_keeps_lun_data(self):
        self.paths.conf.mkdir(parents=True)
        self.paths.data.mkdir(parents=True)
        self.paths.etc.mkdir(parents=True)
        payload = b"LUN-DATA" * 40
        data = self.paths.data / "lun.img"
        data.write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        (self.paths.conf / "exports.json").write_text('{"version":1,"targets":[]}\n', encoding="utf-8")
        (self.paths.conf / "backups").mkdir()
        (self.paths.conf / "backups" / "old").mkdir()
        (self.paths.etc / "network.json").write_text("{}\n", encoding="utf-8")
        (self.paths.etc / "chosen.json").write_text('{"version":2}\n', encoding="utf-8")
        (self.paths.conf / "app").mkdir()
        (self.paths.conf / "app" / "state.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(PersistError) as caught:
            factory_reset(self.paths)
        self.assertIn("marqueur", str(caught.exception))
        self.assertTrue((self.paths.conf / "exports.json").is_file())
        self.assertEqual(hashlib.sha256(data.read_bytes()).hexdigest(), digest)
        (self.paths.etc / "factory").write_text("1\n", encoding="utf-8")
        report = factory_reset(self.paths)
        self.assertTrue(report["cleared"])
        self.assertTrue(report["data_kept"])
        self.assertFalse((self.paths.conf / "exports.json").exists())
        self.assertFalse((self.paths.conf / "backups").exists())
        self.assertFalse((self.paths.etc / "network.json").exists())
        self.assertFalse((self.paths.etc / "factory").exists())
        self.assertTrue((self.paths.etc / "chosen.json").is_file())
        self.assertTrue((self.paths.conf / "app" / "state.json").is_file())
        self.assertEqual(hashlib.sha256(data.read_bytes()).hexdigest(), digest)
        with self.assertRaises(PersistError):
            factory_reset(self.paths)
        self.assertEqual(hashlib.sha256(data.read_bytes()).hexdigest(), digest)

    def test_factory_command_refuses_a_normal_tree(self):
        self.paths.conf.mkdir(parents=True)
        (self.paths.conf / "exports.json").write_text("{}\n", encoding="utf-8")
        code = main(["--root", self.tmp.name, "factory"])
        self.assertEqual(code, 2)
        self.assertTrue((self.paths.conf / "exports.json").is_file())

    def test_boot_certificate_is_regenerated_outside_the_iso(self):
        script = REPO / "live" / "rootfs" / "usr" / "local" / "sbin" / "san-webui-cert"
        text = script.read_text(encoding="utf-8")
        self.assertNotIn("BEGIN EC PRIVATE KEY", text)
        self.assertNotIn("BEGIN PRIVATE KEY", text)
        env = os.environ.copy()
        env["SAN_ROOT"] = self.tmp.name
        first = subprocess.run([str(script)], env=env, check=False, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        key = Path(self.tmp.name) / "var" / "lib" / "san" / "webui" / "tls" / "key.pem"
        cert = key.with_name("cert.pem")
        first_key = key.read_bytes()
        self.assertIn(b"PRIVATE KEY", first_key)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(key.stat().st_mode) & 0o007, 0)
        second = subprocess.run([str(script)], env=env, check=False, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(key.read_bytes(), first_key)
        self.assertTrue(cert.is_file())
        self.assertFalse((Path(self.tmp.name) / "usr" / "lib" / "san-webui" / "tls" / "key.pem").exists())
        iso = Path(self.tmp.name) / "iso"
        iso.mkdir()
        self.assertFalse(any(iso.rglob("key.pem")))

    def test_units_do_not_tie_iscsi_to_the_webui(self):
        restore = (REPO / "live24" / "tui" / "systemd" / "san-restore.service").read_text(encoding="utf-8")
        factory = (REPO / "live" / "rootfs" / "etc" / "systemd" / "system" / "san-factory.service").read_text(encoding="utf-8")
        self.assertNotIn("san-webui", restore)
        self.assertIn("ConditionPathExists=/etc/san/factory", factory)

    def test_release_apply_failure_exits_clean_and_keeps_the_slot(self):
        app = self.paths.conf / "app"
        (app / "a").mkdir(parents=True)
        (app / "a" / "version").write_text("1\n", encoding="utf-8")
        state = {"version": 1, "active": "a", "rolled_back": False, "slots": {"a": {}, "b": {}}}
        (app / "state.json").write_text(json.dumps(state), encoding="utf-8")
        before = (app / "state.json").read_text(encoding="utf-8")
        script = REPO / "live" / "rootfs" / "usr" / "local" / "sbin" / "san-release-apply"
        env = os.environ.copy()
        env["SAN_ROOT"] = self.tmp.name
        env["SAN_RELEASE_API"] = "http://127.0.0.1:8091"
        env["PYTHONPATH"] = str(REPO / "live24" / "tui")
        completed = subprocess.run(
            ["python3", str(script)], env=env, check=False, capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("applied", completed.stdout)
        self.assertEqual((app / "state.json").read_text(encoding="utf-8"), before)

    def test_sealed_round_trip_and_a_flipped_mac_is_refused(self):
        blob = unseal(__import__("san_tui.configio", fromlist=["seal"]).seal({"ref": CONFIG_SECRET}))
        self.assertEqual(blob["ref"], CONFIG_SECRET)
        from san_tui.configio import seal
        sealed = seal({"ref": CONFIG_SECRET})
        sealed["mac"] = "AA=="
        with self.assertRaises(PersistError):
            unseal(sealed)


def base64_of(text):
    import base64
    return base64.b64encode(text.encode()).decode().encode()


if __name__ == "__main__":
    unittest.main()
