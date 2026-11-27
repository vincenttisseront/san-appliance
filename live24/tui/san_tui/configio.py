"""Export versionné, import par WWN, et mode usine.

Le fichier téléchargé est déjà écrit au moment de la sauvegarde.
Le CHAP n'y figure pas en clair : il est scellé pour qu'un appliance neuf
puisse le rouvrir sans formulaire. L'import rattache les LUN par WWN.
Un WWN absent est ignoré, avec la cause. Le mode usine refuse de tourner
sans le fichier marqueur /etc/san/factory.
"""

import base64
import grp
import hashlib
import hmac
import json
import os
import secrets
import shutil
from pathlib import Path

from san_tui.persist import PersistError

EXPORT_VERSION = 2
EXPORT_NAME = "san-config.json"
_CONFIG_FILES = (
    "exports.json",
    "secrets.json",
    "san-config.json",
    "raid.json",
    "lvm.json",
    "nvme.json",
)
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def export_path(paths):
    return paths.conf / EXPORT_NAME


def incoming_path(paths):
    return paths.conf / "incoming" / EXPORT_NAME


def factory_marker(paths):
    return paths.etc / "factory"


def seal(secrets_map):
    key = os.urandom(32)
    nonce = os.urandom(16)
    plain = json.dumps(secrets_map, sort_keys=True, ensure_ascii=False).encode("utf-8")
    cipher = _xor(key, nonce, plain)
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return {
        "alg": "sha256-hmac-xor",
        "key": base64.b64encode(key).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(cipher).decode("ascii"),
        "mac": base64.b64encode(mac).decode("ascii"),
    }


def unseal(blob):
    if not isinstance(blob, dict) or blob.get("alg") != "sha256-hmac-xor":
        raise PersistError("secrets scellés refusés")
    try:
        key = base64.b64decode(blob["key"])
        nonce = base64.b64decode(blob["nonce"])
        cipher = base64.b64decode(blob["ciphertext"])
        mac = base64.b64decode(blob["mac"])
    except (KeyError, ValueError) as exc:
        raise PersistError("secrets scellés refusés") from exc
    expected = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected):
        raise PersistError("secrets scellés refusés")
    try:
        data = json.loads(_xor(key, nonce, cipher).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PersistError("secrets scellés refusés") from exc
    if not isinstance(data, dict):
        raise PersistError("secrets scellés refusés")
    return data


def build_export(document, secrets_map):
    targets = []
    for target in document.get("targets") or []:
        targets.append(_public_target(target))
    payload = {
        "version": EXPORT_VERSION,
        "product": "san-appliance",
        "network": document.get("network") if isinstance(document.get("network"), dict) else {},
        "targets": targets,
    }
    if secrets_map:
        payload["sealed"] = seal(secrets_map)
    return payload


def write_builtin_export(paths, document, secrets_map):
    payload = build_export(document, secrets_map)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for value in (secrets_map or {}).values():
        if value and str(value) in text:
            raise PersistError("export : CHAP en clair")
    path = export_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o640)
    _give_to_webui(tmp)
    os.replace(tmp, path)
    return path


def read_builtin_export(paths):
    """Lit le fichier déjà construit. Ne le régénère pas."""
    path = export_path(paths)
    if not path.is_file():
        raise PersistError("export pas encore construit")
    return path.read_bytes()


def refresh_export(paths):
    from san_tui.persist import load_exports
    document, secrets_map = load_exports(paths)
    return str(write_builtin_export(paths, document, secrets_map))


