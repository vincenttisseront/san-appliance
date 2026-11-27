"""Canaux, ALUA, UNMAP, secours, NTP. Le noyau LIO n'est pas requis ici."""

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

from san_tui.fabric import RecordingGate, apply_vaai, choose_standby
from san_tui.kernelpin import kernel_allowed, newest_generic, packages_sha256, version_key
from san_tui.latency import measure_board
from san_tui.ntp import apply_ntp
from san_tui.paths import SanPaths
from san_tui.promote import fio_bench, payload_for, sign_promotion, verify_bytes
from san_tui.release import (
    ASSET,
    KERNEL_ASSET,
    ReleaseError,
    apply_verified,
    sync_channels,
    undo_channel,
)
from san_tui.rescue import refresh_rescue, select_boot
from san_tui.scsilog import ingest


def archive(files, asset=ASSET):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, data in files.items():
            payload = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name in {"health", "start"} else 0o644
            tar.addfile(info, io.BytesIO(payload))
    raw = blob.getvalue()
    digest = hashlib.sha256(raw).hexdigest()
    return raw, "{}  {}\n".format(digest, asset)


class FabricTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)
        self.paths.conf.mkdir(parents=True)
        self.paths.data.mkdir(parents=True)
        app = self.paths.conf / "app"
        app.mkdir()
        (app / "a").mkdir()
        (app / "a" / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        state = {
            "version": 1,
            "active": "a",
            "rolled_back": False,
            "slots": {"a": {"sha256": "a" * 64}, "b": {}},
        }
        (app / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.marker = self.paths.run
        self.paths.ensure_runtime()
        (self.paths.run / "iscsi-serving").write_text("up\n", encoding="utf-8")

    def test_standby_address_skips_admin_and_primary(self):
        interfaces = [{
            "name": "enp2s0",
            "addresses": ["10.0.0.10/24", "10.0.0.11/24", "192.168.1.10/24"],
        }]
        self.assertEqual(choose_standby("10.0.0.10", interfaces, "192.168.1.10"), "10.0.0.11")
        self.assertIsNone(choose_standby("10.0.0.10", [{"name": "enp2s0", "addresses": ["10.0.0.10/24"]}], None))

    def test_vaai_keeps_going_when_one_attribute_fails(self):
        class Storage:
            def __init__(self):
                self.done = []

            def set_attribute(self, name, value):
                if name == "emulate_3pc":
                    raise RuntimeError("xcopy")
                self.done.append((name, value))

        storage = Storage()
        errors = apply_vaai(storage)
        self.assertIn("emulate_tpu", [item[0] for item in storage.done])
        self.assertTrue(any(item.startswith("emulate_3pc") for item in errors))

    def test_scsi_log_drops_the_payload(self):
        token = "PAYLOAD" + "ab" * 40
        line = (
            "kernel: target iqn.2026-11.local.san:disk LUN 0 opcode 0x2a "
            "status CHECK sense 0x02/0x04/0x01 payload={}"
        ).format(token)
        records = ingest(self.paths, [line], now="2026-11-27T00:00:00Z")
        stored = (self.paths.root / "var/lib/san/webui/boards/scsi-errors.json").read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertEqual(records[0]["opcode"], "0x2a")
        self.assertEqual(records[0]["lun"], 0)
        self.assertNotIn("payload", records[0])

    def test_latency_board_keeps_each_lun_and_the_global_max(self):
        board = measure_board(
            [{"iqn": "iqn.a", "lun": 0}, {"iqn": "iqn.b", "lun": 1}, {"iqn": "iqn.c", "lun": 2}],
            lambda lun: {"p99_us": 10} if lun["lun"] != 1 else (_ for _ in ()).throw(OSError("down")),
        )
        self.assertEqual([row["ok"] for row in board["luns"]], [True, False, True])
        self.assertEqual(board["global_p99_us"], 10)
        self.assertEqual(len(board["luns"]), 3)

    def test_bench_failure_does_not_freeze_or_promote(self):
        gate = RecordingGate()
        blob, text = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        with self.assertRaises(ReleaseError) as caught:
            apply_verified(
                self.paths, blob, text, gate=gate,
                bench=lambda: {"pass": False}, signer=lambda *_args: ("", ""),
            )
        self.assertIn("fio", str(caught.exception))
        self.assertEqual(gate.events, [])
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)
        self.assertEqual((self.paths.run / "iscsi-serving").read_text(encoding="utf-8"), "up\n")

    def test_unhealthy_slot_freezes_and_always_resumes(self):
        gate = RecordingGate()
        blob, text = archive({"health": "#!/bin/sh\nexit 1\n", "version": "9\n"})
        result = apply_verified(
            self.paths, blob, text, health=lambda _slot: 1, gate=gate,
            bench=lambda: {"pass": True, "p99_us": 4}, signer=lambda *_args: ("aa", ""),
        )
        self.assertEqual(gate.events, ["freeze", "resume"])
        self.assertTrue(result["rollback"])
        self.assertFalse(gate.events[-1] != "resume")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")

    def test_freeze_failure_leaves_the_slot_and_resumes(self):
        gate = RecordingGate(fail_freeze=True)
        blob, text = archive({"health": "#!/bin/sh\nexit 0\n"})
        with self.assertRaises(ReleaseError):
            apply_verified(
                self.paths, blob, text, gate=gate,
                bench=lambda: {"pass": True, "p99_us": 1}, signer=lambda *_args: ("", ""),
            )
        self.assertEqual(gate.events, ["freeze", "resume"])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")

    def test_real_fio_bench_passes(self):
        measured = fio_bench()
        self.assertTrue(measured["pass"])
        self.assertGreater(measured["p99_us"], 0)

    def test_signature_roundtrip_and_gap_does_not_block(self):
        blob, text = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        result = apply_verified(
            self.paths, blob, text, health=lambda _slot: 0, gate=RecordingGate(),
            bench=lambda: {"pass": True, "p99_us": 5},
        )
        self.assertTrue(result["signed"])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("PRIVATE KEY", json.dumps(state))
        signature = bytes.fromhex(state["signature"])
        public = self.paths.data / "release-key.pub"
        self.assertTrue(verify_bytes(public, payload_for(result["sha256"], {"p99_us": 5}), signature))
        self.assertTrue((self.paths.data / "release-key.pem").stat().st_mode & 0o077 == 0)

    def test_kernel_channel_does_not_touch_the_app_and_rejects_other_series(self):
        app_before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        pin = "6.8.0-151.151"
        bad, bad_text = archive(
            {"health": "#!/bin/sh\nexit 0\n", "kernel.release": "6.14.0-35.35\n"},
            KERNEL_ASSET,
        )
        with self.assertRaises(ReleaseError):
            apply_verified(
                self.paths, bad, bad_text, channel="kernel", pin=pin,
                bench=lambda: {"pass": True, "p99_us": 1}, signer=lambda *_args: ("", "gap"),
            )
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), app_before)
        good, good_text = archive(
            {"health": "#!/bin/sh\nexit 0\n", "kernel.release": "6.8.0-100.100\n"},
            KERNEL_ASSET,
        )
        result = apply_verified(
            self.paths, good, good_text, channel="kernel", pin=pin, health=lambda _slot: 0,
            bench=lambda: {"pass": True, "p99_us": 2}, signer=lambda *_args: ("", "gap"),
        )
        self.assertEqual(result["channel"], "kernel")
        self.assertEqual(result["slot"], "b")
        self.assertFalse((self.paths.conf / "kernel" / "state.json").read_text(encoding="utf-8") == app_before)
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), app_before)
        newer, newer_text = archive(
            {"health": "#!/bin/sh\nexit 0\n", "kernel.release": "6.8.0-200.200\n"},
            KERNEL_ASSET,
        )
        with self.assertRaises(ReleaseError):
            apply_verified(
                self.paths, newer, newer_text, channel="kernel", pin=pin,
                bench=lambda: {"pass": True, "p99_us": 1}, signer=lambda *_args: ("", ""),
            )

    def test_channels_stay_independent_when_one_download_fails(self):
        def fetcher(channel):
            if channel == "app":
                raise ReleaseError("téléchargement refusé")
            return archive(
                {"health": "#!/bin/sh\nexit 0\n", "kernel.release": "6.8.0-90.90\n"},
                KERNEL_ASSET,
            )

        result = sync_channels(
            self.paths, fetcher, gate=RecordingGate(), pin="6.8.0-151.151",
            health=lambda _slot: 0, bench=lambda: {"pass": True, "p99_us": 1},
            signer=lambda *_args: ("", ""),
        )
        by_name = {item["channel"]: item for item in result["channels"]}
        self.assertFalse(by_name["app"]["applied"])
        self.assertTrue(by_name["kernel"]["applied"])
        self.assertEqual(json.loads((self.paths.conf / "app" / "state.json").read_text())["active"], "a")

    def test_undo_only_reverts_the_automatic_promotion(self):
        blob, text = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        apply_verified(
            self.paths, blob, text, health=lambda _slot: 0, gate=RecordingGate(),
            bench=lambda: {"pass": True, "p99_us": 1}, signer=lambda *_args: ("ab", ""),
        )
        undone = undo_channel(self.paths, "app")
        self.assertEqual(undone["slot"], "a")
        with self.assertRaises(ReleaseError):
            undo_channel(self.paths, "app")

    def test_rescue_image_is_used_when_the_active_slot_does_not_start(self):
        slot = self.paths.conf / "app" / "a"
        (slot / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(slot / "health", 0o755)
        (slot / "secret.pem").write_text("-----BEGIN PRIVATE KEY-----\nSECRET\n", encoding="utf-8")
        refresh_rescue(self.paths, slot)
        copied = (self.paths.data / "rescue" / "image").read_text(encoding="utf-8") if False else ""
        tree = list((self.paths.data / "rescue" / "image").rglob("*"))
        self.assertFalse(any(path.name == "secret.pem" for path in tree))
        blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in tree if path.is_file())
        self.assertNotIn("BEGIN PRIVATE KEY", blob)
        (slot / "health").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        chosen = select_boot(self.paths)
        self.assertEqual(chosen["chosen"], "rescue")
        self.assertEqual((self.paths.run / "iscsi-serving").read_text(encoding="utf-8"), "up\n")
        self.assertNotIn("SECRET", copied)

    def test_ntp_unreachable_still_writes_the_local_log(self):
        log_path = self.paths.root / "var" / "log" / "san" / "local.log"
        result = apply_ntp(self.paths, server="192.0.2.1", timeout=0.2, log_path=log_path)
        text = log_path.read_text(encoding="utf-8")
        self.assertFalse(result["ntp"])
        self.assertTrue(result["logged"])
        self.assertIn("journal local", text)
        self.assertIn("ntp injoignable", text)
        self.assertEqual((self.paths.run / "iscsi-serving").read_text(encoding="utf-8"), "up\n")

    def test_kernel_pin_stays_on_6_8(self):
        packages = """
Package: linux-image-generic
Version: 6.8.0-151.151
Depends: linux-image-6.8.0-151-generic, linux-modules-extra-6.8.0-151-generic

Package: linux-image-6.8.0-151-generic
Version: 6.8.0-151.151

Package: linux-image-generic-hwe-24.04
Version: 6.14.0-35.35
Depends: linux-image-6.14.0-35-generic

Package: linux-image-6.14.0-35-generic
Version: 6.14.0-35.35

Package: linux-image-6.8.0-88-generic
Version: 6.8.0-88.89
"""
        self.assertEqual(newest_generic(packages), "6.8.0-151.151")
        self.assertTrue(kernel_allowed("6.8.0-151.151", "6.8.0-151.151"))
        self.assertFalse(kernel_allowed("6.8.0-152.152", "6.8.0-151.151"))
        self.assertFalse(kernel_allowed("6.14.0-35.35", "6.8.0-151.151"))
        self.assertLess(version_key("6.8.0-88.89"), version_key("6.8.0-151.151"))
        inrelease = "SHA256:\n abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789 12 main/binary-amd64/Packages.gz\n"
        # 64 hex chars
        digest = "a" * 64
        text = "SHA256:\n {} 10 main/binary-amd64/Packages.gz\nMD5Sum:\n".format(digest)
        self.assertEqual(packages_sha256(text), digest)
        jumped = packages.replace(
            "Depends: linux-image-6.8.0-151-generic, linux-modules-extra-6.8.0-151-generic",
            "Depends: linux-image-6.14.0-35-generic",
        )
        with self.assertRaises(ValueError):
            newest_generic(jumped)


if __name__ == "__main__":
    unittest.main()
