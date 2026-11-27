"""Empreinte, slot inactif, et un seul retour au slot précédent."""

import hashlib
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from san_tui.paths import SanPaths
from san_tui.release import (
    ASSET,
    ASSET_SUM,
    ReleaseError,
    apply_verified,
    fetch_and_apply,
    fetch_release,
)


def archive(files):
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        for name, data in files.items():
            payload = data.encode() if isinstance(data, str) else data
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o755 if name.endswith("health") else 0o644
            tar.addfile(info, io.BytesIO(payload))
    return blob.getvalue()


def checksum(blob):
    digest = hashlib.sha256(blob).hexdigest()
    return digest, "{}  {}\n".format(digest, ASSET)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = SanPaths(self.tmp.name)
        self.paths.conf.mkdir(parents=True)
        self.paths.data.mkdir(parents=True)
        self.marker = self.paths.data / "marker"
        self.marker.write_text("keep-data\n", encoding="utf-8")
        state = {
            "version": 1,
            "active": "a",
            "rolled_back": False,
            "slots": {"a": {"sha256": "a" * 64}, "b": {}},
        }
        app = self.paths.conf / "app"
        app.mkdir()
        (app / "a").mkdir()
        (app / "a" / "version").write_text("1\n", encoding="utf-8")
        (app / "state.json").write_text(json.dumps(state), encoding="utf-8")
        self.before = (app / "state.json").read_text(encoding="utf-8")

    def test_bad_checksum_does_not_touch_the_slot_or_the_data(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        with self.assertRaises(ReleaseError) as caught:
            apply_verified(self.paths, blob, "0" * 64 + "  {}\n".format(ASSET))
        self.assertIn("empreinte", str(caught.exception))
        self.assertFalse((self.paths.conf / "app" / "b").exists())
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), self.before)
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")
        self.assertEqual((self.paths.conf / "app" / "a" / "version").read_text(encoding="utf-8"), "1\n")

    def test_happy_path_switches_to_the_inactive_slot(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        result = apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertEqual(result["slot"], "b")
        self.assertFalse(result["rollback"])
        self.assertEqual((self.paths.conf / "app" / "b" / "version").read_text(encoding="utf-8"), "2\n")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "b")
        self.assertEqual((self.paths.conf / "app" / "a" / "version").read_text(encoding="utf-8"), "1\n")
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")

    def test_unhealthy_slot_rolls_back_once(self):
        blob = archive({"health": "#!/bin/sh\nexit 1\n", "version": "9\n"})
        _, text = checksum(blob)
        calls = []

        def health(slot):
            calls.append(slot.name)
            return 1

        result = apply_verified(self.paths, blob, text, health=health)
        self.assertTrue(result["rollback"])
        self.assertEqual(result["slot"], "a")
        self.assertEqual(calls, ["b"])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")
        self.assertTrue(state["rolled_back"])
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")

    def test_path_escape_is_refused_without_switching(self):
        blob = archive({"../outside": "no\n", "health": "#!/bin/sh\nexit 0\n"})
        _, text = checksum(blob)
        with self.assertRaises(ReleaseError):
            apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertFalse(any(path.name == "outside" for path in Path(self.tmp.name).rglob("*")))
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")

    def test_download_refuses_a_foreign_asset_url(self):
        class Opener:
            def open(self, req, timeout=60):
                url = req.full_url
                if url.endswith("/releases/tags/v1"):
                    body = json.dumps({"assets": [
                        {"name": ASSET, "browser_download_url": "http://169.254.169.254/a"},
                        {"name": ASSET_SUM, "browser_download_url": "http://169.254.169.254/a.sha256"},
                    ]}).encode()
                    return io.BytesIO(body)
                raise AssertionError(url)

        with self.assertRaises(ReleaseError) as caught:
            fetch_release("http://127.0.0.1:8765", "v1", "", Opener())
        self.assertNotIn("169.254.169.254", str(caught.exception))

    def test_token_is_not_returned_when_download_fails(self):
        secret = "ghp_" + "A" * 20
        token_file = Path(self.tmp.name) / "github.token"
        token_file.write_text(secret + "\n", encoding="utf-8")
        token_file.chmod(0o600)

        class Opener:
            def open(self, req, timeout=60):
                raise OSError("down")

        with self.assertRaises(ReleaseError) as caught:
            fetch_and_apply(self.paths, api="http://127.0.0.1:8765", token=secret, opener=Opener())
        self.assertNotIn(secret, str(caught.exception))
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in Path(self.tmp.name).rglob("*")
            if path.is_file() and path.name != "github.token"
        )
        self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
