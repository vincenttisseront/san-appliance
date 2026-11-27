"""Inventaire HTTP sur 127.0.0.1. Aucun réglage, aucune autre adresse."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from san_tui.persist import PersistError, _read_json
from san_tui.probe import inventory


class InventoryError(Exception):
    pass


def payload(paths, probe):
    snap = inventory(probe)
    targets = []
    try:
        document = _read_json(paths.conf / "exports.json") or {}
    except PersistError:
        document = {}
    if isinstance(document, dict):
        for target in document.get("targets") or []:
            targets.append({"iqn": target.get("iqn"), "lun": target.get("lun")})
    snap["targets"] = targets
    return snap


def bind_server(paths, probe, host="127.0.0.1", port=9714):
    if host != "127.0.0.1":
        raise InventoryError("inventaire seulement sur 127.0.0.1")
    handler = _handler(paths, probe)
    server = _HTTPServer((host, port), handler)
    bound = server.server_address[0]
    if bound != "127.0.0.1":
        server.server_close()
        raise InventoryError("inventaire seulement sur 127.0.0.1")
    return server


def serve(paths, probe):
    if _held(paths):
        return {"action": "skip", "reason": "inventaire suspendu"}
    server = bind_server(paths, probe)
    thread = threading.Thread(target=_watch_hold, args=(paths, server), daemon=True)
    thread.start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return {"action": "stop"}


def stop(paths):
    paths.ensure_runtime()
    (paths.run / "inventory.hold").write_text("suspendu\n", encoding="utf-8")
    if paths.conf.is_dir():
        (paths.conf / "inventory.hold").write_text("suspendu\n", encoding="utf-8")
    return {"action": "skip", "reason": "inventaire suspendu"}


def resume(paths):
    for hold in (paths.conf / "inventory.hold", paths.run / "inventory.hold"):
        if hold.is_file():
            hold.unlink()
    return {"action": "ready", "reason": "inventaire repris"}


def _held(paths):
    return (paths.conf / "inventory.hold").is_file() or (paths.run / "inventory.hold").is_file()


def _handler(paths, probe):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if _held(paths):
                self.send_response(404)
                self.end_headers()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            path = self.path.split("?", 1)[0]
            if path not in {"/inventory", "/api/inventory"}:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(payload(paths, probe), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    return Handler


class _HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _watch_hold(paths, server):
    while True:
        if _held(paths):
            server.shutdown()
            return
        threading.Event().wait(1)
