"""Un instantané commun aux LUN qui portent déjà le même identifiant de groupe."""

import shutil
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json


def discover(volumes, paths):
    grouped = {}
    by_id = {}
    for volume in volumes or []:
        if volume.get("id"):
            by_id[volume["id"]] = volume
        gid = _group_of(volume)
        _add(grouped, gid, volume)
    try:
        document = _read_json(paths.conf / "groups.json")
    except PersistError:
        document = None
    if isinstance(document, dict):
        for member in document.get("members") or []:
            volume = by_id.get(member.get("id"))
            if volume is None:
                continue
            gid = (member.get("group") or "").strip()
            _add(grouped, gid, volume)
    return {gid: members for gid, members in grouped.items() if len(members) >= 2}


def snapshot_groups(paths, volumes, take=None, now=None):
    groups = discover(volumes, paths)
    if not groups:
        return {"action": "skip", "reason": "aucun groupe"}
    take = take or file_take
    clock = now or _stamp
    made = []
    for gid, members in sorted(groups.items()):
        instant = clock()
        staging = paths.run / "snapshots" / _safe(gid) / instant
        taken = []
        failed = False
        for member in members:
            try:
                item = take(member, instant, staging)
            except Exception:
                failed = True
                break
            if not isinstance(item, dict) or item.get("instant") != instant:
                failed = True
                break
            taken.append(item)
        if failed or len(taken) != len(members):
            shutil.rmtree(staging, ignore_errors=True)
            add_alert(paths, "error", "groupe {} : instantané abandonné, export inchangé".format(gid))
            continue
        made.append({
            "group": gid,
            "instant": instant,
            "members": [item.get("id") for item in taken],
        })
    paths.ensure_runtime()
    _write_json(paths.run / "consistency.json", {"version": 1, "groups": made}, 0o644)
    if not made:
        return {"action": "skip", "reason": "instantané abandonné", "groups": []}
    return {"action": "snap", "groups": made}


def file_take(member, instant, staging):
    staging.mkdir(parents=True, exist_ok=True)
    dest = staging / _safe(member.get("id") or "lun")
    src = Path(member.get("path") or "")
    if src.is_file():
        shutil.copyfile(src, dest)
    else:
        dest.write_text(instant + "\n", encoding="utf-8")
    return {"id": member.get("id"), "instant": instant, "path": str(dest)}


def _group_of(volume):
    gid = (volume.get("group_id") or "").strip()
    if gid:
        return gid
    path = volume.get("path")
    if not path:
        return ""
    marker = Path(str(path) + ".group")
    try:
        if marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def _add(grouped, gid, volume):
    if not gid or volume.get("boot"):
        return
    bucket = grouped.setdefault(gid, [])
    if all(item.get("id") != volume.get("id") for item in bucket):
        bucket.append(volume)


def _safe(value):
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(value))
    return cleaned[-48:] or "groupe"


def _stamp():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
