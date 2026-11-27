"""Portails iSCSI. L'adresse IP reste celle qu'utilise l'initiateur."""

import os
import re
from pathlib import Path

from san_tui.safety import WILDCARD_ADDRS


def _host(cidr):
    if not cidr:
        return ""
    return str(cidr).split("/", 1)[0].strip()


def _ipv4(value):
    if not value or value in WILDCARD_ADDRS:
        return False
    return re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value) is not None


def serving_portals(target, plan, probe, extras=True):
    """Adresses IPv4 hors admin. Le stockage configuré passe en premier.

    Si cette adresse est absente et qu'une autre interface non admin en a une,
    celle-là sert. L'admin n'est jamais un portail.
    """
    port = int(((target or {}).get("portal") or {}).get("port") or 3260)
    storage_ip = _host(((plan or {}).get("storage") or {}).get("address"))
    admin_if = ((plan or {}).get("admin") or {}).get("iface")
    admin_ip = _host(((plan or {}).get("admin") or {}).get("address"))
    found = []
    seen = set()
    for iface in probe.list_interfaces():
        name = iface.get("name")
        if not name or name == admin_if:
            continue
        for cidr in iface.get("addresses") or []:
            ip = _host(cidr)
            if not _ipv4(ip) or ip == admin_ip or ip in seen:
                continue
            if not extras and ip != storage_ip:
                continue
            seen.add(ip)
            found.append({"address": ip, "port": port, "iface": name, "dns": ""})
    found.sort(key=lambda item: (item["address"] != storage_ip, item["address"]))
    return found


def bind_address(portal):
    """L'attache du portail est l'IP, jamais le nom DNS."""
    address = str((portal or {}).get("address") or "")
    if not _ipv4(address):
        raise ValueError("le portail s'attache à l'adresse IP")
    return address


def plan_drop(portals, gone):
    """Retire les portails disparus. Le dernier reste en service."""
    gone = set(gone or [])
    keep = [item for item in portals if item.get("address") not in gone]
    drop = [item for item in portals if item.get("address") in gone]
    if drop and not keep:
        return {"drop": [], "keep": list(portals), "reason": "dernier portail conservé"}
    if not drop:
        return {"drop": [], "keep": list(portals), "reason": "rien à retirer"}
    return {"drop": drop, "keep": keep, "reason": "retiré"}


def drop_portal_dirs(configfs, address, port=3260):
    """Retire un répertoire de portail. Ne écrit pas enable.

    configfs est la racine target (…/config). Sur un vrai configfs, rmdir
    suffit. Le dernier portail d'une cible n'est pas retiré.
    """
    needle = "{}:{}".format(address, port)
    dropped = []
    refused = []
    root = Path(configfs)
    for np in sorted(root.glob("target/iscsi/*/tpgt_*/np")):
        if not np.is_dir():
            continue
        victim = np / needle
        others = [child for child in np.iterdir() if child.is_dir() and child.name != needle]
        if not victim.is_dir():
            continue
        if not others:
            refused.append(str(victim))
            continue
        _remove_portal(victim)
        dropped.append(str(victim))
    return {"dropped": dropped, "refused": refused}


def iser_module_present(modules):
    """Vrai seulement si ib_isert.ko est dans l'arbre de modules de l'image."""
    root = Path(modules)
    if not root.is_dir():
        return False
    return any(root.rglob("ib_isert.ko*"))


def modules_dir():
    override = os.environ.get("SAN_MODULES", "")
    if override:
        return Path(override)
    return Path("/lib/modules") / os.uname().release


def _remove_portal(path):
    try:
        path.rmdir()
        return
    except OSError:
        pass
    for child in list(path.iterdir()):
        if child.is_dir():
            _remove_portal(child)
        else:
            try:
                child.unlink()
            except OSError:
                pass
    path.rmdir()