def import_config(paths, blob, probe):
    """Rattache chaque LUN au volume local qui a le même WWN. Pas d'écran de mapping."""
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8")
    try:
        document = json.loads(blob)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PersistError("export illisible") from exc
    if not isinstance(document, dict) or document.get("version") != EXPORT_VERSION:
        raise PersistError("version d'export refusée")
    if document.get("product") != "san-appliance":
        raise PersistError("export refusé")
    sealed = document.get("sealed")
    secrets_map = unseal(sealed) if sealed else {}
    network = document.get("network") if isinstance(document.get("network"), dict) else {}
    kept = []
    skipped = []
    targets_out = []
    for target in document.get("targets") or []:
        if not isinstance(target, dict):
            skipped.append({"iqn": "?", "lun": None, "wwn": "", "reason": "LUN illisible"})
            continue
        volume = target.get("volume") or {}
        wwn = volume.get("wwn") or ""
        local, reason = match_wwn(probe, wwn, volume.get("serial") or "")
        iqn = target.get("iqn") or "?"
        lun = target.get("lun")
        if reason:
            skipped.append({"iqn": iqn, "lun": lun, "wwn": wwn, "reason": reason})
            continue
        acls = target.get("acls") or []
        if len(acls) != 1 or not isinstance(acls[0], dict):
            skipped.append({"iqn": iqn, "lun": lun, "wwn": wwn, "reason": "ACL absente"})
            continue
        acl = acls[0]
        ref = acl.get("chap_secret_ref") or "{}:{}".format(iqn, acl.get("initiator") or "")
        if not secrets_map.get(ref):
            secrets_map[ref] = generate_chap()
        targets_out.append({
            "iqn": iqn,
            "portal": target.get("portal"),
            "lun": lun,
            "volume": {
                "id": local["id"],
                "wwn": local.get("wwn") or wwn,
                "serial": local.get("serial") or "",
                "size": local.get("size"),
                "block_size": local.get("block_size"),
                "kind": local.get("kind") or "block",
            },
            "acls": [{
                "initiator": acl.get("initiator"),
                "chap_user": acl.get("chap_user"),
                "chap_secret_ref": ref,
            }],
        })
        kept.append({"iqn": iqn, "lun": lun, "wwn": wwn, "id": local["id"]})
    from san_tui.alerts import add_alert
    from san_tui.persist import _write_json, save_exports
    public = {"version": 1, "network": network, "targets": targets_out}
    save_exports(paths, public, secrets_map)
    if network:
        _write_json(paths.etc / "network.json", network, 0o644)
    for item in skipped:
        add_alert(paths, "error", "{} : {}".format(item["iqn"], item["reason"]))
    return {"imported": kept, "skipped": skipped}


def import_incoming(paths, probe):
    source = incoming_path(paths)
    if not source.is_file():
        return {"imported": [], "skipped": [], "reason": "rien à importer"}
    report = import_config(paths, source.read_bytes(), probe)
    dest_dir = paths.conf / "imported"
    dest_dir.mkdir(parents=True, exist_ok=True)
    source.replace(dest_dir / EXPORT_NAME)
    report["source"] = str(source)
    return report


def match_wwn(probe, wwn, serial):
    if not wwn:
        return None, "WWN absent : LUN non repris"
    found = []
    for item in probe.list_volumes():
        if item.get("wwn") == wwn:
            found.append(item)
    if not found:
        return None, "WWN absent : {}".format(wwn)
    if len(found) == 1:
        return found[0], None
    if serial:
        narrowed = [item for item in found if item.get("serial") == serial]
        if len(narrowed) == 1:
            return narrowed[0], None
    return None, "WWN ambigu : {}".format(wwn)


def factory_reset(paths):
    """Efface la configuration. Les fichiers de données LUN restent. Sans marqueur, refuse."""
    marker = factory_marker(paths)
    if not marker.is_file():
        raise PersistError("mode usine refusé : marqueur absent")
    before = _data_snapshot(paths.data)
    for name in _CONFIG_FILES:
        target = paths.conf / name
        if target.is_file() or target.is_symlink():
            target.unlink()
    backups = paths.conf / "backups"
    if backups.is_dir():
        shutil.rmtree(backups)
    incoming = paths.conf / "incoming"
    if incoming.is_dir():
        shutil.rmtree(incoming)
    network = paths.etc / "network.json"
    if network.is_file() or network.is_symlink():
        network.unlink()
    after = _data_snapshot(paths.data)
    if before != after:
        raise PersistError("mode usine : les données LUN ont changé")
    marker.unlink()
    return {"cleared": True, "data_kept": True}


def generate_chap():
    return "".join(secrets.choice(_ALPHABET) for _ in range(16))


def _public_target(target):
    volume = target.get("volume") or {}
    acls = []
    for acl in target.get("acls") or []:
        if not isinstance(acl, dict):
            continue
        acls.append({
            "initiator": acl.get("initiator"),
            "chap_user": acl.get("chap_user"),
            "chap_secret_ref": acl.get("chap_secret_ref"),
        })
    return {
        "iqn": target.get("iqn"),
        "portal": target.get("portal"),
        "lun": target.get("lun"),
        "volume": {
            "wwn": volume.get("wwn") or "",
            "serial": volume.get("serial") or "",
            "size": volume.get("size"),
            "block_size": volume.get("block_size"),
        },
        "acls": acls,
    }


def _xor(key, nonce, data):
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        out.extend(block)
        counter += 1
    return bytes(left ^ right for left, right in zip(data, out))


def _data_snapshot(root):
    found = {}
    path = Path(root)
    if not path.is_dir():
        return found
    for item in path.rglob("*"):
        if item.is_file():
            found[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return found


def _give_to_webui(path):
    try:
        gid = grp.getgrnam("sanui").gr_gid
    except KeyError:
        return
    try:
        os.chown(path, 0, gid)
    except OSError:
        return
