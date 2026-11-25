"""Gardes du boot automatique. Aucun disque ni LIO n'est touché."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from san_tui.abupdate import UpdateError, apply_update, grub_cmdline, plan_update
from san_tui.alerts import add_alert
from san_tui.identity import identity_problem
from san_tui.iscsi import plan_export, restore
from san_tui.lvm import plan_activate
from san_tui.medium import accept_ollama_env, evaluate
from san_tui.nvme import RecordingNvme, plan_subsystem, subsystem_problem
from san_tui.ollama import decide, rollback_needed
from san_tui.paths import SanPaths
from san_tui.perf import assert_kept_safe, complete, regressed, render_table, select_keep
from san_tui.persist import select_support
from san_tui.raid import RecordingMd, plan_assemble
from san_tui.runtime import advise, assemble_raid, restore_nvme
from san_tui.safety import SafetyError
from san_tui.toram import decide as toram_decide
from san_tui.toram import installed_cmdline


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


PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}


def loads(p99, iops=1000, bw=1000000, cpu=1.0):
    names = ("read", "write", "randread-4k", "randwrite-4k", "randread-64k", "randwrite-64k", "randread-1m", "randwrite-1m")
    return {name: {"iops": iops, "p99_us": p99, "throughput_bps": bw, "cpu_per_gb": cpu} for name in names}


class MediumTests(unittest.TestCase):
    def test_version_and_identity_must_match(self):
        disk = volume()
        stamp = {"version": 2, "conf": disk, "data": None}
        self.assertIsNone(evaluate(stamp, disk))
        self.assertIn("version", evaluate({"version": 1, "conf": disk}, disk))
        other = volume(serial="OTHER")
        self.assertIn("série", evaluate(stamp, other))
        self.assertIn("absent", evaluate(stamp, None))

    def test_block_size_mismatch_blocks_the_medium(self):
        disk = volume()
        changed = volume(block_size=4096)
        self.assertIn("bloc", identity_problem(disk, changed, "volume"))

    def test_ollama_env_rejects_a_key(self):
        self.assertTrue(accept_ollama_env("SAN_OLLAMA_ENDPOINT=http://192.168.1.2:11434/advise\n"))
        self.assertFalse(accept_ollama_env("SAN_OLLAMA_ENDPOINT=http://h/advise\nSAN_OLLAMA_KEY=abc\n"))
        self.assertFalse(accept_ollama_env("SAN_OLLAMA_ENDPOINT=http://h/secret-path\n"))


class RaidLvmTests(unittest.TestCase):
    def test_raid_refuses_boot_and_missing_members(self):
        boot = volume("wwn-boot", boot=True, kernel="sda", path="/dev/sda")
        other = volume("wwn-other", kernel="sdc", path="/dev/sdc", serial="OTHER", wwn="naa.other")
        probe = Probe([boot, other])
        plan = plan_assemble({
            "version": 1,
            "arrays": [
                {"name": "san0", "members": [boot["id"], other["id"]]},
                {"name": "gone", "members": [other["id"], "/dev/disk/by-id/missing"]},
                {"name": "../x", "members": [other["id"], boot["id"]]},
            ],
        }, probe)
        self.assertEqual(plan["assemble"], [])
        self.assertTrue(any("boot" in item["reason"] for item in plan["skipped"]))
        self.assertTrue(any("absent" in item["reason"] for item in plan["skipped"]))

    def test_raid_assemble_does_not_scan(self):
        first = volume()
        second = volume("wwn-2", kernel="sdc", path="/dev/sdc", serial="B", wwn="naa.b")
        probe = Probe([first, second])
        backend = RecordingMd()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        (paths.conf / "raid.json").write_text(json.dumps({
            "version": 1,
            "arrays": [{"name": "san0", "level": "raid1", "members": [first["id"], second["id"]]}],
        }), encoding="utf-8")
        (paths.run).mkdir(parents=True)
        (paths.run / "mode").write_text("live\n", encoding="utf-8")
        report = assemble_raid(paths, probe, backend)
        self.assertEqual(report["assemble"], ["san0"])
        self.assertEqual(backend.assembled[0][0], "san0")
        self.assertFalse(backend.scanned)
        with self.assertRaises(Exception):
            backend.scan()

    def test_lvm_refuses_a_mounted_pv(self):
        mounted = volume(mounts=["/mnt/data"])
        probe = Probe([mounted])
        plan = plan_activate({
            "version": 1,
            "groups": [{"name": "sanvg", "pvs": [mounted["id"]], "lvs": ["sanlv"]}],
        }, probe)
        self.assertEqual(plan["activate"], [])
        self.assertIn("monté", plan["skipped"][0]["reason"])


class NvmeTests(unittest.TestCase):
    def test_same_portal_guards_as_iscsi(self):
        with self.assertRaises(Exception):
            plan_subsystem(volume(), {"address": "0.0.0.0"}, "nqn.2026-11.local:disk", "nqn.2026-11.local:host", PLAN)
        item = plan_subsystem(
            volume(), {"address": "10.0.0.10", "port": 4420},
            "nqn.2026-11.local:disk", "nqn.2026-11.local:host", PLAN,
        )
        self.assertFalse(item["allow_any_host"])
        probe = Probe([volume()])
        self.assertIsNone(subsystem_problem(item, PLAN, probe))
        probe.volumes = []
        self.assertIn("absent", subsystem_problem(item, PLAN, probe))

    def test_missing_modules_stop_the_item(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        item = plan_subsystem(
            volume(), {"address": "10.0.0.10"},
            "nqn.2026-11.local:disk", "nqn.2026-11.local:host", PLAN,
        )
        (paths.conf / "nvme.json").write_text(json.dumps({
            "version": 1, "network": PLAN, "subsystems": [item],
        }), encoding="utf-8")
        report = restore_nvme(paths, Probe([volume()]), RecordingNvme(modules=False))
        self.assertTrue(report.get("stopped"))
        self.assertEqual(report["exported"], [])
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("modules absents", text)


class ToramTests(unittest.TestCase):
    def test_toram_stays_off_until_measured_and_never_on_the_iso(self):
        small = toram_decide(1024 * 1024, 900 * 1024 * 1024)
        self.assertFalse(small["enabled"])
        self.assertFalse(small["eligible"])
        large = toram_decide(8 * 1024 * 1024, 900 * 1024 * 1024)
        self.assertFalse(large["enabled"])
        self.assertTrue(large["eligible"])
        self.assertEqual(installed_cmdline(large), "")
        self.assertIn("toram", installed_cmdline({**large, "allow_installed": True}))
        self.assertNotIn("toram", grub_cmdline(large))
        self.assertIn("toram", grub_cmdline({**large, "allow_installed": True}))


class PerfOllamaTests(unittest.TestCase):
    def test_regression_is_not_kept(self):
        base = {"workloads": loads(100)}
        better = {"workloads": loads(80, iops=1200), "change": {"scheduler": "none"}}
        worse = {"workloads": loads(200), "change": {"scheduler": "mq-deadline"}}
        self.assertFalse(regressed(base, better))
        self.assertTrue(regressed(base, worse))
        kept = select_keep(base, {"baseline": base, "better": better, "worse": worse})
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "better")
        assert_kept_safe(base, {"better": better, "worse": worse}, kept)
        with self.assertRaises(ValueError):
            assert_kept_safe(base, {"worse": worse}, [{"name": "worse", "change": worse["change"]}])
        self.assertIn("better", render_table({"keep": kept, "trials": {"baseline": base, "better": better}}))
        self.assertTrue(complete(base))

    def test_ollama_cannot_invent_a_scheduler(self):
        catalog = {
            "current": "baseline",
            "profiles": {
                "baseline": {"change": {}, "p99_us": 100},
                "none": {"change": {"scheduler": "none"}, "p99_us": 80},
            },
        }
        self.assertEqual(decide(None, catalog, "baseline")["action"], "alert")
        self.assertEqual(decide({"scheduler": "cfq"}, catalog, "baseline")["action"], "alert")
        self.assertEqual(decide({"profile": "cfq"}, catalog, "baseline")["reason"], "profil non mesuré")
        choice = decide({"profile": "none"}, catalog, "baseline")
        self.assertEqual(choice["action"], "apply")
        self.assertEqual(choice["change"], {"scheduler": "none"})
        worse = decide({"profile": "none"}, {
            "profiles": {"none": {"change": {"scheduler": "none"}, "p99_us": 200}, "baseline": {"change": {}, "p99_us": 100}},
        }, "baseline")
        self.assertEqual(worse["action"], "rollback")
        self.assertTrue(rollback_needed(80, 120))
        self.assertTrue(rollback_needed(80, None))
        self.assertFalse(rollback_needed(80, 70))

    def test_advise_alerts_and_does_not_apply_when_the_endpoint_is_down(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.etc.mkdir(parents=True)
        (paths.etc / "ollama.env").write_text(
            "SAN_OLLAMA_ENDPOINT=http://127.0.0.1:9/advise\n", encoding="utf-8",
        )
        applied = []
        report = advise(paths, fetcher=lambda: None, measurer=lambda: 10, applier=applied.append)
        self.assertEqual(report["action"], "alert")
        self.assertEqual(applied, [])
        text = (paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("injoignable", text)
        mode = stat.S_IMODE((paths.run / "console.log").stat().st_mode)
        self.assertTrue(mode & stat.S_IRUSR)


class AbTests(unittest.TestCase):
    def test_update_writes_the_inactive_slot_only(self):
        slot_b = volume("virtio-ab-part2")
        data = volume("virtio-ab-part3", kernel="sdd", path="/dev/sdd", serial="DATA", wwn="naa.data")
        state = {
            "version": 1,
            "active": "a",
            "disk": "/dev/disk/by-id/virtio-ab",
            "slots": {
                "a": {"id": "/dev/disk/by-id/virtio-ab-part1"},
                "b": {"id": slot_b["id"]},
            },
            "data": {"id": data["id"]},
        }
        payload = b"image-b"
        digest = __import__("hashlib").sha256(payload).hexdigest()
        pending = {"version": 1, "slot": "b", "image": "root.img", "sha256": digest}
        volumes = {slot_b["id"]: slot_b, data["id"]: data}
        plan = plan_update(state, pending, volumes, "live")
        self.assertEqual(plan["id"], slot_b["id"])
        written = {}
        saved = []

        def read_image(_path):
            return payload

        def write_device(dev, blob):
            self.assertNotEqual(dev, data["id"])
            written[dev] = blob

        def read_device(dev, size):
            return written[dev][:size]

        updated = apply_update(plan, read_image, write_device, read_device, saved.append, state)
        self.assertEqual(written[slot_b["id"]], payload)
        self.assertNotIn(data["id"], written)
        self.assertEqual(updated["active"], "b")
        self.assertEqual(updated["data"]["id"], data["id"])
        self.assertEqual(saved[-1]["active"], "b")
        with self.assertRaises(UpdateError):
            plan_update(state, pending, volumes, "diagnostic")
        with self.assertRaises(UpdateError):
            plan_update(
                {**state, "active": "b"},
                pending,
                volumes,
                "live",
            )


class AlertFanoutTests(unittest.TestCase):
    def test_console_log_is_local_to_the_test_root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        add_alert(paths, "error", "volume absent : disk", ["secret-value"])
        text = (paths.run / "console.log").read_text(encoding="utf-8")
        self.assertIn("san-alert:", text)
        self.assertIn("volume absent", text)
        self.assertNotIn("secret-value", text)
        self.assertNotEqual(paths.root, Path("/"))


class PortalTests(unittest.TestCase):
    def test_second_target_on_the_same_portal_is_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        probe = Probe([volume(), volume("wwn-2", kernel="sdc", path="/dev/sdc", serial="B", wwn="naa.b")])
        select_support(paths, "conf", volume(), volume()["id"])
        from san_tui.iscsi import add_export
        first = plan_export(volume(), {"address": "10.0.0.10"}, "iqn.2026-11.local:a", "iqn.2026-11.local:init", "client1")
        second_vol = volume("wwn-2", kernel="sdc", path="/dev/sdc", serial="B", wwn="naa.b")
        second = plan_export(second_vol, {"address": "10.0.0.10"}, "iqn.2026-11.local:b", "iqn.2026-11.local:init", "client1")
        add_export(paths, PLAN, first, "chap-secret-ok", volume()["id"])
        add_export(paths, PLAN, second, "chap-secret-ok", second_vol["id"])

        class Rec:
            def __init__(self):
                self.applied = []

            def apply(self, target, secret, probe):
                self.applied.append(target["iqn"])

        backend = Rec()
        report = restore(paths, probe, backend)
        self.assertEqual(backend.applied, ["iqn.2026-11.local:a"])
        self.assertIn("un seul portail", report["skipped"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
