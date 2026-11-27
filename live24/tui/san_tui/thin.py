"""Pool thin : alerte avant le plein, pas d'export une fois plein."""

import os
import subprocess

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json

ALERT_AT = 80
FULL_AT = 100
POOL_NAME = "sanpool"
RESERVE_MIB = 256


def classify_percent(data_percent, metadata_percent=None):
    data = _number(data_percent)
    meta = _number(metadata_percent)
    full = (data is not None and data >= FULL_AT) or (meta is not None and meta >= FULL_AT)
    alert = (data is not None and ALERT_AT <= data < FULL_AT) or (
        meta is not None and ALERT_AT <= meta < FULL_AT
    )
    return {"full": full, "alert": alert, "data_percent": data, "metadata_percent": meta}


def refusal_for(volume):
    """None si l'export peut continuer. Une erreur d'outil n'est pas un refus."""
    path = (volume or {}).get("path")
    if not path or not os.path.exists(path):
        return None
    try:
        info = lv_by_path(path)
    except OSError:
        return None
    if not info:
        return None
    if info.get("segtype") not in {"thin", "thin-pool"} and not info.get("pool"):
        return None
    state = classify_percent(info.get("data_percent"), info.get("metadata_percent"))
    if state["full"]:
        return "pool thin plein : export refusé"
    return None


def ensure_pools(paths):
    """Crée sanpool sur les VG enregistrés s'il reste de la place après la réserve."""
    try:
        document = _read_json(paths.conf / "lvm.json")
    except PersistError as exc:
        add_alert(paths, "error", "thin : lvm illisible : {}".format(exc))
        return {"created": [], "skipped": [{"name": "*", "reason": str(exc)}]}
    if not document:
        return {"created": [], "skipped": []}
    created = []
    skipped = []
    alerts = []
    for group in document.get("groups") or []:
        name = group.get("name") or ""
        if not name or "/" in name:
            skipped.append({"name": name or "?", "reason": "nom de groupe refusé"})
            continue
        try:
            result = _ensure_one(name)
        except OSError as exc:
            add_alert(paths, "error", "thin {} : outil indisponible".format(name))
            skipped.append({"name": name, "reason": exc.__class__.__name__})
            continue
        if result.get("created"):
            created.append(name)
        if result.get("reason"):
            skipped.append({"name": name, "reason": result["reason"]})
        state = result.get("state") or {}
        if state.get("alert") or state.get("full"):
            alerts.append({"name": name, **state})
            level = "error" if state.get("full") else "warning"
            add_alert(
                paths,
                level,
                "pool thin {} à {}%".format(name, state.get("data_percent")),
            )
    return {"created": created, "skipped": skipped, "alerts": alerts}


def lv_by_path(path):
    real = os.path.realpath(path)
    for row in _lvs():
        lv_path = row.get("lv_path") or ""
        try:
            same = os.path.realpath(lv_path) == real
        except OSError:
            same = lv_path == path
        if not same:
            continue
        pool_name = row.get("pool_lv") or ""
        data = row.get("data_percent")
        meta = row.get("metadata_percent")
        if pool_name:
            pool = _pool_row(row.get("vg_name"), pool_name)
            if pool:
                data = pool.get("data_percent")
                meta = pool.get("metadata_percent")
        return {
            "vg": row.get("vg_name"),
            "lv": row.get("lv_name"),
            "path": lv_path,
            "segtype": row.get("segtype"),
            "pool": pool_name or None,
            "attr": row.get("lv_attr") or "",
            "size_mib": _mib(row.get("lv_size")),
            "data_percent": data,
            "metadata_percent": meta,
        }
    return None


def _ensure_one(vg):
    rows = [row for row in _lvs() if row.get("vg_name") == vg]
    if any((row.get("lv_name") == POOL_NAME) or (row.get("segtype") == "thin-pool") for row in rows):
        pool = next(row for row in rows if row.get("segtype") == "thin-pool" or row.get("lv_name") == POOL_NAME)
        return {
            "created": False,
            "state": classify_percent(pool.get("data_percent"), pool.get("metadata_percent")),
        }
    free = _vg_free_mib(vg)
    if free is None:
        return {"created": False, "reason": "espace libre illisible"}
    size = free - RESERVE_MIB
    if size < 64:
        return {"created": False, "reason": "réserve de snapshots conservée, pool non créé"}
    proc = subprocess.run(
        ["lvcreate", "--yes", "--type", "thin-pool", "-L", "{}M".format(size), "-n", POOL_NAME, vg],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return {"created": False, "reason": "lvcreate refusé"}
    return {"created": True, "state": classify_percent(0, 0)}


def _pool_row(vg, pool):
    for row in _lvs():
        if row.get("vg_name") == vg and row.get("lv_name") == pool:
            return row
    return None


def _lvs():
    proc = subprocess.run(
        [
            "lvs", "--noheadings", "--separator", "|", "--units", "b", "--nosuffix",
            "-o", "lv_path,vg_name,lv_name,segtype,pool_lv,data_percent,metadata_percent,lv_attr,lv_size,origin",
        ],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise OSError(proc.stderr.strip() or "lvs refusé")
    rows = []
    for line in proc.stdout.splitlines():
        parts = [item.strip() for item in line.split("|")]
        if len(parts) < 10:
            continue
        rows.append({
            "lv_path": parts[0],
            "vg_name": parts[1],
            "lv_name": parts[2],
            "segtype": parts[3],
            "pool_lv": parts[4],
            "data_percent": parts[5],
            "metadata_percent": parts[6],
            "lv_attr": parts[7],
            "lv_size": parts[8],
            "origin": parts[9],
        })
    return rows


def _vg_free_mib(vg):
    proc = subprocess.run(
        ["vgs", "--noheadings", "--units", "m", "--nosuffix", "-o", "vg_free", vg],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        return int(float(proc.stdout.strip().splitlines()[0]))
    except (ValueError, IndexError):
        return None


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mib(value):
    try:
        return int(int(value) / (1024 * 1024))
    except (TypeError, ValueError):
        return None
