"""Choix automatiques, release, import ESOS. Pas d'appel au noyau."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from san_tui.autoboot import auto_boot, ensure_recorded, layout_commands, publish_status
from san_tui.evolve import (
    MIN_DATA_BYTES,
    SIGNING_GAP,
    assign_exports,
    backup_matches,
    choose_governor,
    choose_mtu,
    choose_network,
    export_refusal,
    generate_chap,
    import_esos_iscsi,
    io_uring_decision,
    is_newer,
    pick_data_disk,
    plan_free_layout,
    portal_problem,
    public_exports,
    release_rejected,
    safe_audit,
    session_is_writing,
    signature_decision,
)
from san_tui.paths import SanPaths
from san_tui.persist import rollback_files
from san_tui.release import (
    ASSET,
    apply_verified,
    boot_apply,
    post_apply_health,
    rollback_slot,
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


def disk(name, size, boot=False, signature=""):
    return {
        "id": "/dev/disk/by-id/" + name,
        "kernel": name,
        "path": "/dev/" + name,
        "wwn": "naa." + name,
        "serial": name,
        "size": size,
        "block_size": 512,
        "mounts": [],
        "holders": [],
        "active": False,
        "boot": boot,
        "kind": "block",
        "signature": signature,
    }


class ChoiceTests(unittest.TestCase):
    def test_data_disk_refuses_boot_and_small_media(self):
        boot = disk("boot", MIN_DATA_BYTES * 2, boot=True)
        small = disk("small", 32 * 1024 * 1024)
        used = disk("used", MIN_DATA_BYTES, signature="ext4")
        big = disk("data", MIN_DATA_BYTES + 10)
        bigger = disk("data2", MIN_DATA_BYTES * 3)
        chosen, reason = pick_data_disk([boot, small, used, big, bigger])
        self.assertIsNone(reason)
        self.assertEqual(chosen["id"], bigger["id"])
        self.assertEqual(pick_data_disk([boot, small])[0], None)
        self.assertIsNotNone(export_refusal(boot))
        self.assertIsNone(export_refusal(big))

    def test_layout_uses_only_blank_non_boot_disks(self):
        boot = disk("boot", MIN_DATA_BYTES * 2, boot=True)
        left = disk("a", MIN_DATA_BYTES)
        right = disk("b", MIN_DATA_BYTES + 1000)
        foreign = disk("f", MIN_DATA_BYTES, signature="linux_raid_member")
        layout = plan_free_layout([boot, left, right, foreign], [left["id"]])
        self.assertEqual(layout["raid"]["arrays"], [])
        self.assertEqual(layout["lvm"]["groups"][0]["pvs"], [right["id"]])
        paired = plan_free_layout([left, right], [])
        self.assertEqual(paired["raid"]["arrays"][0]["members"], [right["id"], left["id"]])
        commands = " ".join(" ".join(cmd) for cmd in layout_commands(paired))
        self.assertNotIn("boot", commands)
        self.assertIn(left["id"], commands)

    def test_network_mtu_governor_and_uring(self):
        plan, reason = choose_network([
            {"name": "lo", "addresses": ["127.0.0.1/8"]},
            {"name": "admin0", "addresses": ["192.168.10.10/24"]},
            {"name": "stor0", "addresses": ["10.1.0.10/24"], "max_mtu": 9000, "peer_mtu": 1500},
            {"name": "stor1", "addresses": ["10.2.0.10/24"], "max_mtu": 9000, "peer_mtu": 9000},
        ])
        self.assertIsNone(reason)
        self.assertEqual(plan["admin"]["iface"], "admin0")
        self.assertEqual(plan["storage"]["iface"], "stor0")
        self.assertEqual(plan["storages"][0]["mtu"], 1500)
        self.assertEqual(plan["storages"][1]["mtu"], 9000)
        self.assertEqual(choose_mtu(9000, None), 1500)
        self.assertEqual(choose_mtu(9000, 9000), 9000)
        self.assertEqual(choose_network([{"name": "only", "addresses": []}])[0], None)
        self.assertFalse(choose_governor(False)["set"])
        self.assertEqual(choose_governor(True)["governor"], "performance")
        skipped = io_uring_decision(False, True)
        self.assertFalse(skipped["enabled"])
        self.assertIn("rtslib", skipped["reason"])
        self.assertTrue(io_uring_decision(True, True)["enabled"])

    def test_one_writer_per_volume(self):
        volume = disk("data", MIN_DATA_BYTES)
        storages = [
            {"iface": "stor0", "address": "10.1.0.10/24"},
            {"iface": "stor1", "address": "10.2.0.10/24"},
        ]
        pairs, reasons = assign_exports(storages, [volume])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(reasons[0]["reason"], "un seul écrivain par volume")
        self.assertEqual(pairs[0]["portal"]["address"], "10.1.0.10")


class GuardTests(unittest.TestCase):
    def test_backup_restore_requires_the_same_volume(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        backup = paths.conf / "backups" / "20200101T000000Z"
        backup.mkdir(parents=True)
        volume = disk("data", MIN_DATA_BYTES)
        document = {"version": 1, "targets": [{"volume": {
            "id": volume["id"], "wwn": volume["wwn"], "serial": volume["serial"],
            "size": volume["size"], "block_size": 512,
        }}]}
        (backup / "exports.json").write_text(json.dumps(document), encoding="utf-8")
        (paths.conf / "exports.json").write_text('{"version":1,"targets":[]}\n', encoding="utf-8")
        other = dict(volume)
        other["wwn"] = "naa.other"
        with self.assertRaises(Exception) as caught:
            rollback_files(paths, [other])
        self.assertIn("WWN", str(caught.exception))
        self.assertEqual(json.loads((paths.conf / "exports.json").read_text(encoding="utf-8"))["targets"], [])
        self.assertIsNone(backup_matches(document, [volume]))
        restored = rollback_files(paths, [volume])
        self.assertIn("20200101T000000Z", restored)

    def test_sessions_and_portal(self):
        writing, reason = session_is_writing([{"present": True, "out_cmds": 2}])
        self.assertTrue(writing)
        self.assertIn("écriture", reason)
        idle, _why = session_is_writing([{"present": True, "out_cmds": 0}])
        self.assertFalse(idle)
        unknown, why = session_is_writing([{"present": True, "out_cmds": None}])
        self.assertTrue(unknown)
        self.assertIn("non mesurables", why)
        self.assertIsNone(portal_problem(["10.1.0.10:3260"], [("10.1.0.10", 3260)]))
        self.assertEqual(portal_problem([], [("10.1.0.10", 3260)]), "portail iSCSI absent")
        self.assertIsNone(portal_problem([], []))

    def test_chap_is_generated_and_not_logged(self):
        secret = generate_chap(lambda _n: b"\x11" * 8)
        self.assertEqual(len(secret), 16)
        self.assertNotIn(" ", secret)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = SanPaths(tmp.name)
        paths.conf.mkdir(parents=True)
        (paths.conf / "chosen.json").write_text(json.dumps({
            "version": 2,
            "conf": {"id": "/dev/disk/by-id/conf"},
        }), encoding="utf-8")
        paths.etc.mkdir(parents=True)
        (paths.etc / "network.json").write_text(json.dumps({
            "admin": {"iface": "admin0", "address": "192.168.10.10/24"},
            "storage": {"iface": "stor0", "address": "10.1.0.10/24"},
            "storages": [{"iface": "stor0", "address": "10.1.0.10/24"}],
        }), encoding="utf-8")
        volume = disk("data", MIN_DATA_BYTES)
        result = ensure_recorded(
            paths, [volume], ["iqn.2026-11.local.client:host"], [], modules_nvme=False,
        )
        self.assertEqual(result["exports"], "créé")
        secrets = json.loads((paths.conf / "secrets.json").read_text(encoding="utf-8"))
        stored = next(iter(secrets.values()))
        text = []
        for path in Path(tmp.name).rglob("*"):
            if path.is_file() and path.name != "secrets.json":
                text.append(path.read_text(encoding="utf-8", errors="ignore"))
        blob = "\n".join(text)
        self.assertNotIn(stored, blob)
        self.assertEqual((paths.conf / "secrets.json").stat().st_mode & 0o077, 0)

    def test_public_page_has_no_secret_and_shows_a_missing_disk(self):
        document = {"targets": [{
            "iqn": "iqn.2026-11.local.san:disk0",
            "lun": 0,
            "volume": {"id": "/dev/disk/by-id/data"},
            "acls": [{"initiator": "iqn.2026-11.local.client:host", "chap_user": "init1"}],
        }]}
        alerts = [{"message": "iqn.2026-11.local.san:disk0 : volume absent : /dev/disk/by-id/data"}]
        rows = public_exports(document, alerts)
        self.assertEqual(rows[0]["state"], "refusé")
        self.assertIn("absent", rows[0]["alert"])
        self.assertNotIn("chap", json.dumps(rows))

    def test_esos_import_is_limited(self):
        record = import_esos_iscsi(json.dumps({
            "iqn": "iqn.2020-01.local.esos:disk",
            "lun": 0,
            "wwid": "naa.6001",
            "block_size": 512,
            "acl": "iqn.2020-01.local.client:host",
        }))
        self.assertFalse(record["writable_with_peer"])
        with self.assertRaises(ValueError):
            import_esos_iscsi("TARGET_DRIVER iscsi { HANDLER vdisk_blockio }")
        with self.assertRaises(ValueError):
            import_esos_iscsi(json.dumps({"iqn": "iqn.x", "lun": 0, "wwid": "n", "block_size": 512, "acl": "iqn.y", "handler": "vdisk"}))

    def test_signature_gap_and_p99(self):
        gap = signature_decision(False, False, False)
        self.assertFalse(gap["checked"])
        self.assertFalse(gap["blocked"])
        self.assertIn("Aucune clé", gap["gap"])
        self.assertEqual(gap["gap"], SIGNING_GAP)
        blocked = signature_decision(True, True, False)
        self.assertTrue(blocked["blocked"])
        self.assertTrue(release_rejected(
            {"workloads": {"read": {"iops": 100, "p99_us": 10, "throughput_bps": 100, "cpu_per_gb": 1}}},
            {"workloads": {"read": {"iops": 100, "p99_us": 20, "throughput_bps": 100, "cpu_per_gb": 1}}},
        )[0])
        self.assertFalse(release_rejected({"workloads": {}}, None)[0])
        row = safe_audit({"action": "apply", "sha256": "ab" * 32, "reason": "secret-value", "chap_secret": "nope"}, ["secret-value"])
        self.assertEqual(len(row["sha256"]), 12)
        self.assertNotIn("secret-value", json.dumps(row))
        self.assertNotIn("nope", json.dumps(row))

    def test_versions(self):
        self.assertTrue(is_newer("0", "v1.2.0"))
        self.assertFalse(is_newer("2", "1"))
        self.assertFalse(is_newer("good", "better"))


class ReleaseBootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)
        self.paths.conf.mkdir(parents=True)
        app = self.paths.conf / "app"
        app.mkdir()
        (app / "a").mkdir()
        (app / "a" / "version").write_text("1\n", encoding="utf-8")
        (app / "a" / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (app / "state.json").write_text(json.dumps({
            "version": 1, "active": "a", "rolled_back": False,
            "slots": {"a": {"sha256": "a" * 64}, "b": {}},
        }), encoding="utf-8")
        image = self.paths.root / "usr/lib/san-app/image"
        image.mkdir(parents=True)
        (image / "version").write_text("0\n", encoding="utf-8")
        (image / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def test_writing_session_does_not_switch_the_slot(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        digest = hashlib.sha256(blob).hexdigest()
        result = apply_verified(
            self.paths, blob, "{}  {}\n".format(digest, ASSET), writing=True,
        )
        self.assertFalse(result["applied"])
        self.assertIn("écriture", result["reason"])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")
        self.assertFalse((self.paths.conf / "app" / "b").exists())
        audit = (self.paths.conf / "app" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("ghp_", audit)

    def test_download_failure_leaves_the_slot(self):
        class Opener:
            def open(self, req, timeout=60):
                raise OSError("down")

        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        result = boot_apply(self.paths, Opener(), sessions=[])
        self.assertFalse(result["applied"])
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)

    def test_newer_release_and_image_fallback(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        digest = hashlib.sha256(blob).hexdigest()
        body = json.dumps({"assets": [
            {"name": ASSET, "browser_download_url": "http://127.0.0.1:8765/a"},
            {"name": ASSET + ".sha256", "browser_download_url": "http://127.0.0.1:8765/a.sha256"},
        ]}).encode()
        checksum = "{}  {}\n".format(digest, ASSET).encode()

        class Opener:
            def open(self, req, timeout=60):
                url = req.full_url
                if url.endswith("/releases/latest"):
                    return io.BytesIO(body)
                if url.endswith("/a.sha256"):
                    return io.BytesIO(checksum)
                if url.endswith("/a"):
                    return io.BytesIO(blob)
                raise OSError(url)

        result = boot_apply(self.paths, Opener(), api="http://127.0.0.1:8765", sessions=[])
        self.assertTrue(result["applied"])
        self.assertEqual(result["slot"], "b")
        self.assertEqual(result.get("signature"), "non vérifiée")
        (self.paths.conf / "exports.json").write_text(json.dumps({
            "version": 1,
            "targets": [{"portal": {"address": "10.1.0.10", "port": 3260}, "iqn": "iqn.x"}],
        }), encoding="utf-8")

        def health(slot):
            return 0 if slot.name == "image" else 1

        checked = post_apply_health(self.paths, ["127.0.0.1:3000"], health=health)
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["iscsi"], "inchangé")
        self.assertEqual(checked["slot"], "iso")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "iso")

    def test_manual_rollback_stops_at_the_image(self):
        # Le slot b est sain, puis le retour suivant va à l'image.
        (self.paths.conf / "app" / "b").mkdir()
        (self.paths.conf / "app" / "b" / "health").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        state["active"] = "b"
        (self.paths.conf / "app" / "state.json").write_text(json.dumps(state), encoding="utf-8")

        def health(_slot):
            return 0

        first = rollback_slot(self.paths, health=health)
        self.assertEqual(first["slot"], "a")
        second = rollback_slot(self.paths, health=health)
        self.assertEqual(second["slot"], "iso")
        third = rollback_slot(self.paths, health=health)
        self.assertIn("déjà", third["reason"])

    def test_auto_boot_does_not_format_a_small_or_boot_disk(self):
        calls = []

        def create(volume, _stamp):
            calls.append(volume["id"])

        result = auto_boot(
            self.paths,
            [disk("boot", MIN_DATA_BYTES * 2, boot=True), disk("tiny", 64 * 1024 * 1024)],
            [{"name": "admin0", "addresses": ["192.168.10.10/24"]},
             {"name": "stor0", "addresses": ["10.1.0.10/24"]}],
            create_fs=create,
        )
        self.assertEqual(calls, [])
        self.assertEqual(result["network"], "stor0")
        self.assertFalse(result["io_uring"]["enabled"])
        diagnostic = auto_boot(
            self.paths, [disk("data", MIN_DATA_BYTES * 2)], [],
            diagnostic=True, create_fs=create,
        )
        self.assertEqual(diagnostic["skipped"], "diagnostic")
        self.assertEqual(calls, [])
        publish_status(self.paths)
        status = json.loads((self.paths.run / "public-status.json").read_text(encoding="utf-8"))
        self.assertNotIn("chap_secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
