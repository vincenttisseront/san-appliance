"""TUI curses : disques, interfaces, réseaux, exports, alertes."""

import curses
import json

from san_tui.alerts import load_alerts
from san_tui.persist import _read_json
from san_tui.probe import inventory


SCREENS = (
    "Disques",
    "Interfaces",
    "Réseau admin",
    "Réseau stockage",
    "Exports iSCSI",
    "Alertes",
)


def screen_lines(name, snap, paths):
    if name == "Disques":
        lines = ["Inventaire. Le boot choisit le disque de données. Cet écran n'écrit rien.", ""]
        if not snap["disks"]:
            lines.append("Aucun volume avec identifiant by-id.")
        for disk in snap["disks"]:
            flags = []
            if disk.get("boot"):
                flags.append("boot")
            if disk.get("mounts"):
                flags.append("monté")
            if disk.get("active"):
                flags.append("actif")
            mark = " ".join(flags) or "libre"
            lines.append("{}  [{}]".format(disk["id"], mark))
            lines.append("  wwn={} série={} taille={}".format(
                disk.get("wwn") or "-", disk.get("serial") or "-", disk.get("size")
            ))
            health = _health_line(paths, disk["id"])
            if health:
                lines.append(health)
        return lines
    if name == "Interfaces":
        lines = []
        for iface in snap["interfaces"]:
            addrs = ", ".join(iface.get("addresses") or []) or "sans adresse"
            lines.append("{}  {}  {}".format(iface["name"], iface.get("operstate") or "-", addrs))
        return lines or ["Aucune interface."]
    if name == "Réseau admin":
        return _network_lines(paths, "admin")
    if name == "Réseau stockage":
        return _network_lines(paths, "storage")
    if name == "Exports iSCSI":
        try:
            document = _read_json(paths.conf / "exports.json")
        except Exception as exc:
            return ["conf illisible : {}".format(exc.__class__.__name__)]
        if not document:
            return ["Aucun export enregistré. Le boot en crée un quand un initiateur a été vu. Annulation : san-tui rollback."]
        lines = []
        for target in document.get("targets") or []:
            acls = target.get("acls") or [{}]
            address = (target.get("portal") or {}).get("address")
            port = (target.get("portal") or {}).get("port")
            dns = _portal_dns(paths, target.get("iqn"), address)
            portal = "{}:{}".format(address, port)
            if dns:
                portal = "{} ({})".format(portal, dns)
            lines.append("{}  lun {}  portail {}".format(
                target.get("iqn"),
                target.get("lun"),
                portal,
            ))
            lines.append("  volume {}".format((target.get("volume") or {}).get("id")))
            lines.append("  acl {}  chap {}".format(
                acls[0].get("initiator"), acls[0].get("chap_user")
            ))
        return lines
    alerts = load_alerts(paths)
    if not alerts:
        return ["Aucune alerte."]
    lines = ["Les alertes sont aussi écrites sur la console série.", ""]
    lines.extend("{}  {}  {}".format(item["time"], item["level"], item["message"]) for item in alerts)
    return lines


def _health_line(paths, disk_id):
    try:
        data = _read_json(paths.run / "health.json") or {}
    except Exception:
        return ""
    item = (data.get("disks") or {}).get(disk_id) or {}
    if not item:
        return ""
    passed = item.get("passed")
    if passed is False:
        state = "échec"
    elif passed is True:
        state = "ok"
    else:
        state = "inconnu"
    temp = item.get("temperature")
    if isinstance(temp, int):
        return "  SMART {}  {}°C".format(state, temp)
    return "  SMART {}".format(state)


def _portal_dns(paths, iqn, address):
    try:
        data = _read_json(paths.run / "portals.json") or {}
    except Exception:
        return ""
    for item in data.get(iqn) or []:
        if item.get("address") == address and item.get("dns"):
            return item["dns"]
    return ""


def _network_lines(paths, role):
    plan = _read_json(paths.etc / "network.json") or {}
    item = plan.get(role) or {}
    if not item:
        return ["Pas encore choisi. Le boot le fera. Annulation : san-tui rollback."]
    return ["Choisi au boot : {}".format(item.get("iface")), "adresse {}".format(item.get("address") or "en attente")]


def run_tui(paths, probe):
    if not _isatty():
        snap = inventory(probe)
        print(json.dumps({
            "note": "pas de terminal : inventaire seul, aucun export",
            "disks": len(snap["disks"]),
            "interfaces": [item["name"] for item in snap["interfaces"]],
        }, ensure_ascii=False))
        return 0
    curses.wrapper(lambda stdscr: _loop(stdscr, paths, probe))
    return 0


def _loop(stdscr, paths, probe):
    curses.curs_set(0)
    index = 0
    while True:
        snap = inventory(probe)
        stdscr.erase()
        stdscr.addstr(0, 0, "san-tui  —  flèches, entrée, q quitte")
        for pos, name in enumerate(SCREENS):
            marker = ">" if pos == index else " "
            stdscr.addstr(2 + pos, 0, "{} {}".format(marker, name))
        stdscr.addstr(10, 0, "—" * 40)
        row = 11
        height, width = stdscr.getmaxyx()
        for line in screen_lines(SCREENS[index], snap, paths):
            if row >= height - 1:
                break
            stdscr.addnstr(row, 0, line, max(1, width - 1))
            row += 1
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            index = (index - 1) % len(SCREENS)
        elif key in (curses.KEY_DOWN, ord("j"), curses.KEY_ENTER, 10, 13):
            index = (index + 1) % len(SCREENS)


def _isatty():
    import sys
    return sys.stdin.isatty() and sys.stdout.isatty()
