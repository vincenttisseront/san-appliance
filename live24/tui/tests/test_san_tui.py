"""Contrôles qui ne touchent ni LIO ni un disque physique."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from san_tui.iscsi import ExportError, add_export, plan_export, restore
from san_tui.paths import SanPaths
from san_tui.persist import PersistError, load_exports, save_exports, select_support
from san_tui.probe import boot_disks, disk_of, inventory, parse_mountinfo
from san_tui.redact import redact
from san_tui.safety import SafetyError, check_portal, refusal_reason, require_confirmation
from san_tui.ui import screen_lines


def volume(name="wwn-0x5000", **overrides):
    data = {
        "id": "/dev/disk/by-id/" + name,
        "kernel": "sdb",
        "path": "/dev/sdb",
        "wwn": "naa.5000cca",
        "serial": "SER123",
        "model": "TEST",
        "size": 4096,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": False,
        "kind": "block",
    }
    data.update(overrides)
    return data


class FakeProbe:
    def __init__(self, volumes, addresses=None):
        self.volumes = list(volumes)
        self.addresses = addresses or {"enp2s0": ["10.0.0.10/24"]}

    def list_volumes(self):
        return list(self.volumes)

    def get_volume(self, stable_id):
        needle = stable_id.split("/")[-1]
        for item in self.volumes:
            if item["id"] == stable_id or item["id"].endswith("/" + needle):
                return item
        return None

    def list_interfaces(self):
        return [
            {"name": name, "addresses": addrs, "operstate": "up", "mac": ""}
            for name, addrs in self.addresses.items()
        ]

    def interface_has_address(self, iface, address):
        return address in [item.split("/", 1)[0] for item in self.addresses.get(iface, [])]


PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}
SECRET = "chap-secret-ok"


class SafetyTests(unittest.TestCase):
    def test_refuse_boot_mounted_and_active(self):
        self.assertIn("boot", refusal_reason(volume(boot=True)))
        self.assertIn("monté", refusal_reason(volume(mounts=["/mnt/data"])))
        self.assertIn("actif", refusal_reason(volume(active=True, holders=["dm-0"])))

    def test_refuse_unstable_identity(self):
        self.assertIn("insuffisant", refusal_reason(volume(wwn="", serial="")))

    def test_confirmation_is_exact(self):
        with self.assertRaises(SafetyError):
            require_confirmation(volume()["id"], "sdb")
        require_confirmation(volume()["id"], volume()["id"])

    def test_portal_is_storage_address_only(self):
        self.assertIn("toutes les interfaces", check_portal({"address": "0.0.0.0"}, PLAN))
        self.assertIn("administration", check_portal({"address": "192.168.1.10"}, PLAN))
        self.assertIsNone(check_portal({"address": "10.0.0.10"}, PLAN))
        same = {
            "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
            "storage": {"iface": "enp1s0", "address": "10.0.0.10/24"},
        }
        self.assertIn("distinctes", check_portal({"address": "10.0.0.10"}, same))

    def test_boot_disk_includes_its_partitions(self):
        text = "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 1 8:2 / /boot rw - ext4 /dev/sda2 rw\n"
        mounts = parse_mountinfo(text)
        self.assertEqual(disk_of("sda1"), "sda")
        self.assertEqual(disk_of("nvme0n1p2"), "nvme0n1")
        self.assertIn("sda", boot_disks(mounts))


class PersistTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = SanPaths(self.tmp.name)
        self.probe = FakeProbe([volume(), volume("wwn-0x6000", kernel="sdc", path="/dev/sdc")])

    def tearDown(self):
        self.tmp.cleanup()

    def test_select_and_versioned_backup_hides_secret(self):
        chosen = select_support(self.paths, "conf", volume(), volume()["id"])
        self.assertEqual(chosen["conf"]["id"], volume()["id"])
        target = plan_export(
            volume(), {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, target, SECRET, volume()["id"])
        public = (self.paths.conf / "exports.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, public)
        self.assertNotIn("chap_password", public)
        mode = stat.S_IMODE((self.paths.conf / "secrets.json").stat().st_mode)
        self.assertEqual(mode, 0o600)
        add_export(self.paths, PLAN, target, SECRET, volume()["id"])
        backups = list((self.paths.conf / "backups").iterdir())
        self.assertTrue(backups)
        manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn(SECRET, json.dumps(manifest))

    def test_invalid_public_secret_is_rejected(self):
        with self.assertRaises(PersistError):
            save_exports(self.paths, {"version": 1, "targets": [], "chap_password": "x"}, {})

    def test_missing_volume_is_not_exported(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        other = volume("wwn-0x6000", kernel="sdc", path="/dev/sdc", wwn="naa.6000", serial="OTHER")
        target = plan_export(
            other, {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:gone", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, target, SECRET, other["id"])
        self.probe.volumes = [volume()]
        backend = Recording()
        report = restore(self.paths, self.probe, backend)
        self.assertEqual(backend.applied, [])
        self.assertIn("absent", report["skipped"][0]["reason"])
        alerts = (self.paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, alerts)

    def test_invalid_conf_exports_nothing(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        self.paths.conf.mkdir(parents=True, exist_ok=True)
        (self.paths.conf / "exports.json").write_text("{", encoding="utf-8")
        backend = Recording()
        report = restore(self.paths, self.probe, backend)
        self.assertEqual(backend.applied, [])
        self.assertIn("invalide", report["skipped"][0]["reason"])

    def test_identity_mismatch_blocks_one_target_only(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        good = plan_export(
            volume(), {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:good", "iqn.2026-11.local.san:init", "client1",
        )
        bad_vol = volume("wwn-0x6000", kernel="sdc", path="/dev/sdc", wwn="naa.6000", serial="OTHER")
        bad = plan_export(
            bad_vol, {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:bad", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, good, SECRET, good["volume"]["id"])
        add_export(self.paths, PLAN, bad, SECRET, bad["volume"]["id"])
        changed = dict(bad_vol)
        changed["serial"] = "REPLACED"
        self.probe.volumes = [volume(), changed]
        backend = Recording()
        report = restore(self.paths, self.probe, backend)
        self.assertEqual(backend.applied, ["iqn.2026-11.local.san:good"])
        self.assertEqual(report["skipped"][0]["iqn"], "iqn.2026-11.local.san:bad")

    def test_storage_address_missing_blocks_export(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        target = plan_export(
            volume(), {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, target, SECRET, volume()["id"])
        self.probe.addresses = {}
        backend = Recording()
        report = restore(self.paths, self.probe, backend)
        self.assertEqual(backend.applied, [])
        self.assertIn("réseau stockage", report["skipped"][0]["reason"])

    def test_unselected_disk_is_not_in_the_saved_export(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        untouched = volume("ata-untouched", kernel="sdd", path="/dev/sdd", wwn="naa.untouched", serial="KEEP")
        target = plan_export(
            volume(), {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, target, SECRET, volume()["id"])
        document, _secrets = load_exports(self.paths)
        ids = [item["volume"]["id"] for item in document["targets"]]
        self.assertNotIn(untouched["id"], ids)

    def test_chap_length_and_redaction(self):
        with self.assertRaises(ExportError):
            plan_ok = plan_export(
                volume(), {"address": "10.0.0.10"},
                "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
            )
            add_export(self.paths, PLAN, plan_ok, "short", volume()["id"])
        self.assertEqual(redact("secret short here", ["short"]), "secret [redacted] here")

    def test_boot_inventory_has_no_exports(self):
        snap = inventory(self.probe)
        self.assertEqual(snap["exports"], [])

    def test_screens_do_not_show_the_secret(self):
        select_support(self.paths, "conf", volume(), volume()["id"])
        target = plan_export(
            volume(), {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(self.paths, PLAN, target, SECRET, volume()["id"])
        snap = inventory(self.probe)
        text = "\n".join(screen_lines("Exports iSCSI", snap, self.paths))
        self.assertIn("iqn.2026-11.local.san:disk", text)
        self.assertNotIn(SECRET, text)


class Recording:
    def __init__(self):
        self.applied = []

    def apply(self, target, secret, probe):
        self.applied.append(target["iqn"])
        self.assert_secret = secret


class LeakBackend:
    def apply(self, target, secret, probe):
        raise RuntimeError(secret)


class RedactFailureTests(unittest.TestCase):
    def test_backend_exception_does_not_land_in_the_alert(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = FakeProbe([volume()])
        select_support(paths, "conf", volume(), volume()["id"])
        target = plan_export(
            volume(), {"address": "10.0.0.10"},
            "iqn.2026-11.local.san:disk", "iqn.2026-11.local.san:init", "client1",
        )
        add_export(paths, PLAN, target, SECRET, volume()["id"])
        restore(paths, probe, LeakBackend())
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertIn("échec LIO", text)


if __name__ == "__main__":
    unittest.main()
