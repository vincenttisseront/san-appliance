"""Effets de bord automatiques. Un échec ici ne retire pas l'export iSCSI."""

import json

from san_tui.alerts import add_alert
from san_tui.audit import prepare
from san_tui.groupsnap import snapshot_groups
from san_tui.latency import check_from_disk
from san_tui.qos import apply_profiles
from san_tui.replicate import replicate_from, resume as replicate_resume
from san_tui.replicate import stop as replicate_stop
from san_tui.retire import retire_auto, undo as retire_undo
from san_tui.witness import evaluate, release as witness_release
from san_tui.witness import stay as witness_stay


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
