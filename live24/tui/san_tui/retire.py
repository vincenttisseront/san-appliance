"""Retire un disque de données en échec. Le disque de boot n'est jamais retiré."""

import subprocess
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json


FAILED_STATES = {"offline", "dead", "failed", "faulty", "blocked", "transport-offline"}


def classify(volume):
    if not volume:
        return {"retire": False, "reason": "volume absent"}
    if volume.get("boot"):
        return {"retire": False, "reason": "disque de boot"}
    state = (volume.get("state") or "").lower()
    try:
        errors = int(volume.get("io_errors") or 0)
    except (TypeError, ValueError):
        errors = 0
    if state in FAILED_STATES or errors > 0:
        return {"retire": True, "reason": state or "erreurs d'E/S"}
    return {"retire": False, "reason": "sain"}


def linux_retire(volume, paths, run=None):
    if volume.get("boot"):
        return {"retired": False, "reason": "disque de boot", "commands": []}
    commands = []
    path = volume.get("path") or ""
    for holder in volume.get("holders") or []:
        if not str(holder).startswith("md"):
            continue
        device = "/dev/{}".format(holder)
        commands.append(["mdadm", "--manage", device, "--fail", path])
        commands.append(["mdadm", "--manage", device, "--remove", path])
    if paths.root == Path("/") and commands and run is None:
        for cmd in commands:
            subprocess.run(cmd, check=False, capture_output=True)
    elif run is not None:
        for cmd in commands:
            run(cmd)
    return {"retired": True, "reason": "échec", "commands": commands}


def retire_auto(paths, probe, run=None):
    retired = []
    for volume in probe.list_volumes():
        if not classify(volume)["retire"]:
            continue
        outcome = linux_retire(volume, paths, run=run)
        if not outcome.get("retired"):
            continue
        retired.append(volume["id"])
        add_alert(paths, "error", "disque de données retiré : {}".format(volume["id"]))
    if retired:
        _store(paths, retired)
    return {"action": "retired" if retired else "skip", "retired": retired}


def undo(paths):
    for path in (paths.conf / "retired.json", paths.run / "retired.json"):
        if path.is_file():
            path.unlink()
    return {"action": "ready", "retired": []}


def retired_ids(paths):
    found = set()
    for path in (paths.conf / "retired.json", paths.run / "retired.json"):
        try:
            data = _read_json(path)
        except PersistError:
            continue
        if isinstance(data, dict):
            for item in data.get("ids") or []:
                found.add(item)
    return found


def _store(paths, ids):
    current = sorted(retired_ids(paths).union(ids))
    document = {"version": 1, "ids": current}
    paths.ensure_runtime()
    _write_json(paths.run / "retired.json", document, 0o644)
    if not paths.conf.is_dir():
        return
    try:
        _write_json(paths.conf / "retired.json", document, 0o644)
    except OSError:
        add_alert(paths, "error", "retrait non persisté, export du disque sauté pour ce boot")
