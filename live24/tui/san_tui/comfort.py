"""Tâches automatiques et les commandes qui les retirent."""

from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import mark_undone, undone
from san_tui.health import health_check, smartctl_runner
from san_tui.link import BOND_NAME, PLAIN_MTU
from san_tui.persist import PersistError, _read_json
from san_tui.portals import drop_portal_dirs, serving_portals
from san_tui.raid import (
    KERNEL_SPEED_MAX_KB,
    REBUILD_CAP_KB,
    SCRUB_CAP_KB,
    apply_global_cap,
    apply_sync_cap,
    idle_scrub,
    pace_cap,
    request_scrub,
)


def run_health(paths, probe):
    return health_check(paths, probe.list_volumes(), smartctl_runner())


def run_scrub(paths, sysfs="/sys"):
    dirs = _md_dirs(sysfs)
    if not dirs:
        add_alert(paths, "info", "scrub ignoré : aucun array")
        return {"started": [], "cap_kb": SCRUB_CAP_KB, "reason": "aucun array"}
    started = []
    for md_dir in dirs:
        if request_scrub(md_dir):
            started.append(md_dir.parent.name)
    return {"started": started, "cap_kb": SCRUB_CAP_KB, "reason": "check"}


def stop_scrub(paths, sysfs="/sys"):
    stopped = []
    for md_dir in _md_dirs(sysfs):
        if idle_scrub(md_dir):
            stopped.append(md_dir.parent.name)
    add_alert(paths, "info", "scrub arrêté")
    return {"stopped": stopped}


def run_pace(paths, sysfs="/sys", proc_file="/proc/sys/dev/raid/speed_limit_max"):
    if undone(paths, "pace"):
        return {"reason": "plafond retiré", "cap_kb": None}
    applied = []
    for md_dir in _md_dirs(sysfs):
        action = ""
        action_path = md_dir / "sync_action"
        if action_path.is_file():
            action = action_path.read_text(encoding="utf-8").strip()
        cap = pace_cap(action)
        if apply_sync_cap(md_dir, cap):
            applied.append({"array": md_dir.parent.name, "cap_kb": cap})
    apply_global_cap(Path(proc_file), REBUILD_CAP_KB)
    return {"applied": applied, "global_kb": REBUILD_CAP_KB, "reason": "plafond posé"}


def undo_pace(paths, sysfs="/sys", proc_file="/proc/sys/dev/raid/speed_limit_max"):
    mark_undone(paths, "pace")
    for md_dir in _md_dirs(sysfs):
        apply_sync_cap(md_dir, KERNEL_SPEED_MAX_KB)
    apply_global_cap(Path(proc_file), KERNEL_SPEED_MAX_KB)
    add_alert(paths, "info", "plafond RAID retiré")
    return {"cap_kb": KERNEL_SPEED_MAX_KB}


def undo_spare(paths, backend):
    mark_undone(paths, "spare")
    record = _spare_record(paths)
    if not record.get("id") or not record.get("array"):
        return {"removed": False, "reason": "aucun spare automatique"}
    try:
        backend.remove_spare(record["array"], record["id"])
    except Exception as exc:
        add_alert(paths, "error", "retrait du spare refusé : {}".format(exc.__class__.__name__))
        return {"removed": False, "reason": exc.__class__.__name__}
    add_alert(paths, "info", "spare retiré : {}".format(record["id"]))
    return {"removed": True, "id": record["id"]}


def drop_absent_portals(paths, probe, configfs):
    """Retire un portail dont l'adresse a disparu. Le dernier reste."""
    network = _network(paths)
    present = {
        item["address"]
        for item in serving_portals({"portal": {"port": 3260}}, network, probe, extras=True)
    }
    dropped = []
    refused = []
    root = Path(configfs)
    if not (root / "target").is_dir():
        return {"dropped": [], "refused": [], "reason": "configfs absent"}
    seen = set()
    for portal_dir in root.glob("target/iscsi/*/tpgt_*/np/*"):
        if not portal_dir.is_dir() or ":" not in portal_dir.name:
            continue
        address, _, raw_port = portal_dir.name.partition(":")
        if address in present or address in seen:
            continue
        seen.add(address)
        try:
            port = int(raw_port)
        except ValueError:
            port = 3260
        result = drop_portal_dirs(root, address, port)
        dropped.extend(result["dropped"])
        refused.extend(result["refused"])
    if dropped:
        add_alert(paths, "info", "portail retiré : {}".format(", ".join(sorted(seen))))
    if refused:
        add_alert(paths, "info", "dernier portail conservé")
    return {"dropped": dropped, "refused": refused, "reason": "contrôlé"}


def undo_extra_portals(paths, configfs):
    """Retire les portails ajoutés tout seuls. Garde l'adresse de stockage."""
    mark_undone(paths, "extra-portals")
    network = _network(paths)
    storage = str(((network.get("storage") or {}).get("address") or "").split("/", 1)[0])
    root = Path(configfs)
    dropped = []
    refused = []
    if storage and (root / "target").is_dir():
        seen = set()
        for portal_dir in root.glob("target/iscsi/*/tpgt_*/np/*"):
            if not portal_dir.is_dir() or ":" not in portal_dir.name:
                continue
            address, _, raw_port = portal_dir.name.partition(":")
            if address == storage or address in seen:
                continue
            seen.add(address)
            try:
                port = int(raw_port)
            except ValueError:
                port = 3260
            result = drop_portal_dirs(root, address, port)
            dropped.extend(result["dropped"])
            refused.extend(result["refused"])
    add_alert(paths, "info", "portails supplémentaires retirés")
    return {"dropped": dropped, "refused": refused}


def undo_iser(paths, configfs):
    mark_undone(paths, "iser")
    cleared = 0
    root = Path(configfs)
    for flag in root.glob("target/iscsi/*/tpgt_*/np/*/iser"):
        try:
            flag.write_text("0\n", encoding="utf-8")
            cleared += 1
        except OSError:
            continue
    add_alert(paths, "info", "iSER retiré, iSCSI TCP conservé")
    return {"cleared": cleared}


def undo_bond(paths, runner):
    mark_undone(paths, "bond")
    try:
        runner.delete(BOND_NAME)
    except Exception as exc:
        add_alert(paths, "error", "retrait de l'agrégat ignoré : {}".format(exc.__class__.__name__))
        return {"removed": False, "reason": exc.__class__.__name__}
    add_alert(paths, "info", "agrégat retiré")
    return {"removed": True}


def undo_mtu(paths, runner, iface):
    mark_undone(paths, "mtu")
    if iface:
        runner.set_mtu(iface, PLAIN_MTU)
    add_alert(paths, "info", "MTU 1500")
    return {"mtu": PLAIN_MTU, "iface": iface or ""}


def _md_dirs(sysfs):
    block = Path(sysfs) / "block"
    if not block.is_dir():
        return []
    found = []
    for entry in sorted(block.glob("md*")):
        md_dir = entry / "md"
        if md_dir.is_dir():
            found.append(md_dir)
    return found


def _network(paths):
    for path in (paths.conf / "exports.json", paths.etc / "network.json"):
        try:
            document = _read_json(path)
        except PersistError:
            continue
        if not isinstance(document, dict):
            continue
        network = document.get("network") or document
        if isinstance(network, dict) and (network.get("storage") or network.get("admin")):
            return network
    return {}


def _spare_record(paths):
    for path in (paths.conf / "auto-spare.json", paths.run / "auto-spare.json"):
        try:
            document = _read_json(path)
        except PersistError:
            continue
        if isinstance(document, dict):
            return document
    return {}
