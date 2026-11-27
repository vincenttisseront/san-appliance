"""Gardes de boot : syslog, inventaire, table de partitions, diagnostic, slot."""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

from san_tui.iscsi import RecordingBackend, plan_export, restore
from san_tui.paths import SanPaths
from san_tui.persist import select_support
from san_tui.probe import SysfsProbe, write_boot_inventory
from san_tui.raid import RecordingMd
from san_tui.release import rollback_slot
from san_tui.runtime import assemble_raid
from san_tui.safety import refusal_reason
from san_tui.syslog import apply_remote, disable_remote, load_target, reachable


SECRET = "chap-secret-ok"
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
        "pttype": "gpt",
    }
    data.update(overrides)
    return data


class Probe:
    def __init__(self, volumes):
        self.volumes = list(volumes)
        self.addresses = {"enp2s0": ["10.0.0.10/24"]}
        self.firmware_doc = {"bios_version": "1.2.3", "product_name": "test"}

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
            {"name": "enp2s0", "addresses": ["10.0.0.10/24"], "operstate": "up", "mac": "02:00:00:00:00:02"}
        ]

    def interface_has_address(self, iface, address):
        return iface == "enp2s0" and address == "10.0.0.10"

    def firmware(self):
        return dict(self.firmware_doc)


def saved(paths, vol, iqn):
    select_support(paths, "conf", vol, vol["id"])
    target = plan_export(
        vol, {"address": "10.0.0.10"}, iqn, "iqn.2026-11.local.san:init", "client1",
    )
    from san_tui.iscsi import add_export
    add_export(paths, PLAN, target, SECRET, vol["id"])


class PartitionTests(unittest.TestCase):
    def test_unknown_table_is_refused_and_a_known_disk_still_exports(self):
        self.assertIsNone(refusal_reason(volume(pttype="gpt")))
        self.assertIsNone(refusal_reason(volume(pttype="")))
        self.assertIsNone(refusal_reason(volume(pttype="dos")))
        self.assertIn("inconnue", refusal_reason(volume(pttype="atari")))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        good = volume()
        recorded = volume(
            "wwn-0x6000", kernel="sdc", path="/dev/sdc", wwn="naa.6000", serial="BAD", pttype="gpt",
        )
        probe = Probe([recorded, good])
        from san_tui.iscsi import add_export
        select_support(paths, "conf", good, good["id"])
        bad_plan = plan_export(
            recorded, {"address": "10.0.0.10"}, "iqn.2026-11.local.san:bad",
            "iqn.2026-11.local.san:init", "client1",
        )
        add_export(paths, PLAN, bad_plan, SECRET, recorded["id"])
        good_plan = plan_export(
            good, {"address": "10.0.0.10"}, "iqn.2026-11.local.san:good",
            "iqn.2026-11.local.san:init", "client1",
        )
        add_export(paths, PLAN, good_plan, SECRET, good["id"])
        recorded["pttype"] = "unknown"
        backend = RecordingBackend()
        report = restore(paths, probe, backend)
        self.assertEqual(backend.applied, ["iqn.2026-11.local.san:good"])
        self.assertEqual(report["skipped"][0]["iqn"], "iqn.2026-11.local.san:bad")
        self.assertIn("inconnue", report["skipped"][0]["reason"])
        self.assertFalse((paths.run / "mode").is_file())


class DiagnosticTests(unittest.TestCase):
    def test_invalid_restored_config_exports_nothing_and_skips_raid(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume()])
        saved(paths, volume(), "iqn.2026-11.local.san:disk")
        (paths.conf / "exports.json").write_text("{", encoding="utf-8")
        backend = RecordingBackend()
        report = restore(paths, probe, backend)
        self.assertEqual(backend.applied, [])
        self.assertIn("invalide", report["skipped"][0]["reason"])
        self.assertEqual((paths.run / "mode").read_text(encoding="utf-8").strip(), "diagnostic")
        raid = assemble_raid(paths, probe, RecordingMd())
        self.assertEqual(raid["assemble"], [])
        self.assertEqual(raid["skipped"][0]["reason"], "diagnostic")

    def test_missing_export_file_is_not_diagnostic(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume()])
        select_support(paths, "conf", volume(), volume()["id"])
        report = restore(paths, probe, RecordingBackend())
        self.assertEqual(report["exported"], [])
        self.assertFalse((paths.run / "mode").is_file())


