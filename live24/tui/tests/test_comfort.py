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
from san_tui.comfort import run
from san_tui.groupsnap import snapshot_groups
from san_tui.inventory_api import InventoryError, bind_server
from san_tui.iscsi import add_export, plan_export, portal_guards, restore
from san_tui.latency import check, check_from_disk, threshold_us
from san_tui.metrics import MetricsError, _bind, metrics_host, open_metrics
from san_tui.paths import SanPaths
from san_tui.persist import load_exports, save_exports, select_support
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


if __name__ == "__main__":
    unittest.main()
