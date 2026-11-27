"""Réglages iSCSI automatiques. Pas de disque physique, pas de formulaire."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from san_tui.cli import main
from san_tui.paths import SanPaths
from san_tui.release import ASSET, apply_verified
from san_tui.tune import (
    apply_governor,
    apply_hugepages,
    boot_portals,
    boot_tune,
    cache_mode,
    choose_hugepages,
    chosen_mtu,
    io_uring_plan,
    iscsi_queues,
    plan_backstore,
    prepare_backstore,
    release_refused,
    session_plan,
    tune_target,
    undo_governor,
    undo_queues,
)


def archive(files):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, data in files.items():
            payload = data.encode()
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name == "health" else 0o644
            tar.addfile(info, io.BytesIO(payload))
    return blob.getvalue()


class Runner:
    def __init__(self, df=False, set_ok=True, peer="10.1.0.1"):
        self.df = df
        self.set_ok = set_ok
        self.peer = peer
        self.mtus = []

    def route_peer(self, iface):
        return self.peer

    def set_mtu(self, iface, mtu):
        self.mtus.append((iface, int(mtu)))
        if int(mtu) == 9000 and not self.set_ok:
            return False
        return True

    def ping_df(self, iface, peer, size):
        return self.df


class Probe:
    def list_interfaces(self):
        return [
            {"name": "admin0", "addresses": ["192.168.10.10/24"]},
            {"name": "stor0", "addresses": ["10.1.0.10/24"]},
            {"name": "stor1", "addresses": ["10.2.0.10/24"]},
            {"name": "extra0", "addresses": ["10.9.0.10/24"]},
        ]


class FakeTarget:
    def __init__(self, fail_queue=False):
        self.attrs = {}
        self.fail_queue = fail_queue

    def set_attribute(self, name, value):
        if self.fail_queue and name == "default_cmdsn_depth":
            raise OSError("refus")
        self.attrs[name] = value


class DecisionTests(unittest.TestCase):
    def test_queues_follow_the_storage_core_count(self):
        self.assertEqual(iscsi_queues(4), 4)
        self.assertEqual(iscsi_queues(0), 1)
        self.assertEqual(iscsi_queues(None), 1)

    def test_io_uring_needs_rtslib_and_the_image_kernel(self):
        skipped = io_uring_plan(set(), "")
        self.assertFalse(skipped["enabled"])
        self.assertIn("rtslib", skipped["reason"])
        self.assertIn("noyau", skipped["reason"])
        self.assertFalse(io_uring_plan({"uring"}, "")["enabled"])
        self.assertIn("noyau", io_uring_plan({"uring"}, "")["reason"])
        self.assertFalse(io_uring_plan(set(), "CONFIG_IO_URING=y\n")["enabled"])
        self.assertIn("rtslib", io_uring_plan(set(), "CONFIG_IO_URING=y\n")["reason"])
        self.assertTrue(io_uring_plan({"io_uring"}, "CONFIG_IO_URING=m\n")["enabled"])
        noted = io_uring_plan(set(), "CONFIG_IO_URING=y\n", note="rtslib-fb expose un paramètre io_uring.")
        self.assertTrue(noted["enabled"])
        self.assertFalse(io_uring_plan({"uring"}, "CONFIG_IO_URING=y\n", undone_flag=True)["enabled"])

    def test_one_portal_per_storage_nic(self):
        plan = {
            "admin": {"iface": "admin0", "address": "192.168.10.10/24"},
            "storage": {"iface": "stor0", "address": "10.1.0.10/24"},
            "storages": [
                {"iface": "stor0", "address": "10.1.0.10/24"},
                {"iface": "stor1", "address": "10.2.0.10/24"},
            ],
        }
        portals = boot_portals({"portal": {"port": 3260}}, plan, Probe(), extras=True)
        self.assertEqual([item["address"] for item in portals], ["10.1.0.10", "10.2.0.10"])
        self.assertNotIn("192.168.10.10", [item["address"] for item in portals])
        self.assertNotIn("10.9.0.10", [item["address"] for item in portals])
        self.assertEqual(len(boot_portals({"portal": {"port": 3260}}, plan, Probe(), extras=False)), 1)

    def test_cache_and_hugepages_and_sessions(self):
        self.assertEqual(cache_mode(True), "write-back")
        self.assertEqual(cache_mode(False), "write-through")
        self.assertFalse(choose_hugepages(None, 10)["enabled"])
        self.assertFalse(choose_hugepages(10, 10)["enabled"])
        self.assertTrue(choose_hugepages(10, 7)["enabled"])
        self.assertEqual(session_plan(None, 4)["sessions"], 1)
        self.assertEqual(session_plan(40, 4)["sessions"], 1)
        self.assertEqual(session_plan(100, 1)["sessions"], 1)
        self.assertEqual(session_plan(100, 4)["sessions"], 2)
        self.assertEqual(session_plan(250, 8)["sessions"], 3)
        self.assertIn("ne sature pas", session_plan(40, 4)["reason"])

    def test_backstore_file_does_not_grow_during_a_write(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        path = Path(tmp.name) / "lun.img"
        path.write_bytes(b"\0" * 4096)
        result = prepare_backstore(paths, str(path), 1024 * 1024)
        self.assertTrue(result["preallocated"])
        self.assertFalse(result["grow"])
        self.assertFalse(result["skip"])
        self.assertGreaterEqual(path.stat().st_size, 1024 * 1024)
        size = path.stat().st_size
        fd = os.open(str(path), os.O_RDWR)
        try:
            os.pwrite(fd, b"abcd", size - 4)
        finally:
            os.close(fd)
        self.assertEqual(path.stat().st_size, size)
        self.assertFalse(plan_backstore("block", False)["grow"])
        self.assertTrue(plan_backstore("block", False)["preallocated"])
        short = Path(tmp.name) / "short.img"
        short.write_bytes(b"x")

        def fail(_path, _size):
            raise OSError("no")

        refused = prepare_backstore(paths, str(short), 4096, allocate=fail)
        self.assertTrue(refused["skip"])
        self.assertFalse(refused["grow"])
        self.assertEqual(short.stat().st_size, 1)

    def test_mtu_waits_for_a_df_packet(self):
        refused = chosen_mtu("stor0", Runner(df=False), allow_jumbo=True)
        self.assertEqual(refused["mtu"], 1500)
        self.assertIn("DF", refused["reason"])
        accepted = chosen_mtu("stor0", Runner(df=True), allow_jumbo=True)
        self.assertEqual(accepted["mtu"], 9000)
        alone = chosen_mtu("stor0", Runner(df=True, peer=""), allow_jumbo=True)
        self.assertEqual(alone["mtu"], 1500)
        self.assertNotIn(9000, [mtu for _iface, mtu in Runner(df=True, peer="").mtus])
        undone = chosen_mtu("stor0", Runner(df=True), allow_jumbo=False)
        self.assertEqual(undone["mtu"], 1500)

    def test_governor_only_when_cpufreq_exists(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        missing = apply_governor(paths, False, sysfs=Path(tmp.name) / "absent")
        self.assertFalse(missing["set"])
        self.assertIn("cpufreq", missing["reason"])
        root = Path(tmp.name) / "sys"
        cpu = root / "cpu0" / "cpufreq"
        cpu.mkdir(parents=True)
        (cpu / "scaling_governor").write_text("schedutil\n", encoding="utf-8")
        (cpu / "scaling_available_governors").write_text("schedutil performance\n", encoding="utf-8")
        posed = apply_governor(paths, True, sysfs=root)
        self.assertTrue(posed["set"])
        self.assertEqual((cpu / "scaling_governor").read_text(encoding="utf-8").strip(), "performance")
        undo_governor(paths)
        self.assertEqual((cpu / "scaling_governor").read_text(encoding="utf-8").strip(), "schedutil")
        again = apply_governor(paths, True, sysfs=root)
        self.assertFalse(again["set"])
        self.assertIn("retiré", again["reason"])


class ApplyTests(unittest.TestCase):
    def test_failed_queue_keeps_the_target_enabled(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        tpg = Path(tmp.name) / "iscsi" / "iqn.demo" / "tpgt_1"
        (tpg / "attrib").mkdir(parents=True)
        (tpg / "np").mkdir()
        (tpg / "param").mkdir()
        (tpg / "enable").write_text("1\n", encoding="utf-8")
        (tpg / "attrib" / "default_cmdsn_depth").write_text("64\n", encoding="utf-8")
        (tpg / "param" / "MaxConnections").write_text("1\n", encoding="utf-8")
        network = {
            "admin": {"iface": "admin0", "address": "192.168.10.10/24"},
            "storages": [
                {"iface": "stor0", "address": "10.1.0.10/24"},
                {"iface": "stor1", "address": "10.2.0.10/24"},
            ],
        }
        report = boot_tune(
            paths,
            cores=4,
            cpu_percent=180,
            configfs=str(Path(tmp.name) / "iscsi"),
            network=network,
            measure=lambda: None,
            write_pages=lambda _value: True,
            uring_names=set(),
            kernel_config="",
        )
        self.assertEqual((tpg / "enable").read_text(encoding="utf-8"), "1\n")
        self.assertEqual((tpg / "attrib" / "default_cmdsn_depth").read_text(encoding="utf-8").strip(), "4")
        self.assertTrue((tpg / "np" / "10.1.0.10:3260").is_dir())
        self.assertTrue((tpg / "np" / "10.2.0.10:3260").is_dir())
        self.assertFalse((tpg / "np" / "192.168.10.10:3260").exists())
        self.assertEqual((tpg / "param" / "MaxConnections").read_text(encoding="utf-8").strip(), "2")
        self.assertFalse(report["io_uring"]["enabled"])
        self.assertIn("io_uring", report["io_uring"]["reason"])
        self.assertFalse(report["hugepages"]["enabled"])
        self.assertIn("p99", report["hugepages"]["reason"])
        broken = boot_tune(
            paths,
            cores=4,
            cpu_percent=0,
            configfs=str(Path(tmp.name) / "iscsi"),
            network=1,
            measure=lambda: (_ for _ in ()).throw(RuntimeError("fio")),
            write_pages=lambda _value: True,
            uring_names={"uring"},
            kernel_config="CONFIG_IO_URING=y\n",
        )
        self.assertEqual((tpg / "enable").read_text(encoding="utf-8"), "1\n")
        self.assertIn("ignoré", broken["portals"]["reason"])
        self.assertFalse(broken["hugepages"]["enabled"])
        target = FakeTarget(fail_queue=True)
        notes = tune_target(paths, target, target, str(Path(tmp.name) / "missing"))
        self.assertEqual(target.attrs["emulate_write_cache"], 0)
        self.assertTrue(any("conservé" in note for note in notes))

    def test_hugepages_stay_off_without_a_better_p99(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        seen = []
        same = apply_hugepages(paths, lambda: 12, lambda value: seen.append(value))
        self.assertFalse(same["enabled"])
        self.assertEqual(seen[-1], 0)
        seen.clear()
        calls = {"n": 0}

        def measure():
            calls["n"] += 1
            return 20 if calls["n"] == 1 else 9

        better = apply_hugepages(paths, measure, lambda value: seen.append(value), 16)
        self.assertTrue(better["enabled"])
        self.assertEqual(seen[-1], 16)

    def test_queue_undo_restores_the_previous_depth(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        tpg = Path(tmp.name) / "iscsi" / "iqn.demo" / "tpgt_1"
        (tpg / "attrib").mkdir(parents=True)
        (tpg / "enable").write_text("1\n", encoding="utf-8")
        (tpg / "attrib" / "default_cmdsn_depth").write_text("64\n", encoding="utf-8")
        boot_tune(
            paths,
            cores=3,
            cpu_percent=0,
            configfs=str(Path(tmp.name) / "iscsi"),
            network={"storages": []},
            measure=lambda: None,
            write_pages=lambda _value: True,
            uring_names=set(),
            kernel_config="",
        )
        self.assertEqual((tpg / "attrib" / "default_cmdsn_depth").read_text(encoding="utf-8").strip(), "3")
        undo_queues(paths)
        self.assertEqual((tpg / "attrib" / "default_cmdsn_depth").read_text(encoding="utf-8").strip(), "64")
        self.assertEqual((tpg / "enable").read_text(encoding="utf-8"), "1\n")

    def test_tune_command_needs_no_setting(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertEqual(main(["--root", tmp.name, "tune"]), 0)
        self.assertEqual(main(["--root", tmp.name, "queues-undo"]), 0)
        self.assertTrue((Path(tmp.name) / "run" / "san" / "tune.json").is_file())


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)
        self.paths.conf.mkdir(parents=True)
        app = self.paths.conf / "app"
        (app / "a").mkdir(parents=True)
        (app / "a" / "version").write_text("1\n", encoding="utf-8")
        (app / "a" / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (app / "state.json").write_text(json.dumps({
            "version": 1, "active": "a", "rolled_back": False,
            "slots": {"a": {"sha256": "a" * 64, "p99_us": 10}, "b": {}},
        }), encoding="utf-8")
        share = self.paths.root / "usr" / "share" / "san"
        share.mkdir(parents=True)
        (share / "perf-results.json").write_text(json.dumps({
            "status": "measured",
            "trials": {"baseline": {"workloads": {"read": {
                "iops": 100, "p99_us": 10, "throughput_bps": 100, "cpu_per_gb": 1,
            }}}},
        }), encoding="utf-8")
        self.blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        self.digest = hashlib.sha256(self.blob).hexdigest()
        self.checksum = "{}  {}\n".format(self.digest, ASSET)

    def test_a_worse_p99_is_not_applied(self):
        self.assertTrue(release_refused(self.paths, {"pass": True, "p99_us": 40})[0])
        self.assertFalse(release_refused(self.paths, {"pass": True, "p99_us": 10})[0])
        gate = Gate()
        result = apply_verified(
            self.paths, self.blob, self.checksum,
            health=lambda _slot: 0,
            gate=gate,
            bench=lambda: {"pass": True, "p99_us": 40},
            signer=lambda *_args: ("", ""),
        )
        self.assertFalse(result["applied"])
        self.assertIn("p99", result["reason"])
        self.assertEqual(gate.events, [])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")
        self.assertFalse((self.paths.conf / "app" / "b").exists())

    def test_a_better_p99_is_applied(self):
        gate = Gate()
        result = apply_verified(
            self.paths, self.blob, self.checksum,
            health=lambda _slot: 0,
            gate=gate,
            bench=lambda: {"pass": True, "p99_us": 4},
            signer=lambda *_args: ("", ""),
        )
        self.assertTrue(result["applied"])
        self.assertEqual(result["slot"], "b")
        self.assertIn("freeze", gate.events)


class Gate:
    def __init__(self):
        self.events = []

    def freeze(self):
        self.events.append("freeze")

    def resume(self):
        self.events.append("resume")


if __name__ == "__main__":
    unittest.main()
