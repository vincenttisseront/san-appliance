"""Clone un LUN vers un volume libre assez grand, avec un WWN neuf."""

import os
import subprocess

import secrets

from san_tui.alerts import add_alert
from san_tui.lio import wwn_of
from san_tui.persist import PersistError, _read_json, _write_json, load_exports
from san_tui.thin import _lvs, lv_by_path


def new_wwn(source):
    current = (source or "").lower()
    for _ in range(8):
        wwn = "naa." + secrets.token_hex(8)
        if wwn != current:
            return wwn
    return "naa." + secrets.token_hex(8)


def clone(paths, probe):
    """Choisit le plus petit LV libre qui contient la source. N'enlève pas le LUN source."""
    if _undone(paths):
        return {"ok": False, "reason": "clone annulé"}
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        return {"ok": False, "reason": str(exc)}
    existing = _read_json(paths.conf / "clone.json") or {}
    if isinstance(existing, dict) and existing.get("wwn"):
        return {"ok": True, "reason": "clone déjà en place", "wwn": existing.get("wwn"), "volume": existing.get("volume")}
    source, iqn, lun = _source(document)
    if source is None:
        return {"ok": False, "reason": "aucun LUN source"}
    current = probe.get_volume(source.get("id")) or source
    device = current.get("path")
    if not device or not os.path.exists(device):
        return {"ok": False, "reason": "source absente"}
    snap = _snapshot_device(paths, source.get("id"))
    origin = snap or device
    try:
        free = _free_lv(document, probe, int(current.get("size") or 0))
    except OSError as exc:
        add_alert(paths, "error", "clone : inventaire LVM impossible")
        return {"ok": False, "reason": exc.__class__.__name__}
    if isinstance(free, str):
        return {"ok": False, "reason": free}
    wwn = new_wwn(current.get("wwn") or wwn_of(device))
    try:
        _dd(origin, free["path"])
        number = _attach(document, secrets, free, wwn, iqn)
    except Exception as exc:
        add_alert(paths, "error", "clone refusé")
        return {"ok": False, "reason": exc.__class__.__name__, "source_wwn": wwn_of(device)}
    record = {
        "version": 1,
        "source": source.get("id"),
        "volume": free["id"],
        "wwn": wwn,
        "iqn": iqn,
        "lun": number,
    }
    _write_json(paths.conf / "clone.json", record, 0o644)
    return {
        "ok": True,
        "volume": free["id"],
        "wwn": wwn_of(free["path"]) or wwn,
        "source": source.get("id"),
        "source_wwn": wwn_of(device),
        "lun": number,
    }


def undo(paths):
    record = _read_json(paths.conf / "clone.json") or {}
    removed = False
    if isinstance(record, dict) and record.get("iqn") and record.get("lun") is not None:
        from san_tui.lio import unlink_lun
        try:
            removed = unlink_lun(record["iqn"], int(record["lun"]))
        except OSError:
            return {"ok": False, "reason": "retrait du LUN clone refusé"}
    _write_json(paths.conf / "undo" / "clone.json", {"undone": True}, 0o644)
    if paths.conf.joinpath("clone.json").is_file():
        paths.conf.joinpath("clone.json").unlink()
    return {"ok": True, "removed": removed}


def _snapshot_device(paths, ident):
    data = _read_json(paths.conf / "snapshots.json") or {}
    if not isinstance(data, dict):
        return None
    for item in data.get("snapshots") or []:
        if item.get("volume") == ident and item.get("vg") and item.get("snap"):
            path = "/dev/{}/{}".format(item["vg"], item["snap"])
            if os.path.exists(path):
                return path
    return None


def _source(document):
    for target in document.get("targets") or []:
        volume = target.get("volume")
        if isinstance(volume, dict):
            return volume, target.get("iqn"), int(target.get("lun") or 0)
    return None, None, None


def _free_lv(document, probe, need_bytes=0):
    used = set()
    for target in document.get("targets") or []:
        if isinstance(target.get("volume"), dict):
            used.add(target["volume"].get("id"))
        for extra in target.get("extras") or []:
            if isinstance(extra.get("volume"), dict):
                used.add(extra["volume"].get("id"))
    candidates = []
    for row in _lvs():
        attr = row.get("lv_attr") or ""
        if row.get("lv_name", "").startswith(("sansnap", "santmp", "sanpool")):
            continue
        if not attr or attr[0] in {"s", "S", "t", "T", "e", "C"}:
            continue
        if row.get("origin") or row.get("pool_lv"):
            continue
        path = row.get("lv_path")
        if not path:
            continue
        volume = _volume_for_path(probe, path)
        if volume is None or volume.get("id") in used:
            continue
        if volume.get("mounts") or volume.get("boot"):
            continue
        proc = subprocess.run(
            ["blkid", "-p", "-o", "value", "-s", "TYPE", path],
            text=True, capture_output=True, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            continue
        candidates.append(volume)
    if not candidates:
        return "aucun volume libre"
    fitting = []
    for volume in candidates:
        size = int(volume.get("size") or 0)
        if need_bytes and size and size < need_bytes:
            continue
        fitting.append(volume)
    if not fitting:
        return "aucun volume libre assez grand"
    fitting.sort(key=lambda item: (int(item.get("size") or 0), item.get("id") or ""))
    return fitting[0]


def _volume_for_path(probe, path):
    real = os.path.realpath(path)
    for volume in probe.list_volumes():
        try:
            if os.path.realpath(volume.get("path") or "") == real:
                return volume
        except OSError:
            continue
    return None


def _attach(document, secrets, volume, wwn, iqn):
    from rtslib_fb import BlockStorageObject, LUN, MappedLUN, NodeACL, TPG, Target, FabricModule
    number = _next_lun(iqn)
    volume = dict(volume)
    volume["lun"] = number
    name = "clone{}".format(number)
    storage = BlockStorageObject(name, dev=volume["path"], wwn=wwn)
    fabric = FabricModule("iscsi")
    tgt = Target(fabric, iqn, mode="lookup")
    tpg = TPG(tgt, 1, mode="lookup")
    lun = LUN(tpg, number, storage_object=storage)
    for target in document.get("targets") or []:
        if target.get("iqn") != iqn:
            continue
        for acl in target.get("acls") or []:
            node = NodeACL(tpg, acl["initiator"], mode="lookup")
            MappedLUN(node, number, tpg_lun=lun, write_protect=False)
    volume["lun"] = number
    return number


def _next_lun(iqn):
    from san_tui.lio import lun_names
    numbers = []
    for name in lun_names(iqn):
        try:
            numbers.append(int(name.split("_", 1)[1]))
        except (IndexError, ValueError):
            continue
    return (max(numbers) + 1) if numbers else 1


def _dd(source, target):
    proc = subprocess.run(
        ["dd", "if={}".format(source), "of={}".format(target), "bs=1M", "conv=fsync"],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or "dd").strip()[-200:])


def _undone(paths):
    data = _read_json(paths.conf / "undo" / "clone.json") or {}
    return bool(isinstance(data, dict) and data.get("undone"))
