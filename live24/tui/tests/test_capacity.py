"""Décisions automatiques. Aucun disque, aucun LIO."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from san_tui.chap import new_secret
from san_tui.clone import new_wwn
from san_tui.iscsi import check_chap
from san_tui.lacp import should_bond
from san_tui.portal import choose_standby, next_address, primary_down, storage_reachable
from san_tui.quota import limits_for, load_profiles, profile_name_for_size
from san_tui.queues import apply_nvme_queues, queue_attribute, storage_cores
from san_tui.thin import classify_percent, refusal_for


PLAN = {
    "admin": {"iface": "enp1s0", "address": "192.168.1.10/24"},
    "storage": {"iface": "enp2s0", "address": "10.0.0.10/24"},
}


class Probe:
    def __init__(self, ifaces):
        self.ifaces = ifaces

    def list_interfaces(self):
        return list(self.ifaces)

    def interface_has_address(self, iface, address):
        for item in self.ifaces:
            if item["name"] == iface:
                return address in [a.split("/", 1)[0] for a in item.get("addresses") or []]
        return False


class ProfileTests(unittest.TestCase):
    def test_size_picks_a_profile(self):
        self.assertEqual(profile_name_for_size(1000), "small")
        self.assertEqual(profile_name_for_size(9 * 1024 ** 3), "standard")
        self.assertEqual(profile_name_for_size(200 * 1024 ** 3), "large")
        self.assertEqual(profile_name_for_size(None), "small")

    def test_limits_come_from_the_shipped_profile(self):
        profiles = load_profiles()
        limits = limits_for({"size": 1000}, profiles)
        self.assertEqual(limits["profile"], "small")
        self.assertEqual(limits["iops"], profiles["small"]["iops"])
        self.assertEqual(limits["throughput_bps"], profiles["small"]["throughput_bps"])


class ThinTests(unittest.TestCase):
    def test_alert_before_full_and_refusal_at_full(self):
        self.assertTrue(classify_percent(80)["alert"])
        self.assertFalse(classify_percent(80)["full"])
        self.assertTrue(classify_percent(100)["full"])
        self.assertFalse(classify_percent(100)["alert"])
        self.assertTrue(classify_percent(10, 100)["full"])
        self.assertIsNone(refusal_for({"path": "/dev/does-not-exist"}))


class PortalTests(unittest.TestCase):
    def test_standby_is_the_other_storage_nic(self):
        ifaces = [
            {"name": "enp1s0"},
            {"name": "enp2s0"},
            {"name": "enp3s0"},
            {"name": "lo"},
        ]
        self.assertEqual(choose_standby("enp1s0", "enp2s0", ifaces), "enp3s0")
        self.assertEqual(next_address("10.0.0.10/24"), "10.0.0.11/24")

    def test_missing_storage_stays_unreachable_without_a_standby(self):
        probe = Probe([
            {"name": "enp1s0", "addresses": ["192.168.1.10/24"], "operstate": "up"},
        ])
        self.assertTrue(primary_down(probe, "enp2s0"))
        self.assertFalse(storage_reachable(PLAN, probe, "10.0.0.10"))


class LacpTests(unittest.TestCase):
    def test_bond_only_when_both_storage_nics_see_lacp(self):
        self.assertFalse(should_bond("admin", "stor", None, {}))
        self.assertFalse(should_bond("admin", "stor", "admin", {"stor": True, "admin": True}))
        self.assertFalse(should_bond("admin", "stor", "stor2", {"stor": True, "stor2": False}))
        self.assertTrue(should_bond("admin", "stor", "stor2", {"stor": True, "stor2": True}))


class SecretTests(unittest.TestCase):
    def test_generated_chap_fits_the_target_rule(self):
        secret = new_secret()
        check_chap(secret)
        self.assertNotIn(" ", secret)

    def test_clone_wwn_is_new(self):
        first = new_wwn("naa.5000")
        second = new_wwn(first)
        self.assertTrue(first.startswith("naa."))
        self.assertNotEqual(first, "naa.5000")
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 4 + 16)


class QueueTests(unittest.TestCase):
    def test_missing_attribute_does_not_apply(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        report = apply_nvme_queues(tmp.name)
        self.assertFalse(report["applied"])
        self.assertIn("absent", report["reason"])
        self.assertGreaterEqual(report["cores"], 1)
        self.assertEqual(storage_cores(), report["cores"])
        (Path(tmp.name) / "attr_qid_max").write_text("0\n", encoding="utf-8")
        self.assertEqual(queue_attribute(tmp.name).name, "attr_qid_max")
        os.chmod(Path(tmp.name) / "attr_qid_max", stat.S_IRUSR | stat.S_IWUSR)
