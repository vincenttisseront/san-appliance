"""Export d'audit signé, sans secret. La clé n'est pas dans l'image."""

import hashlib
import hmac
import json
import os
from pathlib import Path

from san_tui.alerts import add_alert, load_alerts
from san_tui.persist import PersistError, _read_json
from san_tui.probe import inventory
from san_tui.redact import public_mapping, redact


def prepare(paths, probe):
    key = ensure_key(paths)
    if not key:
        return {"action": "skip", "reason": "clé absente"}
    body = _body(paths, probe, key)
    mac = sign(body, key)
    body["signature"] = {
        "alg": "hmac-sha256",
        "key_id": hashlib.sha256(key).hexdigest()[:16],
        "mac": mac,
    }
    paths.ensure_runtime()
    target = paths.run / "audit-export.json"
    text = json.dumps(body, indent=2, ensure_ascii=False) + "\n"
    if key.decode("latin1", "replace") in text or _key_leaked(text, key):
        add_alert(paths, "error", "audit non publié : secret dans le document")
        return {"action": "skip", "reason": "secret dans le document"}
    target.write_text(text, encoding="utf-8")
    os.chmod(target, 0o644)
    return {"action": "ready", "path": str(target), "key_id": body["signature"]["key_id"]}


def ensure_key(paths):
    if not _conf_ready(paths):
        return None
    path = paths.conf / "audit.key"
    if path.is_file():
        try:
            mode = path.stat().st_mode & 0o077
        except OSError:
            return None
        if mode != 0:
            add_alert(paths, "error", "clé d'audit trop ouverte, export non signé")
            return None
        try:
            key = path.read_bytes()
        except OSError:
            return None
        if len(key) < 32:
            return None
        return key
    key = os.urandom(32)
    try:
        path.write_bytes(key)
        os.chmod(path, 0o600)
    except OSError:
        return None
    return key


def _conf_ready(paths):
    """La clé reste sur le support de conf, pas sur la racine live."""
    if not paths.conf.is_dir():
        return False
    if paths.root != Path("/"):
        return True
    from san_tui.runtime import medium_ok
    return medium_ok(paths)


def sign(body, key):
    payload = canonical(body)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def canonical(body):
    unsigned = {key: value for key, value in body.items() if key != "signature"}
    return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _body(paths, probe, key):
    secrets = _secret_values(paths)
    alerts = []
    for item in load_alerts(paths):
        alerts.append({
            "time": item.get("time"),
            "level": item.get("level"),
            "message": redact(item.get("message") or "", secrets),
        })
    try:
        document = _read_json(paths.conf / "exports.json") or {}
    except PersistError:
        document = {}
    public = public_mapping(document if isinstance(document, dict) else {})
    encoded = redact(json.dumps(public, ensure_ascii=False), secrets)
    try:
        public = json.loads(encoded)
    except json.JSONDecodeError:
        public = {}
    snap = inventory(probe)
    body = {
        "version": 1,
        "alerts": alerts,
        "exports": public.get("targets") or [],
        "disks": snap.get("disks") or [],
    }
    cleaned = redact(json.dumps(body, ensure_ascii=False), secrets + [key.hex()])
    return json.loads(cleaned)


def _secret_values(paths):
    try:
        raw = _read_json(paths.conf / "secrets.json") or {}
    except PersistError:
        return []
    if not isinstance(raw, dict):
        return []
    return [str(value) for value in raw.values() if value]


def _key_leaked(text, key):
    return key.hex() in text or hashlib.sha256(key).hexdigest() == text
