"""Métriques Prometheus sur l'adresse d'administration, jamais sur le stockage."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from san_tui.alerts import load_alerts
from san_tui.persist import PersistError, _read_json
from san_tui.retire import retired_ids
from san_tui.safety import WILDCARD_ADDRS


class MetricsError(Exception):
    pass


def metrics_host(plan):
    admin = _host((plan or {}).get("admin"))
    storage = _host((plan or {}).get("storage"))
    admin_if = ((plan or {}).get("admin") or {}).get("iface")
    storage_if = ((plan or {}).get("storage") or {}).get("iface")
    if not admin or admin in WILDCARD_ADDRS:
        return None
    if not storage or admin == storage or admin_if == storage_if:
        return None
    return admin


def render(paths):
    lines = [
        "# HELP san_up 1 lorsque ce service répond",
        "# TYPE san_up gauge",
        "san_up 1",
        "# TYPE san_disk_retired gauge",
        "san_disk_retired {}".format(len(retired_ids(paths))),
        "# TYPE san_alerts gauge",
        "san_alerts {}".format(len(load_alerts(paths))),
    ]
    try:
        state = _read_json(paths.run / "replicate-state.json") or {}
    except PersistError:
        state = {}
    if isinstance(state, dict):
        for key, item in state.items():
            if not isinstance(item, dict):
                continue
            offset = int(item.get("offset") or 0)
            lines.append('san_replicate_offset_bytes{{lun="{}"}} {}'.format(_label(key), offset))
    try:
        samples = _read_json(paths.run / "lun-p99.json") or {}
    except PersistError:
        samples = {}
    for sample in (samples.get("samples") or []) if isinstance(samples, dict) else []:
        iqn = sample.get("iqn") or ""
        if not iqn.startswith("iqn."):
            continue
        lines.append('san_lun_p99_us{{iqn="{}"}} {}'.format(_label(iqn), int(sample.get("p99_us") or 0)))
    return "\n".join(lines) + "\n"


def open_metrics(paths, probe, plan, port=9100):
    del probe
    if _held(paths):
        return None, {"action": "skip", "reason": "métriques suspendues"}
    host = metrics_host(plan)
    if host is None:
        return None, {"action": "skip", "reason": "adresse admin absente"}
    try:
        server = _bind(paths, plan, host, port)
    except OSError:
        return None, {"action": "retry", "reason": "admin injoignable"}
    return server, {"action": "listen", "host": host, "port": server.server_address[1]}


def serve(paths, probe, pause=None):
    import time
    pause = pause or (lambda: time.sleep(5))
    plan = _plan(paths)
    while True:
        server, report = open_metrics(paths, probe, plan)
        if report["action"] == "skip":
            return report
        if report["action"] == "retry":
            plan = _plan(paths)
            pause()
            continue
        thread = threading.Thread(target=_watch, args=(paths, server), daemon=True)
        thread.start()
        try:
            server.serve_forever()
        finally:
            server.server_close()
        return {"action": "stop"}


def stop(paths):
    paths.ensure_runtime()
    (paths.run / "metrics.hold").write_text("suspendu\n", encoding="utf-8")
    if paths.conf.is_dir():
        (paths.conf / "metrics.hold").write_text("suspendu\n", encoding="utf-8")
    return {"action": "skip", "reason": "métriques suspendues"}


def resume_metrics(paths):
    for hold in (paths.conf / "metrics.hold", paths.run / "metrics.hold"):
        if hold.is_file():
            hold.unlink()
    return {"action": "ready", "reason": "métriques reprises"}


def _held(paths):
    return (paths.conf / "metrics.hold").is_file() or (paths.run / "metrics.hold").is_file()


def network_plan(paths):
    return _plan(paths)


def _bind(paths, plan, host, port):
    chosen = metrics_host(plan)
    storage = _host((plan or {}).get("storage"))
    if chosen is None or host != chosen or host == storage or host in WILDCARD_ADDRS:
        raise MetricsError("écoute hors de l'admin refusée")
    handler = _handler(paths)
    server = _HTTPServer((host, port), handler)
    bound = server.server_address[0]
    if bound != host:
        server.server_close()
        raise MetricsError("écoute hors de l'admin refusée")
    return server


class _HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def _handler(paths):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if _held(paths):
                self.send_response(404)
                self.end_headers()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path.split("?", 1)[0] != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            body = render(paths).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    return Handler


def _watch(paths, server):
    while True:
        if _held(paths):
            server.shutdown()
            return
        threading.Event().wait(1)


def _plan(paths):
    try:
        plan = _read_json(paths.etc / "network.json")
    except PersistError:
        plan = None
    if isinstance(plan, dict) and plan.get("admin") and plan.get("storage"):
        return plan
    try:
        document = _read_json(paths.conf / "exports.json") or {}
    except PersistError:
        document = {}
    if isinstance(document, dict) and isinstance(document.get("network"), dict):
        return document["network"]
    return plan if isinstance(plan, dict) else {}


def _host(item):
    if not isinstance(item, dict):
        return ""
    return (item.get("address") or "").split("/", 1)[0]


def _label(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")
