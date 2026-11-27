"""Quota du LUN et plafond par initiateur, lus du profil déjà présent."""

from san_tui.persist import PersistError, _read_json, _write_json


def load_profiles(paths, volumes):
    found = {}
    try:
        document = _read_json(paths.conf / "volume-profile.json")
    except PersistError:
        document = None
    if isinstance(document, dict) and document.get("version") == 1:
        volumes_doc = document.get("volumes") or {}
        if isinstance(volumes_doc, dict):
            found.update(volumes_doc)
    for volume in volumes or []:
        profile = volume.get("profile")
        if isinstance(profile, dict) and volume.get("id") and volume["id"] not in found:
            found[volume["id"]] = profile
    return found


def plan_profile(profile):
    """Le plafond d'un initiateur s'ajoute au quota du LUN et ne le dépasse pas."""
    if not isinstance(profile, dict):
        return None
    quota = profile.get("quota") if isinstance(profile.get("quota"), dict) else {}
    lun_iops = _number(quota.get("iops"))
    lun_bps = _number(quota.get("bps"))
    raw = profile.get("initiators") if isinstance(profile.get("initiators"), dict) else {}
    initiators = {}
    for iqn, limits in raw.items():
        if not str(iqn).startswith("iqn.") or not isinstance(limits, dict):
            continue
        iops = _number(limits.get("iops"))
        bps = _number(limits.get("bps"))
        if lun_iops is not None and iops is not None:
            iops = min(iops, lun_iops)
        if lun_bps is not None and bps is not None:
            bps = min(bps, lun_bps)
        initiators[str(iqn)] = {"iops": iops, "bps": bps}
    if lun_iops is None and lun_bps is None and not initiators:
        return None
    return {"quota": {"iops": lun_iops, "bps": lun_bps}, "initiators": initiators}


def queue_depth(iops):
    if not iops:
        return None
    depth = int(iops) // 128
    if depth < 1:
        depth = 1
    if depth > 64:
        depth = 64
    return depth


def apply_profiles(paths, volumes, sysfs=None):
    profiles = load_profiles(paths, volumes)
    if not profiles:
        return {"action": "skip", "reason": "aucun profil"}
    applied = []
    for volume in volumes or []:
        profile = profiles.get(volume.get("id"))
        plan = plan_profile(profile)
        if plan is None:
            continue
        depth = queue_depth((plan["quota"] or {}).get("iops"))
        wrote = False
        if depth is not None and sysfs is not None and volume.get("kernel"):
            target = sysfs / "block" / volume["kernel"] / "queue" / "nr_requests"
            if target.is_file():
                target.write_text(str(depth) + "\n", encoding="utf-8")
                wrote = True
        applied.append({
            "id": volume.get("id"),
            "quota": plan["quota"],
            "initiators": plan["initiators"],
            "queue_depth": depth,
            "queue_written": wrote,
        })
    if not applied:
        return {"action": "skip", "reason": "aucun profil"}
    paths.ensure_runtime()
    _write_json(paths.run / "qos.json", {"version": 1, "volumes": applied}, 0o644)
    return {"action": "applied", "volumes": applied}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None
