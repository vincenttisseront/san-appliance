"""Tâches automatiques et les commandes qui les retirent."""

from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import mark_undone, undone
from san_tui.health import health_check, smartctl_runner
from san_tui.link import BOND_NAME, PLAIN_MTU
from san_tui.persist import PersistError, _read_json
from san_tui.portals import drop_portal_dirs, serving_portals
import json
from san_tui.audit import prepare
from san_tui.groupsnap import snapshot_groups
from san_tui.latency import check_from_disk
from san_tui.qos import apply_profiles
from san_tui.replicate import replicate_from, resume as replicate_resume
from san_tui.replicate import stop as replicate_stop
from san_tui.retire import retire_auto, undo as retire_undo
from san_tui.witness import evaluate, release as witness_release
from san_tui.witness import stay as witness_stay
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

def run(paths, probe, hooks=None):
    hooks = hooks or {}
    report = {"iscsi": "unchanged"}
    if _diagnostic(paths):
        report["retire"] = {"action": "skip", "reason": "diagnostic"}
        report["replicate"] = {"action": "skip", "reason": "diagnostic"}
        report["witness"] = {"action": "stay", "reason": "diagnostic"}
        report["snapshot"] = {"action": "skip", "reason": "diagnostic"}
        report["qos"] = {"action": "skip", "reason": "diagnostic"}
    else:
        _step(paths, report, "retire", lambda: _call(hooks, "retire", lambda: retire_auto(paths, probe)))
        _step(paths, report, "replicate", lambda: _call(hooks, "replicate", lambda: replicate_from(paths, probe)))
        _step(paths, report, "witness", lambda: _call(hooks, "witness", lambda: evaluate(paths)))
        _step(paths, report, "snapshot", lambda: _call(
            hooks, "snapshot", lambda: snapshot_groups(paths, probe.list_volumes()),
        ))
        _step(paths, report, "qos", lambda: _call(
            hooks, "qos", lambda: apply_profiles(paths, probe.list_volumes()),
        ))
    _step(paths, report, "audit", lambda: _call(hooks, "audit", lambda: prepare(paths, probe)))
    _step(paths, report, "latency", lambda: _call(hooks, "latency", lambda: check_from_disk(paths)))
    report["iscsi"] = "unchanged"
    return report


def main_command(args, paths, probe):
    command = args.command
    if command == "comfort":
        return _print(run(paths, probe))
    if command == "replicate":
        from san_tui.replicate import run_replication
        if args.follow:
            return _print(run_replication(paths, probe, once=False))
        return _print(replicate_from(paths, probe))
    if command == "replicate-stop":
        return _print(replicate_stop(paths))
    if command == "replicate-resume":
        return _print(replicate_resume(paths))
    if command == "witness":
        return _print(evaluate(paths))
    if command == "witness-stay":
        return _print(witness_stay(paths))
    if command == "witness-resume":
        return _print(witness_release(paths))
    if command == "snapshot-groups":
        return _print(snapshot_groups(paths, probe.list_volumes()))
    if command == "qos-apply":
        return _print(apply_profiles(paths, probe.list_volumes()))
    if command == "audit-prepare":
        return _print(prepare(paths, probe))
    if command == "inventory-api":
        from san_tui.inventory_api import serve
        return _print(serve(paths, probe))
    if command == "inventory-stop":
        from san_tui.inventory_api import stop
        return _print(stop(paths))
    if command == "inventory-resume":
        from san_tui.inventory_api import resume
        return _print(resume(paths))
    if command == "metrics":
        from san_tui.metrics import serve
        return _print(serve(paths, probe))
    if command == "metrics-stop":
        from san_tui.metrics import stop
        return _print(stop(paths))
    if command == "metrics-resume":
        from san_tui.metrics import resume_metrics
        return _print(resume_metrics(paths))
    if command == "latency-check":
        return _print(check_from_disk(paths))
    if command == "retire":
        return _print(retire_auto(paths, probe))
    if command == "retire-undo":
        return _print(retire_undo(paths))
    return 2


def _step(paths, report, name, fn):
    try:
        report[name] = fn()
    except Exception as exc:
        add_alert(paths, "error", "{} interrompu : {}".format(name, exc.__class__.__name__))
        report[name] = {"action": "skip", "reason": exc.__class__.__name__}


def _call(hooks, name, default):
    if name in hooks:
        return hooks[name]()
    return default()


def _diagnostic(paths):
    from san_tui.runtime import diagnostic
    return diagnostic(paths)


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
