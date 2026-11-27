"""Réglages bloc automatiques. Aucun disque physique."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from san_tui.blocktune import (
    apply_tuning,
    bench_moves,
    burst_parameters,
    cpus_from_mask,
    irq_list,
    measured_queue_depth,
    observe_offload,
    undo,
    write_perf_table,
)
from san_tui.fabric import apply_vaai
from san_tui.luks import exported_path, format_volume, open_volume, prepare, refusal_for
from san_tui.paths import SanPaths
from san_tui.ui import SCREENS


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class Probe:
    def __init__(self, volumes):
        self.volumes = list(volumes)

    def get_volume(self, ident):
        for item in self.volumes:
            if item.get("id") == ident:
                return item
        return None

    def list_volumes(self):
        return list(self.volumes)


def _layout(root, bench=None):
    base = Path(root)
    paths = SanPaths(root)
    paths.conf.mkdir(parents=True)
    paths.etc.mkdir(parents=True)
    raw = {
        "id": "/dev/disk/by-id/wwn-raw",
        "path": "/dev/sdb",
        "kernel": "sdb",
        "kind": "block",
    }
    file_lun = {
        "id": "/dev/disk/by-id/wwn-file",
        "path": "/var/lib/san/data/lun.img",
        "kernel": "sdc",
        "kind": "file",
    }
    _write(paths.conf / "exports.json", json.dumps({
        "version": 1,
        "network": {
            "admin": {"iface": "admin0", "address": "192.168.0.10/24"},
            "storage": {"iface": "stor0", "address": "10.0.0.10/24"},
        },
        "targets": [{
            "iqn": "iqn.2026-11.local.san:disk",
            "volume": raw,
            "extras": [{"lun": 1, "volume": file_lun}],
        }],
    }))
    _write(paths.conf / "secrets.json", "{}\n")
    _write(base / "sys/block/sdb/queue/scheduler", "[mq-deadline] none\n")
    _write(base / "sys/block/sdb/queue/read_ahead_kb", "128\n")
    _write(base / "sys/block/sdb/queue/nr_requests", "128\n")
    _write(base / "sys/block/sdb/device/queue_depth", "32\n")
    _write(base / "sys/block/sdc/queue/scheduler", "[mq-deadline] none\n")
    _write(base / "sys/block/sdc/queue/read_ahead_kb", "256\n")
    _write(base / "sys/block/sdc/queue/nr_requests", "64\n")
    _write(base / "sys/class/net/stor0/device/msi_irqs/25", "")
    _write(base / "sys/class/net/stor0/features", "tso=on gro=on\n")
    _write(base / "sys/class/net/admin0/device/msi_irqs/7", "")
    _write(base / "sys/devices/system/cpu/present", "0-1\n")
    _write(base / "proc/irq/25/smp_affinity", "3\n")
    _write(base / "proc/irq/7/smp_affinity", "1\n")
    _write(base / "sys/kernel/config/target/core/iblock_0/lun/attrib/emulate_tpu", "0\n")
    _write(base / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/ImmediateData", "No\n")
    _write(base / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/FirstBurstLength", "8192\n")
    if bench is not None:
        _write(base / "usr/share/san/perf-results.json", json.dumps(bench))
    return paths, Probe([raw, file_lun])


def _loads(p99, iops):
    return {"read": {"iops": iops, "p99_us": p99, "throughput_bps": 1000, "cpu_per_gb": 1}}


def _bench(name, change, before, after):
    return {
        "status": "measured",
        "keep": [{"name": name, "change": change, "p99_us": after[0]}],
        "trials": {
            "baseline": {"workloads": _loads(*before), "change": {}},
            name: {"workloads": _loads(*after), "change": change},
        },
    }


class BlockTuneTests(unittest.TestCase):
    def test_one_change_at_a_time_and_a_worse_p99_rolls_back(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        calls = []

        def fake_run(argv, *args, **kwargs):
            calls.append(list(argv))
            text = "tcp-segmentation-offload: on\ngeneric-receive-offload: off\n"
            return subprocess.CompletedProcess(argv, 0, text, "")

        self.addCleanup(setattr, subprocess, "run", subprocess.run)
        subprocess.run = fake_run
        paths, probe = _layout(tmp.name, _bench("burst-64k", {"burst": "64k"}, (40, 1000), (20, 1400)))
        seen = []

        def measure():
            seen.append(len(seen))
            return [100, 90, 80, 70, 60, 50, 400][len(seen) - 1]

        report = apply_tuning(
            paths, probe,
            sysfs=Path(tmp.name) / "sys",
            proc=Path(tmp.name) / "proc",
            configfs=Path(tmp.name) / "sys/kernel/config",
            measure=measure,
        )
        self.assertEqual(report["iscsi"], "unchanged")
        actions = [(item["name"], item["action"]) for item in report["steps"]]
        self.assertEqual(actions, [
            ("scheduler", "kept"),
            ("read-ahead", "kept"),
            ("irq", "kept"),
            ("nr-requests", "kept"),
            ("unmap", "kept"),
            ("burst", "rollback"),
        ])
        root = Path(tmp.name)
        self.assertEqual((root / "sys/block/sdb/queue/scheduler").read_text(encoding="utf-8").strip(), "none")
        self.assertEqual((root / "sys/block/sdc/queue/scheduler").read_text(encoding="utf-8").strip(), "[mq-deadline] none")
        self.assertEqual((root / "sys/block/sdb/queue/read_ahead_kb").read_text(encoding="utf-8").strip(), "0")
        self.assertEqual((root / "sys/block/sdc/queue/read_ahead_kb").read_text(encoding="utf-8").strip(), "256")
        self.assertEqual((root / "proc/irq/25/smp_affinity").read_text(encoding="utf-8").strip(), "2")
        self.assertEqual((root / "sys/block/sdb/queue/nr_requests").read_text(encoding="utf-8").strip(), "32")
        self.assertEqual((root / "sys/block/sdc/queue/nr_requests").read_text(encoding="utf-8").strip(), "64")
        self.assertEqual((root / "sys/kernel/config/target/core/iblock_0/lun/attrib/emulate_tpu").read_text(encoding="utf-8").strip(), "1")
        self.assertEqual((root / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/ImmediateData").read_text(encoding="utf-8").strip(), "No")
        self.assertEqual((root / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/FirstBurstLength").read_text(encoding="utf-8").strip(), "8192")
        self.assertEqual(report["offload"]["changed"], False)
        self.assertEqual(report["offload"]["advertised"]["tcp-segmentation-offload"], "on")
        self.assertEqual(report["offload"]["advertised"]["generic-receive-offload"], "off")
        self.assertTrue(calls)
        self.assertFalse(any("-K" in arg for argv in calls for arg in argv))
        self.assertEqual((root / "sys/class/net/stor0/features").read_text(encoding="utf-8"), "tso=on gro=on\n")
        table = (paths.root / "var/lib/san/webui/boards/perf.txt").read_text(encoding="utf-8")
        self.assertIn("iops", table)
        self.assertIn("p99_us", table)
        self.assertNotIn("button", table.lower())

    def test_defaults_stay_when_the_bench_does_not_move(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        same = _bench("burst-64k", {"burst": "64k"}, (40, 1000), (40, 1000))
        paths, probe = _layout(tmp.name, same)
        self.assertFalse(bench_moves(same))
        self.assertEqual(burst_parameters(paths), {})
        report = apply_tuning(
            paths, probe,
            sysfs=Path(tmp.name) / "sys",
            proc=Path(tmp.name) / "proc",
            configfs=Path(tmp.name) / "sys/kernel/config",
            measure=lambda: 10,
        )
        burst = [item for item in report["steps"] if item["name"] == "burst"][0]
        self.assertEqual(burst["action"], "skip")
        root = Path(tmp.name)
        self.assertEqual((root / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/ImmediateData").read_text(encoding="utf-8").strip(), "No")
        self.assertEqual((root / "sys/kernel/config/target/iscsi/iqn.2026-11.local.san:disk/tpgt_1/param/FirstBurstLength").read_text(encoding="utf-8").strip(), "8192")

    def test_irq_is_left_alone_when_the_list_is_absent(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths, probe = _layout(tmp.name)
        root = Path(tmp.name)
        (root / "sys/class/net/stor0/device/msi_irqs/25").unlink()
        (root / "sys/class/net/stor0/device/msi_irqs").rmdir()
        self.assertIsNone(irq_list(root / "sys", "stor0"))
        report = apply_tuning(
            paths, probe,
            sysfs=root / "sys",
            proc=root / "proc",
            configfs=root / "sys/kernel/config",
            measure=lambda: 10,
        )
        irq = [item for item in report["steps"] if item["name"] == "irq"][0]
        self.assertEqual(irq["action"], "skip")
        self.assertEqual((root / "proc/irq/25/smp_affinity").read_text(encoding="utf-8").strip(), "3")

    def test_a_failed_step_leaves_iscsi_and_the_next_change(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths, probe = _layout(tmp.name)
        import san_tui.blocktune as blocktune
        original = blocktune._Scheduler.apply

        def explode(self):
            raise OSError("refus")

        blocktune._Scheduler.apply = explode
        self.addCleanup(setattr, blocktune._Scheduler, "apply", original)
        report = apply_tuning(
            paths, probe,
            sysfs=Path(tmp.name) / "sys",
            proc=Path(tmp.name) / "proc",
            configfs=Path(tmp.name) / "sys/kernel/config",
            measure=lambda: 10,
        )
        self.assertEqual(report["iscsi"], "unchanged")
        scheduler = [item for item in report["steps"] if item["name"] == "scheduler"][0]
        self.assertEqual(scheduler["action"], "skip")
        self.assertEqual((Path(tmp.name) / "sys/block/sdb/queue/read_ahead_kb").read_text(encoding="utf-8").strip(), "0")
        self.assertEqual((Path(tmp.name) / "sys/block/sdb/queue/scheduler").read_text(encoding="utf-8").strip(), "[mq-deadline] none")

    def test_undo_restores_and_the_next_boot_does_not_reapply(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths, probe = _layout(tmp.name)
        sysfs = Path(tmp.name) / "sys"
        proc = Path(tmp.name) / "proc"
        configfs = sysfs / "kernel/config"
        apply_tuning(paths, probe, sysfs=sysfs, proc=proc, configfs=configfs, measure=lambda: 10)
        undo(paths, sysfs=sysfs, proc=proc, configfs=configfs)
        root = Path(tmp.name)
        self.assertEqual((root / "sys/block/sdb/queue/scheduler").read_text(encoding="utf-8").strip(), "mq-deadline")
        self.assertEqual((root / "sys/block/sdb/queue/read_ahead_kb").read_text(encoding="utf-8").strip(), "128")
        self.assertEqual((root / "proc/irq/25/smp_affinity").read_text(encoding="utf-8").strip(), "3")
        self.assertEqual((root / "sys/block/sdb/queue/nr_requests").read_text(encoding="utf-8").strip(), "128")
        self.assertEqual((root / "sys/kernel/config/target/core/iblock_0/lun/attrib/emulate_tpu").read_text(encoding="utf-8").strip(), "0")
        _write(root / "sys/block/sdb/queue/scheduler", "[mq-deadline] none\n")
        again = apply_tuning(paths, probe, sysfs=sysfs, proc=proc, configfs=configfs, measure=lambda: 10)
        self.assertTrue(all(item["action"] == "skip" for item in again["steps"]))
        self.assertEqual((root / "sys/block/sdb/queue/scheduler").read_text(encoding="utf-8").strip(), "[mq-deadline] none")

    def test_queue_depth_comes_from_the_disk_and_nowhere_else(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        block = root / "block"
        _write(block / "device/queue_depth", "16\n")
        _write(block / "mq/0/nr_tags", "128\n")
        self.assertEqual(measured_queue_depth(block), 16)
        missing = root / "empty"
        missing.mkdir()
        self.assertIsNone(measured_queue_depth(missing))
        self.assertEqual(cpus_from_mask("2", 2), {1})

    def test_offload_observation_does_not_change_the_values(self):
        observed = observe_offload({
            "tcp-segmentation-offload": "on [fixed]",
            "generic-receive-offload": "off",
        })
        self.assertEqual(observed["changed"], False)
        self.assertEqual(observed["advertised"]["tcp-segmentation-offload"], "on")
        self.assertEqual(observed["advertised"]["generic-receive-offload"], "off")

    def test_fast_path_is_not_encrypted_and_unmap_has_no_screen(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths, probe = _layout(tmp.name)
        device = Path(tmp.name) / "dev-sdb"
        device.write_bytes(b"\0" * 4096)
        volume = {"id": "/dev/disk/by-id/wwn-raw", "path": str(device)}
        called = []

        def banned(*_args, **_kwargs):
            called.append("format")
            raise AssertionError("luksFormat")

        import san_tui.luks as luks
        self.addCleanup(setattr, luks, "format_volume", format_volume)
        self.addCleanup(setattr, luks, "open_volume", open_volume)
        luks.format_volume = banned
        luks.open_volume = banned
        report = prepare(paths, probe)
        self.assertEqual(report["opened"], [])
        self.assertEqual(report["encryption"], "hors du chemin rapide")
        self.assertEqual(called, [])
        self.assertEqual(exported_path(volume), str(device))
        self.assertIsNone(refusal_for(paths, volume))
        original_luks = luks.is_luks
        luks.is_luks = lambda _device: True
        self.addCleanup(setattr, luks, "is_luks", original_luks)
        self.assertEqual(refusal_for(paths, volume), "chiffrement hors du chemin rapide")
        self.assertNotIn("UNMAP", SCREENS)
        self.assertNotIn("Ordonnanceur", SCREENS)

        class Storage:
            def __init__(self):
                self.done = []

            def set_attribute(self, name, value):
                self.done.append((name, value))

        storage = Storage()
        apply_vaai(storage)
        self.assertIn(("emulate_tpu", 1), storage.done)
        from san_tui.auto import mark_undone
        mark_undone(paths, "unmap")
        cleared = Storage()
        apply_vaai(cleared, paths)
        self.assertIn(("emulate_tpu", 0), cleared.done)

    def test_table_is_written_without_a_bench(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        result = write_perf_table(paths)
        self.assertTrue(result["written"])
        text = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("iops", text)
        self.assertIn("aucun banc", text)


if __name__ == "__main__":
    unittest.main()
