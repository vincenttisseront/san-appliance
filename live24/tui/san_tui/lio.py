"""configfs LIO. Aucune de ces opérations n'écrit enable=0."""

import os
from pathlib import Path

ROOT = Path("/sys/kernel/config/target/iscsi")
CORE = Path("/sys/kernel/config/target/core")


def tpg(iqn):
    return ROOT / iqn / "tpgt_1"


def enable_value(iqn):
    path = tpg(iqn) / "enable"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8").strip()


def session_count(iqn):
    base = tpg(iqn)
    if not base.is_dir():
        return 0
    total = 0
    sessions = base / "sessions"
    if sessions.is_dir():
        total += sum(1 for entry in sessions.iterdir() if entry.is_dir())
    acls = base / "acls"
    if not acls.is_dir():
        return total
    for acl in acls.iterdir():
        info = acl / "info"
        if not info.is_file():
            continue
        text = info.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        if "no active iscsi session" in lowered:
            continue
        if "isid" in lowered:
            total += 1
    return total


def set_chap_password(iqn, initiator, secret):
    path = tpg(iqn) / "acls" / initiator / "auth" / "password"
    if not path.is_file():
        return False
    path.write_text(secret, encoding="utf-8")
    return True


def add_portal(iqn, address, port):
    dest = tpg(iqn) / "np" / "{}:{}".format(address, int(port))
    if dest.exists():
        return False
    dest.mkdir(parents=True)
    return True


def remove_portal(iqn, address, port):
    dest = tpg(iqn) / "np" / "{}:{}".format(address, int(port))
    if not dest.is_dir():
        return False
    dest.rmdir()
    return True


def lun_names(iqn):
    directory = tpg(iqn) / "lun"
    if not directory.is_dir():
        return []
    return sorted(entry.name for entry in directory.iterdir() if entry.name.startswith("lun_"))


def unlink_lun(iqn, number):
    """Retire un seul LUN. Les autres répertoires lun_* restent."""
    name = "lun_{}".format(int(number))
    acls = tpg(iqn) / "acls"
    if acls.is_dir():
        for acl in acls.iterdir():
            mapped = acl / name
            if mapped.is_dir():
                _rm_configfs(mapped)
    dest = tpg(iqn) / "lun" / name
    if not dest.is_dir():
        return False
    _rm_configfs(dest)
    return True


def find_backstore(device):
    if not CORE.is_dir():
        return None
    try:
        real = os.path.realpath(device)
    except OSError:
        real = device
    for plugin in CORE.iterdir():
        if not plugin.is_dir() or plugin.name in {"alua", "pr"}:
            continue
        for obj in plugin.iterdir():
            udev = obj / "udev_path"
            if not udev.is_file():
                continue
            current = udev.read_text(encoding="utf-8").strip()
            try:
                current = os.path.realpath(current)
            except OSError:
                pass
            if current == real:
                return obj
    return None


def drop_backstore(device):
    obj = find_backstore(device)
    if obj is None or not obj.is_dir():
        return False
    try:
        _rm_configfs(obj)
    except OSError as exc:
        raise OSError("backstore {} : errno {}".format(obj.name, exc.errno)) from exc
    return True


def _rm_configfs(path):
    """Retire un nœud configfs. Le lien vers le backstore est retiré avant le rmdir."""
    if path.is_symlink() or not path.is_dir():
        return
    for child in list(path.iterdir()):
        if child.is_symlink():
            child.unlink()
    try:
        path.rmdir()
    except OSError as exc:
        left = ",".join(sorted(child.name for child in path.iterdir()))
        raise OSError("{} : errno {} reste [{}]".format(path, exc.errno, left)) from exc


def wwn_of(device):
    obj = find_backstore(device)
    if obj is None:
        return ""
    for relative in ("wwn/vpd_unit_serial", "wwn"):
        path = obj / relative
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return ""
