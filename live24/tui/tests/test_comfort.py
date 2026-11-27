"""Certificat, session, export, import par WWN, mode usine. Aucun disque physique."""

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from san_tui.cli import main
from san_tui.configio import (
    factory_reset,
    import_config,
    read_builtin_export,
    unseal,
)
from san_tui.iscsi import plan_export, restore
from san_tui.paths import SanPaths
from san_tui.persist import PersistError, load_exports, select_support


SECRET = "chap-secret-ok"
OTHER = "chap-other-ok1"
REPO = Path(__file__).resolve().parents[3]
PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}


def volume(name, wwn, serial="SER"):
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


class Probe:
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

    def interface_has_address(self, iface, address):
        return address in [item.split("/", 1)[0] for item in self.addresses.get(iface, [])]


class Recording:
    def __init__(self):
        self.applied = []

    def apply(self, target, secret, probe):
        self.applied.append((target["iqn"], secret))


class ComfortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)

    def test_export_file_is_already_built_and_hides_chap(self):
        conf = volume("wwn-conf", "naa.conf")
        lun = volume("wwn-old", "naa.lun", "LUN")
        missing = volume("wwn-missing", "naa.missing", "GONE")
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
        add_export(self.paths, PLAN, first, SECRET, lun["id"])
        add_export(self.paths, PLAN, second, OTHER, missing["id"])
        built = (self.paths.conf / "san-config.json").read_bytes()
        self.assertNotIn(SECRET.encode(), built)
        self.assertNotIn(OTHER.encode(), built)
        self.assertNotIn(base64_of(SECRET), built)
        document = json.loads(built.decode())
        self.assertEqual(document["version"], 2)
        self.assertNotIn("id", document["targets"][0]["volume"])
        replaced = b'{"version":2,"product":"san-appliance","ready":true}\n'
        (self.paths.conf / "san-config.json").write_bytes(replaced)
        self.assertEqual(read_builtin_export(self.paths), replaced)

    def test_import_reattaches_by_wwn_and_skips_a_missing_one(self):
        source = SanPaths(self.tmp.name + "/source")
        conf = volume("wwn-conf", "naa.conf")
        lun = volume("wwn-old", "naa.lun", "LUN")
        missing = volume("wwn-missing", "naa.missing", "GONE")
        select_support(source, "conf", conf, conf["id"])
        from san_tui.iscsi import add_export
        add_export(source, PLAN, plan_export(
            lun, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:keep", "iqn.2026-11.local.san:init", "client1",
        ), SECRET, lun["id"])
        add_export(source, PLAN, plan_export(
            missing, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:skip", "iqn.2026-11.local.san:init", "client1",
        ), OTHER, missing["id"])
        blob = (source.conf / "san-config.json").read_bytes()
        fresh_conf = volume("wwn-conf-new", "naa.conf", "CONF2")
        fresh_lun = volume("wwn-new", "naa.lun", "LUN2")
        probe = Probe([fresh_conf, fresh_lun])
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
        self.assertEqual(secrets["iqn.2026-11.local.san:keep:iqn.2026-11.local.san:init"], SECRET)
        exported = (self.paths.conf / "san-config.json").read_bytes()
        self.assertNotIn(SECRET.encode(), exported)
        alerts = (self.paths.run / "alerts.json").read_text(encoding="utf-8")
        self.assertIn("naa.missing", alerts)
        self.assertNotIn(SECRET, alerts)
        self.assertNotIn(OTHER, alerts)
        backend = Recording()
        restored = restore(self.paths, probe, backend)
        self.assertEqual(restored["exported"], ["iqn.2026-11.local.san:keep"])
        self.assertEqual(backend.applied, [("iqn.2026-11.local.san:keep", SECRET)])
        self.assertTrue(any(item["iqn"].endswith(":skip") for item in restored["skipped"]) is False)

    def test_restore_still_exports_when_the_webui_session_is_dead(self):
        conf = volume("wwn-conf", "naa.conf")
        lun = volume("wwn-lun", "naa.lun", "LUN")
        probe = Probe([conf, lun])
        select_support(self.paths, "conf", conf, conf["id"])
        from san_tui.iscsi import add_export
        add_export(self.paths, PLAN, plan_export(
            lun, {"address": "10.0.0.10", "port": 3260},
            "iqn.2026-11.local.san:keep", "iqn.2026-11.local.san:init", "client1",
        ), SECRET, lun["id"])
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
        blob = unseal(__import__("san_tui.configio", fromlist=["seal"]).seal({"ref": SECRET}))
        self.assertEqual(blob["ref"], SECRET)
        from san_tui.configio import seal
        sealed = seal({"ref": SECRET})
        sealed["mac"] = "AA=="
        with self.assertRaises(PersistError):
            unseal(sealed)


def base64_of(text):
    import base64
    return base64.b64encode(text.encode()).decode().encode()


if __name__ == "__main__":
    unittest.main()
