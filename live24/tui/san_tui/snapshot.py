"""Instantané crash-consistent d'un LUN, et retour d'un seul LUN."""

import os
import subprocess

from san_tui.alerts import add_alert
from san_tui.lio import drop_backstore, enable_value, lun_names, unlink_lun
from san_tui.persist import PersistError, _read_json, _write_json, load_exports
from san_tui.thin import lv_by_path


def take(paths, probe):
    """Copie crash-consistent, sans fsfreeze. Un échec laisse l'export en place."""
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        return {"snapshots": [], "skipped": [{"volume": "*", "reason": str(exc)}]}
    state = _load(paths)
    done = []
    skipped = []
    for volume in _exported(document):
        ident = volume.get("id") or "?"
        current = probe.get_volume(ident) or volume
        device = current.get("path")
        if ident in {item.get("volume") for item in state.get("snapshots") or []}:
            skipped.append({"volume": ident, "reason": "instantané déjà présent"})
            continue
        try:
            info = lv_by_path(device) if device and os.path.exists(device) else None
        except OSError:
            info = None
        if not info or not info.get("vg"):
            skipped.append({"volume": ident, "reason": "pas un volume LVM"})
            continue
        try:
            snap = _copy_snapshot(info)
        except OSError as exc:
            add_alert(paths, "error", "instantané refusé : {}".format(ident))
            skipped.append({"volume": ident, "reason": str(exc) or "lvcreate refusé"})
            continue
        if not snap:
            add_alert(paths, "error", "instantané refusé : {}".format(ident))
            skipped.append({"volume": ident, "reason": "copie impossible"})
            continue
        state.setdefault("snapshots", []).append({
            "volume": ident,
            "vg": info["vg"],
            "lv": info["lv"],
            "snap": snap,
        })
        done.append({"volume": ident, "snap": snap, "enable": _enable_of(document, ident)})
    state["version"] = 1
    _write_json(paths.conf / "snapshots.json", state, 0o644)
    return {"snapshots": done, "skipped": skipped}


def rollback(paths, probe, volume_id=None):
    """Reprend un seul LUN. Les autres LUN et enable ne sont pas modifiés."""
    state = _load(paths)
    snaps = state.get("snapshots") or []
    chosen = _pick(snaps, volume_id)
    if isinstance(chosen, str):
        return {"ok": False, "reason": chosen, "volumes": [item.get("volume") for item in snaps]}
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        return {"ok": False, "reason": str(exc)}
    others = []
    target = None
    lun = 0
    for item in document.get("targets") or []:
        if (item.get("volume") or {}).get("id") == chosen["volume"]:
            target = item
            lun = int(item.get("lun") or 0)
        else:
            for extra in item.get("extras") or []:
                if (extra.get("volume") or {}).get("id") == chosen["volume"]:
                    target = item
                    lun = int(extra.get("lun") or 0)
        for name in lun_names(item.get("iqn") or ""):
            if item is target and name == "lun_{}".format(lun):
                continue
            others.append({"iqn": item.get("iqn"), "lun": name, "enable": enable_value(item.get("iqn") or "")})
    if target is None:
        return {"ok": False, "reason": "LUN absent des exports"}
    iqn = target.get("iqn") or ""
    enable_before = enable_value(iqn)
    current = probe.get_volume(chosen["volume"]) or {}
    device = current.get("path")
    snap_path = "/dev/{}/{}".format(chosen["vg"], chosen["snap"])
    unlinked = False
    try:
        unlinked = unlink_lun(iqn, lun)
        if device:
            drop_backstore(device)
        _dd(snap_path, "/dev/{}/{}".format(chosen["vg"], chosen["lv"]))
        _relink(target, secrets, chosen["volume"], lun, probe)
    except Exception as exc:
        if unlinked:
            try:
                _relink(target, secrets, chosen["volume"], lun, probe)
            except Exception:
                pass
        add_alert(paths, "error", "retour arrière refusé : {}".format(chosen["volume"]))
        return {
            "ok": False,
            "reason": "{}: {}".format(exc.__class__.__name__, exc),
            "others": others,
            "enable": enable_value(iqn),
            "luns": lun_names(iqn),
        }
    return {
        "ok": True,
        "volume": chosen["volume"],
        "enable_before": enable_before,
        "enable_after": enable_value(iqn),
        "others": others,
        "other_enables": {item["iqn"]: enable_value(item["iqn"] or "") for item in others},
    }


def _copy_snapshot(info):
    vg = info["vg"]
    lv = info["lv"]
    temp = "santmp{}".format(lv)[:16]
    dest = "sansnap{}".format(lv)[:16]
    size = max(int(info.get("size_mib") or 32), 32)
    proc = subprocess.run(
        ["lvcreate", "--yes", "--snapshot", "-n", temp, "-L", "32M", "{}/{}".format(vg, lv)],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise OSError("lvcreate")
    try:
        proc = subprocess.run(
            ["lvcreate", "--yes", "-n", dest, "-L", "{}M".format(size), vg],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            raise OSError("lvcreate copie")
        source = "/dev/{}/{}".format(vg, temp)
        target = "/dev/{}/{}".format(vg, dest)
        _dd(source, target)
    finally:
        subprocess.run(["lvremove", "--yes", "{}/{}".format(vg, temp)], check=False, capture_output=True)
    return dest


def _dd(source, target):
    proc = subprocess.run(
        ["dd", "if={}".format(source), "of={}".format(target), "bs=1M", "conv=fsync"],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or "dd").strip()[-200:])


def _relink(target, secrets, volume_id, lun, probe):
    from san_tui.iscsi import map_lun
    volume = probe.get_volume(volume_id)
    if volume is None:
        raise OSError("volume absent")
    map_lun(target, secrets, volume, lun)


def _exported(document):
    found = []
    for target in document.get("targets") or []:
        if isinstance(target.get("volume"), dict):
            found.append(target["volume"])
        for extra in target.get("extras") or []:
            if isinstance(extra.get("volume"), dict):
                found.append(extra["volume"])
    return found


def _load(paths):
    try:
        data = _read_json(paths.conf / "snapshots.json") or {"version": 1, "snapshots": []}
    except PersistError:
        data = {"version": 1, "snapshots": []}
    if not isinstance(data, dict):
        data = {"version": 1, "snapshots": []}
    data.setdefault("snapshots", [])
    return data


def _pick(snaps, volume_id):
    if volume_id:
        for item in snaps:
            if item.get("volume") == volume_id:
                return item
        return "instantané absent"
    if len(snaps) == 1:
        return snaps[0]
    return "plusieurs LUN : préciser le volume"


def _enable_of(document, ident):
    for item in document.get("targets") or []:
        if (item.get("volume") or {}).get("id") == ident:
            return enable_value(item.get("iqn") or "")
        for extra in item.get("extras") or []:
            if (extra.get("volume") or {}).get("id") == ident:
                return enable_value(item.get("iqn") or "")
    return None