class InventoryTests(unittest.TestCase):
    def test_boot_inventory_is_written_once_with_firmware(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume()])
        first = write_boot_inventory(paths, probe)
        dest = paths.run / "boot-inventory.json"
        body = dest.read_bytes()
        stamp = dest.stat().st_mtime_ns
        time.sleep(0.01)
        probe.volumes.append(volume("wwn-new", serial="NEW", wwn="naa.new"))
        probe.firmware_doc["bios_version"] = "changed"
        second = write_boot_inventory(paths, probe)
        self.assertEqual(dest.read_bytes(), body)
        self.assertEqual(dest.stat().st_mtime_ns, stamp)
        self.assertEqual(second["firmware"]["bios_version"], "1.2.3")
        self.assertEqual(len(second["disks"]), 1)
        self.assertEqual(first["exports"], [])
        self.assertEqual(first["nics"][0]["mac"], "02:00:00:00:00:02")

    def test_sysfs_reads_firmware_and_partition_table(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        block = root / "sys" / "class" / "block" / "sdb"
        (block / "device").mkdir(parents=True)
        (block / "pttype").write_text("gpt\n", encoding="utf-8")
        (block / "size").write_text("8\n", encoding="utf-8")
        (block / "dev").write_text("8:16\n", encoding="utf-8")
        dmi = root / "sys" / "class" / "dmi" / "id"
        dmi.mkdir(parents=True)
        (dmi / "bios_version").write_text("V1\n", encoding="utf-8")
        (dmi / "product_name").write_text("lab\n", encoding="utf-8")
        by_id = root / "dev" / "disk" / "by-id"
        by_id.mkdir(parents=True)
        os.symlink("/dev/sdb", by_id / "wwn-0xabc")
        proc = root / "proc" / "self"
        proc.mkdir(parents=True)
        (proc / "mountinfo").write_text("", encoding="utf-8")
        udev = root / "udev"
        udev.mkdir()
        (udev / "b8:16").write_text("E:ID_PART_TABLE_TYPE=dos\n", encoding="utf-8")
        probe = SysfsProbe(sysfs=root / "sys", dev=root / "dev", proc=root / "proc", udev=udev)
        self.assertEqual(probe.firmware()["bios_version"], "V1")
        self.assertEqual(probe.firmware()["product_name"], "lab")
        (block / "pttype").write_text("", encoding="utf-8")
        found = probe.get_volume("wwn-0xabc")
        self.assertIsNotNone(found)
        self.assertEqual(found["pttype"], "dos")


class SyslogTests(unittest.TestCase):
    def test_down_remote_stays_local_and_iscsi_still_exports(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.etc.mkdir(parents=True)
        (paths.etc / "syslog.json").write_text(
            json.dumps({"host": "192.0.2.1", "port": 514}) + "\n",
            encoding="utf-8",
        )
        installed = []

        def install(target):
            installed.append(target)

        report = apply_remote(paths, reach=lambda host, port: False, install=install)
        self.assertFalse(report["remote"])
        self.assertTrue(report["local"])
        self.assertEqual(report["reason"], "injoignable")
        self.assertEqual(installed, [None])
        probe = Probe([volume()])
        saved(paths, volume(), "iqn.2026-11.local.san:disk")
        backend = RecordingBackend()
        restored = restore(paths, probe, backend)
        self.assertEqual(backend.applied, ["iqn.2026-11.local.san:disk"])
        self.assertEqual(restored["exported"], ["iqn.2026-11.local.san:disk"])

    def test_embedded_secret_is_not_forwarded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.etc.mkdir(parents=True)
        (paths.etc / "syslog.json").write_text(
            json.dumps({"host": "logs.example", "port": 514, "password": "nope"}) + "\n",
            encoding="utf-8",
        )
        self.assertIsNone(load_target(paths))

    def test_local_command_undoes_a_reachable_target(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        (paths.conf / "syslog.json").write_text(
            json.dumps({"host": "logs.example", "port": 514}) + "\n",
            encoding="utf-8",
        )
        installed = []
        disable_remote(paths, install=lambda target: installed.append(target))
        report = apply_remote(paths, reach=lambda host, port: True, install=lambda target: installed.append(target))
        self.assertEqual(report["reason"], "local demandé")
        self.assertFalse(report["remote"])

    def test_tcp_probe_sees_an_open_port_and_a_blackhole(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            self.assertTrue(reachable("127.0.0.1", port, timeout=0.5))
        finally:
            server.close()
        started = time.monotonic()
        self.assertFalse(reachable("192.0.2.1", 9, timeout=0.3))
        self.assertLess(time.monotonic() - started, 2.0)


class SlotTests(unittest.TestCase):
    def test_release_rollback_returns_to_the_previous_slot_once(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        paths.data.mkdir(parents=True)
        marker = paths.data / "marker"
        marker.write_text("keep\n", encoding="utf-8")
        app = paths.conf / "app"
        (app / "a").mkdir(parents=True)
        (app / "b").mkdir()
        (app / "a" / "version").write_text("1\n", encoding="utf-8")
        (app / "b" / "version").write_text("2\n", encoding="utf-8")
        state = {"version": 1, "active": "b", "rolled_back": False, "slots": {"a": {}, "b": {}}}
        (app / "state.json").write_text(json.dumps(state), encoding="utf-8")
        first = rollback_slot(paths)
        self.assertTrue(first["rollback"])
        self.assertEqual(first["slot"], "a")
        self.assertEqual(first["previous"], "b")
        self.assertEqual((app / "b" / "version").read_text(encoding="utf-8"), "2\n")
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep\n")
        second = rollback_slot(paths)
        self.assertFalse(second["rollback"])
        self.assertEqual(second["slot"], "a")
        stored = json.loads((app / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["active"], "a")


class WebuiLockTests(unittest.TestCase):
    def test_unlock_removes_the_lock_without_touching_exports(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume()])
        saved(paths, volume(), "iqn.2026-11.local.san:disk")
        lock = paths.root / "var" / "lib" / "san" / "webui" / "lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text('{"failures": 5, "locked_until": 99}\n', encoding="utf-8")
        from san_tui.cli import _unlock_webui
        self.assertEqual(_unlock_webui(paths), {"locked": False})
        self.assertFalse(lock.exists())
        backend = RecordingBackend()
        self.assertEqual(restore(paths, probe, backend)["exported"], ["iqn.2026-11.local.san:disk"])

    def test_webui_lock_file_does_not_stop_iscsi_restore(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume()])
        saved(paths, volume(), "iqn.2026-11.local.san:disk")
        lock = paths.root / "var" / "lib" / "san" / "webui" / "lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_text(json.dumps({"failures": 5, "locked_until": 9_999_999_999_999}), encoding="utf-8")
        backend = RecordingBackend()
        report = restore(paths, probe, backend)
        self.assertEqual(report["exported"], ["iqn.2026-11.local.san:disk"])
        self.assertEqual(backend.applied, ["iqn.2026-11.local.san:disk"])


if __name__ == "__main__":
    unittest.main()
