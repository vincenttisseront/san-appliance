"""Second portail iSCSI si l'interface de stockage tombe. Le TPG n'est pas désactivé."""

import subprocess
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.lio import add_portal, enable_value, remove_portal
from san_tui.persist import PersistError, _read_json, _write_json, load_exports


def choose_standby(admin, storage, ifaces):
    banned = {admin or "", storage or "", "lo", "san-stor", _default_iface()}
    names = []
    for item in ifaces or []:
        name = item.get("name") if isinstance(item, dict) else str(item)
        if not name or name in banned or name.startswith(("bond", "san-stor")):
            continue
        names.append(name)
    names.sort()
    return names[0] if names else None


def _default_iface():
    """L'interface de la route par défaut n'est pas un lien de stockage."""
    import json
    import subprocess
    try:
        proc = subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            text=True, capture_output=True, check=False, timeout=2,
        )
        data = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return ""
    if isinstance(data, list) and data:
        return data[0].get("dev") or ""
    return ""


def next_address(cidr):
    host, prefix = str(cidr).split("/", 1)
    parts = [int(item) for item in host.split(".")]
    parts[3] = parts[3] + 1
    if parts[3] > 254:
        parts[3] = 1
    return "{}.{}.{}.{}/{}".format(parts[0], parts[1], parts[2], parts[3], prefix)


def host_of(cidr):
    return str(cidr or "").split("/", 1)[0]


def primary_down(probe, iface):
    item = _iface(probe, iface)
    if item is None:
        return True
    if str(item.get("carrier") or "") == "0":
        return True
    return (item.get("operstate") or "").lower() in {"down", "lowerlayerdown", "notpresent", "dormant"}


def storage_reachable(plan, probe, address):
    storage = (plan.get("storage") or {}).get("iface")
    if probe.interface_has_address(storage, address):
        return True
    if probe.interface_has_address("san-stor", address) and _bond_owns_storage(storage, plan):
        return True
    if primary_down(probe, storage) and standby_address(plan, probe):
        return True
    return False


def bind_address(target, plan, probe):
    portal = (target.get("portal") or {}).get("address")
    storage = (plan.get("storage") or {}).get("iface")
    if not primary_down(probe, storage):
        return portal
    standby = standby_address(plan, probe)
    return host_of(standby) if standby else portal


def standby_address(plan, probe):
    storage = plan.get("storage") or {}
    admin = plan.get("admin") or {}
    name = choose_standby(admin.get("iface"), storage.get("iface"), probe.list_interfaces())
    if not name:
        return None
    item = _iface(probe, name) or {}
    for cidr in item.get("addresses") or []:
        if host_of(cidr) and host_of(cidr) != host_of(admin.get("address")):
            return cidr
    derived = ""
    try:
        derived = next_address(storage.get("address") or "")
    except (ValueError, IndexError):
        return None
    if host_of(derived) == host_of(admin.get("address")):
        return None
    return derived


def once(paths, probe):
    if _undone(paths):
        return {"taken": False, "reason": "portail de secours annulé"}
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        return {"taken": False, "reason": str(exc)}
    plan = document.get("network") or {}
    storage = (plan.get("storage") or {}).get("iface")
    if not storage or not primary_down(probe, storage):
        return {"taken": False, "reason": "interface stockage joignable"}
    cidr = standby_address(plan, probe)
    if not cidr:
        add_alert(paths, "error", "pas d'interface de secours pour le portail")
        return {"taken": False, "reason": "pas d'interface de secours"}
    _assign(probe, plan, cidr)
    address = host_of(cidr)
    added = []
    for target in document.get("targets") or []:
        iqn = target.get("iqn") or ""
        port = int((target.get("portal") or {}).get("port") or 3260)
        before = enable_value(iqn)
        try:
            add_portal(iqn, address, port)
        except OSError as exc:
            add_alert(paths, "error", "portail de secours refusé : {}".format(iqn))
            continue
        added.append({
            "iqn": iqn,
            "address": address,
            "port": port,
            "enable_before": before,
            "enable_after": enable_value(iqn),
        })
    return {"taken": bool(added), "portals": added, "address": address}


def watch(paths, probe):
    import time
    while True:
        try:
            once(paths, probe)
        except Exception as exc:
            add_alert(paths, "error", "veille portail : {}".format(exc.__class__.__name__))
        time.sleep(2)


def undo(paths, probe):
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        return {"ok": False, "reason": str(exc)}
    plan = document.get("network") or {}
    cidr = standby_address(plan, probe)
    removed = []
    if cidr:
        address = host_of(cidr)
        for target in document.get("targets") or []:
            iqn = target.get("iqn") or ""
            port = int((target.get("portal") or {}).get("port") or 3260)
            try:
                if remove_portal(iqn, address, port):
                    removed.append(iqn)
            except OSError:
                return {"ok": False, "reason": "retrait du portail refusé"}
    _write_json(paths.conf / "undo" / "portal.json", {"undone": True}, 0o644)
    return {"ok": True, "removed": removed}


def _assign(probe, plan, cidr):
    storage = (plan.get("storage") or {}).get("iface")
    admin = (plan.get("admin") or {}).get("iface")
    name = choose_standby(admin, storage, probe.list_interfaces())
    if not name or not Path("/sys/class/net", name).exists():
        return
    item = _iface(probe, name) or {}
    if any(host_of(addr) == host_of(cidr) for addr in item.get("addresses") or []):
        return
    subprocess.run(["ip", "link", "set", name, "up"], check=False, capture_output=True)
    subprocess.run(["ip", "addr", "replace", cidr, "dev", name], check=False, capture_output=True)


def _bond_owns_storage(storage, plan):
    slaves = Path("/sys/class/net/san-stor/bonding/slaves")
    if not slaves.is_file() or not storage:
        return False
    names = slaves.read_text(encoding="utf-8").split()
    admin = (plan.get("admin") or {}).get("iface")
    return storage in names and admin not in names


def _iface(probe, name):
    for item in probe.list_interfaces():
        if item.get("name") == name:
            return item
    return None


def _undone(paths):
    data = _read_json(paths.conf / "undo" / "portal.json") or {}
    return bool(isinstance(data, dict) and data.get("undone"))
