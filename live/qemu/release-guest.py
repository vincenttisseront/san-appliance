#!/usr/bin/env python3
"""Inside the live VM: bad checksum, rollback, then a healthy release."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ASSET = "san-appliance-app.tar.gz"
ASSET_SUM = "san-appliance-app.tar.gz.sha256"
IQN = "iqn.2026-11.local.san:vm"
API = "http://127.0.0.1:8765"


def run(cmd):
    completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def require(condition, message):
    if not condition:
        raise SystemExit(message)


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


def sha256(blob):
    return hashlib.sha256(blob).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def listeners():
    code, out, err = run(["ss", "-H", "-ltn"])
    require(code == 0, err or out)
    rows = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            rows.append(fields[3])
    return rows


GOOD = archive({
    "health": "#!/bin/sh\nexit 0\n",
    "version": "2\n",
})
SICK = archive({
    "health": "#!/bin/sh\nexit 1\n",
    "version": "9\n",
})
BAD = archive({
    "health": "#!/bin/sh\nexit 0\n",
    "version": "bad\n",
})
BLOBS = {
    "good": (GOOD, "{}  {}\n".format(sha256(GOOD), ASSET)),
    "sick": (SICK, "{}  {}\n".format(sha256(SICK), ASSET)),
    "bad": (BAD, "{}  {}\n".format("0" * 64, ASSET)),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        tag = path.rstrip("/").split("/")[-1]
        if path.endswith("/releases/tags/" + tag) or path.endswith("/releases/latest"):
            name = tag if tag in BLOBS else "good"
            body = json.dumps({"assets": [
                {"name": ASSET, "browser_download_url": API + "/files/" + name + ".tar.gz"},
                {"name": ASSET_SUM, "browser_download_url": API + "/files/" + name + ".sha256"},
            ]}).encode()
        elif path.startswith("/files/") and path.endswith(".tar.gz"):
            body = BLOBS[path.split("/")[-1].replace(".tar.gz", "")][0]
        elif path.startswith("/files/") and path.endswith(".sha256"):
            body = BLOBS[path.split("/")[-1].replace(".sha256", "")][1].encode()
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def post_update(tag):
    payload = json.dumps({"tag": tag, "api": API}).encode()
    _status, body = fetch(
        "http://127.0.0.1:3000/appliance-update",
        data=payload,
        headers={"content-type": "application/json"},
    )
    return json.loads(body.decode())


def choose_disk():
    code, out, err = run(["lsblk", "-J", "-o", "NAME,TYPE,MOUNTPOINT,SIZE"])
    require(code == 0, err)
    tree = json.loads(out)
    for item in tree.get("blockdevices") or []:
        if item.get("type") != "disk":
            continue
        name = item["name"]
        if name.startswith(("sr", "loop", "fd", "zram")):
            continue
        if item.get("mountpoint"):
            continue
        size = Path("/sys/block") / name / "size"
        try:
            sectors = int(size.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
        if sectors < 2048:
            continue
        return Path("/dev") / name
    raise SystemExit("disque jetable absent: {}".format(out))


def iscsi_up():
    code, out, err = run(["targetcli", "/iscsi", "ls"])
    return code == 0 and IQN in out, out[-400:] + err[-200:]


def fetch(url, data=None, headers=None):
    last = None
    for _ in range(20):
        try:
            request = Request(url, data=data, headers=headers or {})
            with urlopen(request, timeout=30) as response:
                return response.status, response.read()
        except Exception as exc:
            last = exc
            time.sleep(2)
    raise SystemExit("HTTP {} : {}".format(url, last))


def main():
    status, page = fetch("http://127.0.0.1:3000/appliance-update")
    require(status == 200 and "Mise à jour" in page.decode(), "page WebUI absente")
    status, _body = fetch("http://127.0.0.1:3000/")
    require(status in {200, 302}, "WebUI HTTP {}".format(status))

    disk = choose_disk()
    code, _out, err = run(["mkfs.ext4", "-F", "-L", "SANCONF", "-q", str(disk)])
    require(code == 0, "mkfs: " + err[-300:])
    Path("/var/lib/san/conf").mkdir(parents=True, exist_ok=True)
    code, _out, err = run(["mount", str(disk), "/var/lib/san/conf"])
    require(code == 0, "mount: " + err[-300:])
    app = Path("/var/lib/san/conf/app")
    (app / "a").mkdir(parents=True)
    (app / "a" / "version").write_text("1\n", encoding="utf-8")
    (app / "state.json").write_text(json.dumps({
        "version": 1,
        "active": "a",
        "rolled_back": False,
        "slots": {"a": {}, "b": {}},
    }), encoding="utf-8")
    Path("/var/lib/san/data").mkdir(parents=True, exist_ok=True)
    marker = Path("/var/lib/san/data/marker")
    marker.write_bytes(b"SAN-DATA-KEEP\n" * 40)
    export = Path("/var/lib/san/data/export.img")
    export.write_bytes(b"SAN-LUN\n" + b"\0" * ((8 * 1024 * 1024) - 8))
    before_marker = file_sha(marker)
    before_export = file_sha(export)

    code, out, err = run([
        "targetcli", "/backstores/fileio", "create", "sandata", str(export), "8M",
    ])
    require(code == 0, "fileio: " + (err or out)[-400:])
    code, out, err = run(["targetcli", "/iscsi", "create", IQN])
    require(code == 0, "iscsi create: " + (err or out)[-400:])
    code, out, err = run([
        "targetcli", "/iscsi/{}/tpg1/luns".format(IQN), "create", "/backstores/fileio/sandata",
    ])
    require(code == 0, "lun: " + (err or out)[-400:])
    run(["targetcli", "/iscsi/{}/tpg1/portals".format(IQN), "delete", "0.0.0.0", "3260"])
    code, out, err = run([
        "targetcli", "/iscsi/{}/tpg1/portals".format(IQN), "create", "127.0.0.1", "3260",
    ])
    require(code == 0, "portal: " + (err or out)[-400:])
    code, out, err = run([
        "targetcli", "/iscsi/{}/tpg1".format(IQN), "set", "attribute",
        "authentication=0", "generate_node_acls=1", "demo_mode_write_protect=0",
    ])
    require(code == 0, "acl: " + (err or out)[-400:])
    bound = listeners()
    require("0.0.0.0:3260" not in bound and "[::]:3260" not in bound, "portail ouvert: {}".format(bound))
    require("127.0.0.1:3260" in bound, "portail loopback absent: {}".format(bound))
    serving, detail = iscsi_up()
    require(serving, "cible absente " + detail)
    after_create = file_sha(export)

    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    bad = post_update("bad")
    require(bad.get("applied") is False, "mauvaise empreinte appliquée: {}".format(bad))
    require("empreinte" in bad.get("error", ""), "erreur inattendue: {}".format(bad))
    state = json.loads((app / "state.json").read_text(encoding="utf-8"))
    require(state["active"] == "a", "slot changé après mauvaise empreinte")
    require(not (app / "b").exists(), "slot b écrit malgré l'empreinte")

    sick = post_update("sick")
    require(sick.get("rollback") is True, "pas de retour: {}".format(sick))
    require(sick.get("slot") == "a", "slot après retour: {}".format(sick))
    state = json.loads((app / "state.json").read_text(encoding="utf-8"))
    require(state["active"] == "a" and state["rolled_back"] is True, "état après retour: {}".format(state))

    good = post_update("good")
    require(good.get("applied") is True and good.get("slot") == "b", "release saine: {}".format(good))
    require((app / "b" / "version").read_text(encoding="utf-8") == "2\n", "version du slot")
    require((app / "a" / "version").read_text(encoding="utf-8") == "1\n", "ancien slot modifié")

    require(file_sha(marker) == before_marker, "marqueur data modifié")
    require(file_sha(export) == after_create, "LUN modifié")
    serving, detail = iscsi_up()
    require(serving, "cible perdue " + detail)
    bound = listeners()
    require("127.0.0.1:3000" in bound, "WebUI absente")
    require("0.0.0.0:3000" not in bound and "[::]:3000" not in bound, "WebUI hors loopback")
    require("127.0.0.1:9753" in bound, "updater absent")
    token = Path("/etc/san/github.token")
    require(not token.exists(), "jeton présent dans l'image")
    print("SAN-RESULT " + json.dumps({
        "bad_checksum": True,
        "rollback": True,
        "applied_slot": "b",
        "data_sha256": before_marker,
        "lun_sha256": after_create,
        "iscsi": IQN,
        "listeners": [item for item in bound if item.endswith(":3000") or item.endswith(":3260") or item.endswith(":9753")],
    }))


if __name__ == "__main__":
    main()
