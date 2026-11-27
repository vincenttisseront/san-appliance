"""Empreinte, slot inactif, et un seul retour au slot précédent."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import urllib.request
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from san_tui.paths import SanPaths
from san_tui.release import (
    ASSET,
    ASSET_SUM,
    MAX_BYTES,
    ReleaseError,
    apply_verified,
    fetch_and_apply,
    fetch_release,
    newest_slot,
    public_asset_status,
    undo_slot,
    verify_archive,
    parse_checksum,
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
            fetch_and_apply(self.paths, tag="v9", api="http://127.0.0.1:8765", token=secret, opener=Opener())
        self.assertNotIn(secret, str(caught.exception))
        text = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in Path(self.tmp.name).rglob("*")
            if path.is_file() and path.name != "github.token"
        )
        self.assertNotIn(secret, text)

    def test_diff_is_written_before_the_slot_pointer_moves(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        seen = {}

        def health(_slot):
            state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
            seen["active"] = state["active"]
            seen["diff"] = (self.paths.conf / "app" / "diff.txt").read_text(encoding="utf-8")
            return 0

        result = apply_verified(self.paths, blob, text, health=health)
        self.assertEqual(seen["active"], "a")
        self.assertIn("version", seen["diff"])
        self.assertEqual(result["slot"], "b")
        self.assertIn("version", result["diff"])
        self.assertFalse(result["rollback"])

    def test_a_writing_session_does_not_switch_the_slot(self):
        marker = self.paths.root / "var" / "lib" / "san" / "webui" / "writing"
        marker.parent.mkdir(parents=True)
        marker.write_text("1\n", encoding="utf-8")
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        result = apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertTrue(result["deferred"])
        self.assertEqual(result["slot"], "a")
        self.assertIn("version", result["diff"])
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")
        self.assertFalse((self.paths.conf / "app" / "b").exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")

    def test_a_stale_writing_marker_does_not_block_apply(self):
        marker = self.paths.root / "var" / "lib" / "san" / "webui" / "writing"
        marker.parent.mkdir(parents=True)
        marker.write_text("1\n", encoding="utf-8")
        old = time.time() - 1000
        os.utime(marker, (old, old))
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        result = apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertTrue(result["applied"])
        self.assertEqual(result["slot"], "b")

    def test_newer_slot_carries_the_later_timestamp(self):
        state_path = self.paths.conf / "app" / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["slots"]["a"]["installed"] = "2020-01-01T00:00:00Z"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        result = apply_verified(
            self.paths, blob, text, health=lambda _slot: 0, clock=lambda: "2020-06-01T00:00:00Z",
        )
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["slots"]["b"]["installed"], "2020-06-01T00:00:00Z")
        self.assertEqual(newest_slot(saved), "b")
        self.assertEqual(result["newest"], "b")
        self.assertGreater(saved["slots"]["b"]["installed"], saved["slots"]["a"]["installed"])

    def test_older_release_keeps_the_active_slot(self):
        (self.paths.conf / "app" / "a" / "version").write_text("3\n", encoding="utf-8")
        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        with self.assertRaises(ReleaseError) as caught:
            apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertIn("ancienne", str(caught.exception))
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)
        self.assertFalse((self.paths.conf / "app" / "b").exists())
        self.assertEqual((self.paths.conf / "app" / "a" / "version").read_text(encoding="utf-8"), "3\n")

    def test_the_same_version_is_not_applied_again(self):
        (self.paths.conf / "app" / "a" / "version").write_text("2\n", encoding="utf-8")
        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        result = apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        self.assertFalse(result["applied"])
        self.assertEqual(result["reason"], "déjà en place")
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)

    def test_oversized_archive_keeps_the_active_slot(self):
        import san_tui.release as release
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "4\n"})
        _, text = checksum(blob)
        self.assertGreater(len(blob), 8)
        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        previous = release.MAX_BYTES
        release.MAX_BYTES = 8
        try:
            with self.assertRaises(ReleaseError) as caught:
                apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        finally:
            release.MAX_BYTES = previous
        self.assertIn("grande", str(caught.exception))
        self.assertEqual(release.MAX_BYTES, MAX_BYTES)
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)
        self.assertFalse((self.paths.conf / "app" / "b").exists())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")

    def test_announced_length_is_refused_before_the_body_is_read(self):
        class Huge:
            headers = {"Content-Length": str(MAX_BYTES + 1)}

            def read(self, _n=-1):
                raise AssertionError("corps lu")

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, req, timeout=60):
                url = req.full_url
                if url.endswith("/releases/tags/v1"):
                    body = json.dumps({"assets": [
                        {"name": ASSET, "browser_download_url": "http://127.0.0.1:8765/files/a.tar.gz"},
                        {"name": ASSET_SUM, "browser_download_url": "http://127.0.0.1:8765/files/a.sha256"},
                    ]}).encode()
                    return io.BytesIO(body)
                return Huge()

        before = (self.paths.conf / "app" / "state.json").read_text(encoding="utf-8")
        with self.assertRaises(ReleaseError) as caught:
            fetch_release("http://127.0.0.1:8765", "v1", "", Opener())
        self.assertIn("grande", str(caught.exception))
        self.assertEqual((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"), before)

    def test_undo_returns_to_the_previous_slot(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _, text = checksum(blob)
        apply_verified(self.paths, blob, text, health=lambda _slot: 0)
        undone = undo_slot(self.paths)
        self.assertTrue(undone["undo"])
        self.assertEqual(undone["slot"], "a")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "a")

    def test_public_release_asset_or_local_standin_checks_the_checksum(self):
        status = public_asset_status()
        self.assertIn("present", status)
        if status["present"]:
            archive_blob, checksum_text = fetch_release("", "", "")
            source = "github"
        else:
            self.assertTrue(status["reason"])
            archive_blob, checksum_text = _standin_release()
            source = "local-stand-in"
        digest = verify_archive(archive_blob, parse_checksum(checksum_text, ASSET))
        self.assertEqual(digest, hashlib.sha256(archive_blob).hexdigest())
        result = apply_verified(self.paths, archive_blob, checksum_text, health=lambda _slot: 0)
        self.assertEqual(result["sha256"], digest)
        if source == "local-stand-in":
            self.assertTrue(result["applied"])
            self.assertFalse(status["present"])
        print("public-asset {}".format(json.dumps({"source": source, "status": status})))

    def test_boot_script_applies_the_standin_and_keeps_iscsi_data(self):
        blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
        _digest, text = checksum(blob)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _fmt, *_args):
                return

            def do_GET(self):
                port = self.server.server_address[1]
                path = self.path.split("?", 1)[0]
                if path.endswith("/releases/latest"):
                    body = json.dumps({"assets": [
                        {"name": ASSET, "browser_download_url": "http://127.0.0.1:{}/a.tar.gz".format(port)},
                        {"name": ASSET_SUM, "browser_download_url": "http://127.0.0.1:{}/a.sha256".format(port)},
                    ]}).encode()
                elif path.endswith(".tar.gz"):
                    body = blob
                elif path.endswith(".sha256"):
                    body = text.encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = None
        for port in range(8780, 8799):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        self.assertIsNotNone(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        script = Path(__file__).resolve().parents[3] / "live" / "rootfs" / "usr" / "local" / "sbin" / "san-release-apply"
        env = os.environ.copy()
        env["SAN_ROOT"] = self.tmp.name
        env["SAN_RELEASE_API"] = "http://127.0.0.1:{}".format(server.server_address[1])
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        try:
            completed = subprocess.run(
                ["python3", str(script)], env=env, check=False, capture_output=True, text=True,
            )
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertTrue(result["applied"])
        self.assertEqual(result["sha256"], hashlib.sha256(blob).hexdigest())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "keep-data\n")
        state = json.loads((self.paths.conf / "app" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["active"], "b")
        self.assertTrue(state["slots"]["b"].get("installed"))


def _standin_release():
    blob = archive({"health": "#!/bin/sh\nexit 0\n", "version": "2\n"})
    _digest, text = checksum(blob)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _fmt, *_args):
            return

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path.endswith("/releases/tags/good") or path.endswith("/releases/latest"):
                body = json.dumps({"assets": [
                    {"name": ASSET, "browser_download_url": "http://127.0.0.1:{}/files/app.tar.gz".format(self.server.server_address[1])},
                    {"name": ASSET_SUM, "browser_download_url": "http://127.0.0.1:{}/files/app.sha256".format(self.server.server_address[1])},
                ]}).encode()
            elif path.endswith(".tar.gz"):
                body = blob
            elif path.endswith(".sha256"):
                body = text.encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = None
    for port in range(8765, 8790):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise AssertionError("port local indisponible")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        return fetch_release(
            "http://127.0.0.1:{}".format(server.server_address[1]),
            "good",
            "",
            urllib.request.build_opener(),
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    unittest.main()
