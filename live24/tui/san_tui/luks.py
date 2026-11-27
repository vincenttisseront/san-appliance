"""Chiffrement au repos. La clé vit sur le support de conf, jamais dans l'ISO."""

import os
import subprocess

from san_tui.alerts import add_alert
from san_tui.persist import _read_json, _write_json

MAP_NAME = "/run/san/crypt-open.json"


def key_file(paths):
    return paths.conf / "keys" / "volume.key"


def refusal_for(paths, volume):
    """Refus seulement si le volume est LUKS et que la clé manque."""
    device = (volume or {}).get("path")
    if not device or not os.path.exists(device):
        return None
    try:
        luks = is_luks(device)
    except OSError:
        return None
    if not luks:
        return None
    key = key_file(paths)
    if not key.is_file() or key.stat().st_size == 0:
        return "clé de chiffrement absente : export refusé"
    mapper = mapper_name(volume)
    if not os.path.exists("/dev/mapper/" + mapper):
        if not open_volume(device, mapper, key):
            return "volume chiffré non ouvert"
        _remember(volume.get("id"), "/dev/mapper/" + mapper)
    return None


def exported_path(volume):
    """Chemin servi : le mapper si ce volume a été ouvert, sinon le volume."""
    ident = (volume or {}).get("id")
    mapping = _read_map()
    mapped = mapping.get(ident)
    if mapped and os.path.exists(mapped):
        return mapped
    return (volume or {}).get("path")


def prepare(paths, probe):
    """Ouvre les LUKS. Chiffre un LV vide géré. N'invente pas de clé pour un LUKS déjà là."""
    from san_tui.persist import PersistError, load_exports
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        add_alert(paths, "error", "chiffrement : conf illisible")
        return {"opened": [], "skipped": [{"volume": "*", "reason": str(exc)}]}
    opened = []
    skipped = []
    for volume in _iter_volumes(document):
        current = probe.get_volume(volume.get("id")) or volume
        device = current.get("path")
        ident = volume.get("id") or "?"
        if not device or not os.path.exists(device):
            skipped.append({"volume": ident, "reason": "volume absent"})
            continue
        try:
            luks = is_luks(device)
        except OSError:
            add_alert(paths, "error", "cryptsetup indisponible : {}".format(ident))
            skipped.append({"volume": ident, "reason": "cryptsetup indisponible"})
            continue
        key = key_file(paths)
        if luks and (not key.is_file() or key.stat().st_size == 0):
            add_alert(paths, "error", "clé de chiffrement absente : export refusé : {}".format(ident))
            skipped.append({"volume": ident, "reason": "clé de chiffrement absente : export refusé"})
            continue
        if not luks:
            if not _blank_managed(device):
                skipped.append({"volume": ident, "reason": "déjà occupé, laissé en clair"})
                continue
            if key.is_file() and key.stat().st_size > 0:
                pass
            else:
                key = _create_key(paths)
            if key is None:
                skipped.append({"volume": ident, "reason": "clé non écrite hors de l'image"})
                continue
            if not format_volume(device, key):
                add_alert(paths, "error", "luksFormat refusé : {}".format(ident))
                skipped.append({"volume": ident, "reason": "luksFormat refusé"})
                continue
        mapper = mapper_name(current)
        if not open_volume(device, mapper, key):
            add_alert(paths, "error", "luksOpen refusé : {}".format(ident))
            skipped.append({"volume": ident, "reason": "luksOpen refusé"})
            continue
        _remember(ident, "/dev/mapper/" + mapper)
        opened.append({"volume": ident, "mapper": "/dev/mapper/" + mapper, "mode": _mode(key)})
    return {"opened": opened, "skipped": skipped, "key": str(key_file(paths))}


def is_luks(device):
    proc = subprocess.run(
        ["cryptsetup", "isLuks", device],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode == 127 or "No such file" in (proc.stderr or "") and "cryptsetup" in (proc.stderr or ""):
        raise OSError("cryptsetup absent")
    return proc.returncode == 0


def format_volume(device, key):
    proc = subprocess.run(
        ["cryptsetup", "luksFormat", "--batch-mode", "--type", "luks2", "--key-file", str(key), device],
        text=True, capture_output=True, check=False,
    )
    return proc.returncode == 0


def open_volume(device, name, key):
    if os.path.exists("/dev/mapper/" + name):
        return True
    proc = subprocess.run(
        ["cryptsetup", "open", "--key-file", str(key), device, name],
        text=True, capture_output=True, check=False,
    )
    return proc.returncode == 0


def mapper_name(volume):
    raw = (volume.get("id") or volume.get("path") or "vol").rsplit("/", 1)[-1]
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in raw).strip("-")
    return "san-crypt-" + cleaned[-40:]


def _create_key(paths):
    """Crée la clé sur le support de conf. Refuse un chemin dans l'image."""
    path = key_file(paths)
    resolved = str(path)
    if resolved.startswith(("/usr", "/etc/san", "/lib", "/boot")):
        return None
    if "rootfs" in resolved or resolved.endswith(".iso"):
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        os.chmod(path, 0o600)
        return path
    import secrets
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, secrets.token_bytes(64))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return path


def _blank_managed(device):
    from san_tui.thin import lv_by_path
    try:
        info = lv_by_path(device)
    except OSError:
        return False
    if not info or info.get("segtype") in {"thin-pool", "cache-pool"}:
        return False
    if info.get("attr", "").startswith(("s", "S", "t", "e")):
        return False
    proc = subprocess.run(["blkid", "-p", "-o", "value", "-s", "TYPE", device], text=True, capture_output=True, check=False)
    if proc.returncode == 0 and proc.stdout.strip():
        return False
    return _zero_prefix(device)


def _zero_prefix(device):
    try:
        fd = os.open(device, os.O_RDONLY)
    except OSError:
        return False
    try:
        chunk = os.read(fd, 1024 * 1024)
    finally:
        os.close(fd)
    return bool(chunk) and chunk == b"\0" * len(chunk)


def _iter_volumes(document):
    for target in document.get("targets") or []:
        if isinstance(target.get("volume"), dict):
            yield target["volume"]
        for extra in target.get("extras") or []:
            if isinstance(extra.get("volume"), dict):
                yield extra["volume"]


def _remember(ident, mapper):
    mapping = _read_map()
    if ident:
        mapping[ident] = mapper
    path = _map_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, mapping, 0o644)


def _read_map():
    data = _read_json(_map_path())
    return data if isinstance(data, dict) else {}


def _map_path():
    override = os.environ.get("SAN_CRYPT_MAP")
    if override:
        return __import__("pathlib").Path(override)
    return __import__("pathlib").Path(MAP_NAME)


def _mode(path):
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return ""
