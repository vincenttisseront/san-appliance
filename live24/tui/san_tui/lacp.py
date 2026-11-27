"""LACP sur le réseau stockage, seulement si le commutateur l'annonce."""

import socket
import subprocess
import time
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json, load_exports
from san_tui.portal import choose_standby

BOND = "san-stor"
LACP_ETHERTYPE = 0x8809


def should_bond(admin, storage, standby, seen):
    if not storage or not standby or storage == standby:
        return False
    if storage == admin or standby == admin:
        return False
    return bool(seen.get(storage)) and bool(seen.get(standby))


def lacp_seen(iface, timeout):
    try:
        raw = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(LACP_ETHERTYPE))
    except OSError:
        return False
    try:
        raw.bind((iface, 0))
        raw.settimeout(timeout)
        frame = raw.recv(2048)
    except (TimeoutError, socket.timeout, OSError):
        return False
    finally:
        raw.close()
    return len(frame) >= 15 and frame[14] == 1


def apply_lacp(paths, probe, listen_seconds=3):
    if _undone(paths):
        return {"bonded": False, "reason": "LACP annulé"}
    plan = _plan(paths)
    if not plan:
        return {"bonded": False, "reason": "réseau stockage non choisi"}
    admin = (plan.get("admin") or {}).get("iface")
    storage = (plan.get("storage") or {}).get("iface")
    standby = choose_standby(admin, storage, probe.list_interfaces())
    if not should_bond(admin, storage, standby, {storage: True, standby: True}):
        return {"bonded": False, "reason": "une seule interface stockage"}
    seen = {}
    for iface in (storage, standby):
        if not Path("/sys/class/net", iface).exists():
            seen[iface] = False
            continue
        subprocess.run(["ip", "link", "set", iface, "up"], check=False, capture_output=True)
        seen[iface] = lacp_seen(iface, listen_seconds)
    if not should_bond(admin, storage, standby, seen):
        return {"bonded": False, "reason": "LACP non annoncé", "seen": seen}
    address = (plan.get("storage") or {}).get("address")
    try:
        _bond([storage, standby], address, admin)
    except OSError as exc:
        add_alert(paths, "error", "LACP refusé : agrégat non créé")
        _restore_address([storage, standby], address)
        return {"bonded": False, "reason": exc.__class__.__name__, "seen": seen, "reverted": True}
    time.sleep(3)
    slaves = _slaves()
    if admin in slaves or not _bond_up() or storage not in slaves or standby not in slaves:
        _teardown([storage, standby], address)
        add_alert(paths, "info", "LACP annoncé, agrégat non porteur : interfaces restituées")
        return {
            "bonded": False,
            "reason": "annonce vue, agrégat non porteur, interfaces restituées",
            "seen": seen,
            "reverted": True,
            "slaves": slaves,
        }
    return {"bonded": True, "slaves": slaves, "seen": seen, "address": address}


def undo(paths, probe):
    plan = _plan(paths)
    storage = (plan.get("storage") or {}).get("iface") if plan else None
    standby = choose_standby(
        (plan.get("admin") or {}).get("iface") if plan else None,
        storage,
        probe.list_interfaces(),
    ) if plan else None
    address = (plan.get("storage") or {}).get("address") if plan else None
    ifaces = [item for item in (storage, standby) if item]
    _teardown(ifaces, address)
    _write_json(paths.conf / "undo" / "lacp.json", {"undone": True}, 0o644)
    return {"ok": True, "bond": Path("/sys/class/net", BOND).exists()}


def _bond(ifaces, address, admin):
    if admin in ifaces:
        raise OSError("admin")
    subprocess.run(["modprobe", "bonding"], check=False, capture_output=True)
    if not Path("/sys/class/net", BOND).exists():
        proc = subprocess.run(
            ["ip", "link", "add", BOND, "type", "bond", "mode", "802.3ad", "lacp_rate", "fast"],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise OSError("ip link add")
    for iface in ifaces:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False, capture_output=True)
        proc = subprocess.run(
            ["ip", "link", "set", iface, "master", BOND],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise OSError("esclave")
        subprocess.run(["ip", "link", "set", iface, "up"], check=False, capture_output=True)
    subprocess.run(["ip", "link", "set", BOND, "up"], check=False, capture_output=True)
    if address:
        subprocess.run(["ip", "addr", "replace", address, "dev", BOND], check=False, capture_output=True)


def _teardown(ifaces, address):
    if Path("/sys/class/net", BOND).exists():
        subprocess.run(["ip", "link", "set", BOND, "down"], check=False, capture_output=True)
        subprocess.run(["ip", "link", "delete", BOND], check=False, capture_output=True)
    _restore_address(ifaces, address)


def _restore_address(ifaces, address):
    if not ifaces or not address:
        return
    iface = ifaces[0]
    if Path("/sys/class/net", iface).exists():
        subprocess.run(["ip", "link", "set", iface, "up"], check=False, capture_output=True)
        subprocess.run(["ip", "addr", "replace", address, "dev", iface], check=False, capture_output=True)


def _bond_up():
    path = Path("/sys/class/net", BOND, "operstate")
    if not path.is_file():
        return False
    if path.read_text(encoding="utf-8").strip() == "up":
        return True
    status = Path("/proc/net/bonding", BOND)
    if not status.is_file():
        return False
    text = status.read_text(encoding="utf-8", errors="replace")
    return "MII Status: up" in text and "Partner Mac Address: 00:00:00:00:00:00" not in text


def _slaves():
    path = Path("/sys/class/net", BOND, "bonding", "slaves")
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8").split()


def _plan(paths):
    try:
        document, _secrets = load_exports(paths)
    except PersistError:
        document = None
    if isinstance(document, dict) and document.get("network"):
        return document["network"]
    data = _read_json(paths.etc / "network.json")
    return data if isinstance(data, dict) else None


def _undone(paths):
    data = _read_json(paths.conf / "undo" / "lacp.json") or {}
    return bool(isinstance(data, dict) and data.get("undone"))
