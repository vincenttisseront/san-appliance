"""Persistance séparée : live, conf, data. Le support est choisi explicitement."""

import json
import os
import shutil
from datetime import datetime, timezone

from san_tui.alerts import add_alert
from san_tui.redact import SECRET_KEYS
from san_tui.safety import SafetyError, assert_usable, require_confirmation


class PersistError(Exception):
    pass


def select_support(paths, role, volume, typed, mounter=None):
    """Enregistre un support conf ou data. Ne formate pas. N'exporte pas."""
    if role not in {"conf", "data"}:
        raise PersistError("rôle inconnu : {}".format(role))
    assert_usable(volume)
    require_confirmation(volume["id"], typed)
    paths.ensure_runtime()
    choice = _read_json(paths.choice) or {"version": 1, "conf": None, "data": None}
    choice["version"] = 2
    choice[role] = {
        "id": volume["id"],
        "wwn": volume.get("wwn") or "",
        "serial": volume.get("serial") or "",
        "size": volume.get("size"),
        "block_size": volume.get("block_size"),
    }
    _write_json(paths.choice, choice, 0o644)
    if mounter is not None and role == "conf":
        mounter(volume, paths.conf)
    return choice


def load_choice(paths):
    data = _read_json(paths.choice)
    if data is None:
        return None
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        raise PersistError("choix de support invalide")
    return data


def save_exports(paths, document, secrets):
    """Écrit la conf publique et les secrets à part, après une sauvegarde versionnée."""
    _reject_embedded_secrets(document)
    paths.conf.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.conf, 0o755)
    backup_dir = _backup(paths)
    public_path = paths.conf / "exports.json"
    secret_path = paths.conf / "secrets.json"
    _write_json(public_path, document, 0o644)
    _write_json(secret_path, secrets, 0o600)
    from san_tui.configio import write_builtin_export
    write_builtin_export(paths, document, secrets)
    return backup_dir


def load_exports(paths):
    """Retourne (document, secrets) ou lève PersistError si la conf est invalide."""
    public_path = paths.conf / "exports.json"
    if not public_path.is_file():
        raise PersistError("conf absente")
    document = _read_json(public_path)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise PersistError("conf invalide")
    if not isinstance(document.get("targets"), list):
        raise PersistError("conf invalide : targets")
    _reject_embedded_secrets(document)
    secrets = _read_json(paths.conf / "secrets.json") or {}
    if not isinstance(secrets, dict):
        raise PersistError("fichier de secrets invalide")
    return document, secrets


def _backup(paths):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = paths.conf / "backups" / stamp
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("exports.json", "secrets.json"):
        src = paths.conf / name
        if src.is_file():
            target = dest / name
            shutil.copy2(src, target)
            if name == "secrets.json":
                os.chmod(target, 0o600)
            copied.append(name)
    manifest = {"created": stamp, "files": copied}
    _write_json(dest / "manifest.json", manifest, 0o600)
    return dest


def _reject_embedded_secrets(document):
    found = _find_keys(document, SECRET_KEYS)
    if found:
        raise PersistError("conf invalide : secret présent dans le fichier public")


def _find_keys(value, keys):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys:
                return True
            if _find_keys(item, keys):
                return True
    elif isinstance(value, list):
        return any(_find_keys(item, keys) for item in value)
    return False


def _read_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PersistError("conf invalide : {}".format(exc.msg)) from exc


def _write_json(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def note_invalid(paths, message):
    return add_alert(paths, "error", message)


def rollback_files(paths):
    """Reprend la sauvegarde précédente. N'invente pas une cible."""
    root = paths.conf / "backups"
    if not root.is_dir():
        raise PersistError("aucune sauvegarde")
    dirs = sorted(path for path in root.iterdir() if path.is_dir())
    if not dirs:
        raise PersistError("aucune sauvegarde")
    src = dirs[-1]
    exported = src / "exports.json"
    if exported.is_file():
        shutil.copy2(exported, paths.conf / "exports.json")
    else:
        _write_json(paths.conf / "exports.json", {"version": 1, "targets": []}, 0o644)
    secret = src / "secrets.json"
    if secret.is_file():
        shutil.copy2(secret, paths.conf / "secrets.json")
        os.chmod(paths.conf / "secrets.json", 0o600)
    else:
        _write_json(paths.conf / "secrets.json", {}, 0o600)
    return str(src)
