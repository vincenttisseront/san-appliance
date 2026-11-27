"""Réplication asynchrone d'un LUN vers un pair déjà écrit. Sans pair, rien."""

import socket
import time

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json


CHUNK = 65536


def load_peer(paths):
    """None si aucun pair n'est déjà configuré. N'en crée pas."""
    try:
        data = _read_json(paths.conf / "peer.json")
    except PersistError:
        add_alert(paths, "error", "pair illisible, réplication laissée arrêtée")
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    address = (data.get("address") or "").strip()
    if address in {"", "0.0.0.0", "::", "::0", "*"}:
        return None
    try:
        port = int(data.get("port") or 9730)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {"address": address, "port": port, "lun": data.get("lun") or ""}


def replicate_once(paths, read_at, link_open, luns):
    if _held(paths):
        return {"action": "skip", "reason": "réplication suspendue"}
    peer = load_peer(paths)
    if peer is None:
        return {"action": "skip", "reason": "aucun pair"}
    chosen = [item for item in (luns or []) if not item.get("boot")]
    if peer.get("lun"):
        named = [
            item for item in chosen
            if item.get("id") == peer["lun"] or item.get("path") == peer["lun"]
        ]
        chosen = named or [{"id": peer["lun"], "path": peer["lun"]}]
    if not chosen:
        return {"action": "skip", "reason": "aucun LUN"}
    paths.ensure_runtime()
    state = _load_state(paths)
    reports = []
    for lun in chosen:
        reports.append(_push(paths, peer, lun, state, read_at, link_open))
    _save_state(paths, state)
    return {"action": "done", "peer": "{}:{}".format(peer["address"], peer["port"]), "luns": reports}


def run_replication(paths, probe, once=False, pause=None):
    pause = pause or (lambda: time.sleep(5))
    while True:
        report = replicate_from(paths, probe)
        if once or report.get("action") == "skip":
            return report
        luns = report.get("luns") or []
        if luns and all(item.get("action") == "skip" for item in luns):
            return report
        pause()


def replicate_from(paths, probe):
    return replicate_once(paths, device_read, open_socket, luns_from(paths, probe))


def luns_from(paths, probe):
    try:
        document, _secrets = _exports(paths)
    except PersistError:
        return []
    found = []
    for target in document.get("targets") or []:
        volume = target.get("volume") or {}
        current = None
        if probe is not None and volume.get("id"):
            current = probe.get_volume(volume.get("id"))
        item = current or volume
        if item.get("boot"):
            continue
        if item.get("id") or item.get("path"):
            found.append(item)
    return found


def device_read(lun, offset, length):
    path = lun.get("path")
    if not path:
        raise OSError("chemin absent")
    with open(path, "rb") as handle:
        handle.seek(offset)
        return handle.read(length)


def open_socket(peer, lun_id, timeout=2):
    sock = socket.create_connection((peer["address"], int(peer["port"])), timeout)
    sock.settimeout(timeout)
    return SocketLink(sock, lun_id)


class SocketLink:
    def __init__(self, sock, lun_id):
        self.sock = sock
        self.lun_id = lun_id

    def send(self, offset, block):
        header = "SANREP {} {} {}\n".format(offset, len(block), self.lun_id).encode()
        self.sock.sendall(header)
        self.sock.sendall(block)
        line = _readline(self.sock)
        parts = line.split()
        if len(parts) != 2 or parts[0] != "ACK":
            raise ConnectionError("acquittement refusé")
        if int(parts[1]) != offset + len(block):
            raise ConnectionError("acquittement refusé")
        return len(block)

    def close(self):
        self.sock.close()


def serve_once(conn, store):
    """Reçoit un bloc à l'offset annoncé. Le pair distant exécute ceci."""
    line = _readline(conn)
    parts = line.split(" ", 3)
    if len(parts) != 4 or parts[0] != "SANREP":
        conn.sendall(b"ERR refus\n")
        return
    offset = int(parts[1])
    length = int(parts[2])
    if offset < 0 or length < 0 or length > CHUNK:
        conn.sendall(b"ERR refus\n")
        return
    payload = _read_exact(conn, length)
    current = store.get("data", b"")
    if offset > len(current):
        conn.sendall(b"ERR trou\n")
        return
    store["data"] = current[:offset] + payload
    conn.sendall("ACK {}\n".format(offset + length).encode())


def stop(paths):
    paths.ensure_runtime()
    (paths.run / "replicate.hold").write_text("suspendu\n", encoding="utf-8")
    if paths.conf.is_dir():
        (paths.conf / "replicate.hold").write_text("suspendu\n", encoding="utf-8")
    return {"action": "skip", "reason": "réplication suspendue"}


def resume(paths):
    for hold in (paths.conf / "replicate.hold", paths.run / "replicate.hold"):
        if hold.is_file():
            hold.unlink()
    return {"action": "ready", "reason": "réplication reprise"}


def _held(paths):
    return (paths.conf / "replicate.hold").is_file() or (paths.run / "replicate.hold").is_file()


def _push(paths, peer, lun, state, read_at, link_open):
    key = lun.get("id") or lun.get("path") or "?"
    offset = int((state.get(key) or {}).get("offset") or 0)
    try:
        link = link_open(peer, key)
    except Exception:
        _mark_resume(paths, state, key, offset)
        return {"id": key, "action": "resume", "offset": offset, "reason": "lien coupé"}
    try:
        while True:
            try:
                block = read_at(lun, offset, CHUNK)
            except Exception:
                return {"id": key, "action": "skip", "offset": offset, "reason": "LUN illisible"}
            if not block:
                state[key] = {"offset": offset, "status": "synced"}
                _save_state(paths, state)
                return {"id": key, "action": "synced", "offset": offset}
            try:
                acked = link.send(offset, block)
            except Exception:
                _mark_resume(paths, state, key, offset)
                return {"id": key, "action": "resume", "offset": offset, "reason": "lien coupé"}
            if acked != len(block):
                _mark_resume(paths, state, key, offset)
                return {"id": key, "action": "resume", "offset": offset, "reason": "acquittement incomplet"}
            offset += acked
            state[key] = {"offset": offset, "status": "copying"}
            _save_state(paths, state)
    finally:
        closer = getattr(link, "close", None)
        if closer:
            try:
                closer()
            except Exception:
                pass


def _mark_resume(paths, state, key, offset):
    previous = (state.get(key) or {}).get("status")
    state[key] = {"offset": offset, "status": "resume"}
    _save_state(paths, state)
    if previous != "resume":
        add_alert(paths, "info", "réplication en attente de lien : {}".format(key))


def _load_state(paths):
    try:
        data = _read_json(paths.run / "replicate-state.json")
    except PersistError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(paths, state):
    paths.ensure_runtime()
    _write_json(paths.run / "replicate-state.json", state, 0o644)


def _exports(paths):
    from san_tui.persist import load_exports
    return load_exports(paths)


def _readline(sock):
    data = b""
    while b"\n" not in data:
        piece = sock.recv(1)
        if not piece:
            raise ConnectionError("lien coupé")
        data += piece
        if len(data) > 8192:
            raise ConnectionError("en-tête refusé")
    return data.decode("utf-8", "replace").strip()


def _read_exact(sock, length):
    data = b""
    while len(data) < length:
        piece = sock.recv(length - len(data))
        if not piece:
            raise ConnectionError("lien coupé")
        data += piece
    return data
