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


def volume(name="wwn-free", **overrides):
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


class Probe:
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
        boot = volume("boot", boot=True, size=9)
        exported = volume("exported", size=9)
        member = volume("member", size=9)
        occupied = volume("occupied", size=9)
        small = volume("small", size=4)
        blank = volume("blank", size=9)
        foreign = volume("foreign", size=9)
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
        first = volume("a", kernel="sdb", path="/dev/sdb")
        second = volume("b", kernel="sdc", path="/dev/sdc", serial="B", wwn="naa.b")
        spare = volume("c", kernel="sdd", path="/dev/sdd", serial="C", wwn="naa.c", size=first["size"])
        boot = volume("boot", boot=True, size=first["size"] * 2)
        exported = volume("exp", size=first["size"] * 2)
        (paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{"volume": {"id": exported["id"]}}],
        }), encoding="utf-8")
        probe = Probe([first, second, spare, boot, exported])
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
        self.assertEqual(role_of(volume(), set(), set(), set(), "plain", True), "free")


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

        report = health_check(paths, [volume()], run)
        self.assertFalse(report["exports_changed"])
        self.assertEqual(canary.read_bytes(), before)
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("SMART en échec", text)
        self.assertIn("70°C", text)
        skipped = health_check(paths, [volume()], None)
        self.assertEqual(skipped["skipped"], "smartctl absent")
        self.assertFalse(skipped["exports_changed"])
        self.assertEqual(canary.read_bytes(), before)

    def test_screen_shows_smart_and_keeps_the_ip(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.ensure_runtime()
        disk = volume()
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
        probe = Probe([], {
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
        report = run_health(paths, Probe([volume()]))
        self.assertFalse(report["exports_changed"])


if __name__ == "__main__":
    unittest.main()
