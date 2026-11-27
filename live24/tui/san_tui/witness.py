"""Témoin externe pour le slot. Sans réponse, le slot local reste le slot actif."""

import socket

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json


def load_witness(paths):
    try:
        data = _read_json(paths.conf / "witness.json")
    except PersistError:
        add_alert(paths, "error", "témoin illisible, slot local conservé")
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    address = (data.get("address") or "").strip()
    if address in {"", "0.0.0.0", "::", "::0", "*"}:
        return None
    try:
        port = int(data.get("port") or 9644)
    except (TypeError, ValueError):
        return None
    if not 1 <= port <= 65535:
        return None
    return {"address": address, "port": port}


def local_slot(paths):
    try:
        state = _read_json(paths.conf / "ab.json")
    except PersistError:
        state = None
    if isinstance(state, dict) and state.get("active") in {"a", "b"}:
        return state["active"]
    return "a"


def decide(local, answer):
    """La seule voix est la réponse du témoin. Une absence de réponse ne déplace rien."""
    slot = local if local in {"a", "b"} else "a"
    if answer == "other":
        return {
            "action": "failover",
            "slot": "b" if slot == "a" else "a",
            "local": slot,
            "reason": "témoin",
        }
    reason = "témoin local" if answer == "local" else "témoin muet"
    return {"action": "stay", "slot": slot, "local": slot, "reason": reason}


def ask_witness(witness, timeout=1.0):
    sock = socket.create_connection((witness["address"], int(witness["port"])), timeout)
    try:
        sock.settimeout(timeout)
        sock.sendall(b"SAN-WITNESS\n")
        line = b""
        while b"\n" not in line:
            piece = sock.recv(1)
            if not piece:
                raise ConnectionError("témoin muet")
            line += piece
            if len(line) > 64:
                raise ConnectionError("témoin muet")
    finally:
        sock.close()
    word = line.decode("utf-8", "replace").strip()
    if word not in {"local", "other"}:
        return None
    return word


def evaluate(paths, ask=None, timeout=1.0):
    current = local_slot(paths)
    if _held(paths):
        decision = {"action": "stay", "slot": current, "local": current, "reason": "bascule suspendue"}
        _record(paths, decision, move=False)
        return decision
    witness = load_witness(paths)
    if witness is None:
        decision = {"action": "stay", "slot": current, "local": current, "reason": "aucun témoin"}
        _record(paths, decision, move=False)
        return decision
    ask = ask or (lambda item: ask_witness(item, timeout))
    try:
        answer = ask(witness)
    except Exception:
        answer = None
    decision = decide(current, answer)
    if decision["reason"] == "témoin muet":
        _once(paths, "info", "témoin muet, slot local conservé")
    return _commit(paths, decision)


def stay(paths):
    paths.ensure_runtime()
    (paths.run / "witness.hold").write_text("local\n", encoding="utf-8")
    if paths.conf.is_dir():
        (paths.conf / "witness.hold").write_text("local\n", encoding="utf-8")
    return evaluate(paths)


def release(paths):
    for hold in (paths.conf / "witness.hold", paths.run / "witness.hold"):
        if hold.is_file():
            hold.unlink()
    return {"action": "ready", "reason": "témoin repris"}


def _held(paths):
    return (paths.conf / "witness.hold").is_file() or (paths.run / "witness.hold").is_file()


def _commit(paths, decision):
    if decision["action"] != "failover":
        _record(paths, decision, move=False)
        return decision
    try:
        state = _read_json(paths.conf / "ab.json")
    except PersistError:
        state = None
    slots = state.get("slots") if isinstance(state, dict) else None
    if not isinstance(slots, dict) or decision["slot"] not in slots:
        stayed = {
            "action": "stay",
            "slot": decision["local"],
            "local": decision["local"],
            "reason": "slot pair absent",
        }
        _record(paths, stayed, move=False)
        return stayed
    state["active"] = decision["slot"]
    _write_json(paths.conf / "ab.json", state, 0o644)
    _record(paths, decision, move=True)
    return decision


def _record(paths, decision, move):
    paths.ensure_runtime()
    _write_json(paths.run / "slot.json", {
        "active": decision["slot"],
        "action": decision["action"],
        "reason": decision["reason"],
    }, 0o644)
    # Le silence ramène le pointeur de boot sur le slot local. Il ne crée pas de quorum.
    slot = decision["slot"] if move else decision["local"]
    if not paths.conf.is_dir():
        return
    try:
        (paths.conf / "boot-slot").write_text(slot + "\n", encoding="utf-8")
    except OSError:
        add_alert(paths, "error", "slot non enregistré, export iSCSI inchangé")


def _once(paths, level, message):
    marker = paths.run / "witness-noted"
    paths.ensure_runtime()
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == message:
        return
    marker.write_text(message + "\n", encoding="utf-8")
    add_alert(paths, level, message)
