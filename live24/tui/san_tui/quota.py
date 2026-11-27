"""Quotas IOPS et débit lus du profil de volume. L'iSCSI n'en dépend pas."""

import json
import os
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json, _write_json, load_exports

GIB = 1024 ** 3


def profiles_file(paths=None):
    candidates = []
    if paths is not None:
        candidates.append(paths.root / "usr" / "share" / "san" / "volume-profiles.json")
    candidates.append(Path("/usr/share/san/volume-profiles.json"))
    candidates.append(
        Path(__file__).resolve().parents[3] / "live" / "rootfs" / "usr" / "share" / "san" / "volume-profiles.json"
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def load_profiles(paths=None):
    path = profiles_file(paths)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    profiles = data.get("profiles") if isinstance(data, dict) else None
    return profiles if isinstance(profiles, dict) else {}


def profile_name_for_size(size):
    """Petit, standard ou grand. Aucun choix saisi."""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return "small"
    if size < 8 * GIB:
        return "small"
    if size < 128 * GIB:
        return "standard"
    return "large"


def limits_for(volume, profiles):
    name = profile_name_for_size((volume or {}).get("size"))
    entry = profiles.get(name) or {}
    iops = entry.get("iops")
    bps = entry.get("throughput_bps")
    if not isinstance(iops, int) or not isinstance(bps, int) or iops < 1 or bps < 1:
        return None
    return {"profile": name, "iops": iops, "throughput_bps": bps}


def _undo(paths):
    data = _read_json(paths.conf / "undo" / "quota.json") or {}
    volumes = data.get("volumes") if isinstance(data, dict) else []
    return set(volumes or [])


def device_numbers(path):
    info = os.stat(path)
    return os.major(info.st_rdev), os.minor(info.st_rdev)


def apply_io_max(device, iops, bps, cgroup=None):
    """Écrit io.max. Lève OSError si le contrôleur refuse."""
    major, minor = device_numbers(device)
    root = Path(cgroup or os.environ.get("SAN_QUOTA_CGROUP") or "/sys/fs/cgroup/san-quota")
    root.mkdir(parents=True, exist_ok=True)
    parent = Path("/sys/fs/cgroup/cgroup.subtree_control")
    if parent.is_file() and "io" not in parent.read_text(encoding="utf-8").split():
        parent.write_text("+io\n", encoding="utf-8")
    line = "{}:{} rbps={} wbps={} riops={} wiops={}\n".format(major, minor, bps, bps, iops, iops)
    target = root / "io.max"
    # Une seule ligne : le noyau met à jour ce périphérique et conserve les autres.
    target.write_text(line, encoding="utf-8")
    readback = target.read_text(encoding="utf-8")
    _claim_iscsi_threads(root)
    return {"major": major, "minor": minor, "readback": readback.strip()}


def _claim_iscsi_threads(cgroup):
    procs = cgroup / "cgroup.procs"
    if not procs.is_file():
        return
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not comm.startswith("iscsi_"):
            continue
        try:
            procs.write_text(entry.name, encoding="utf-8")
        except OSError:
            continue


def clear_io_max(device, cgroup=None):
    major, minor = device_numbers(device)
    target = Path(cgroup or "/sys/fs/cgroup/san-quota") / "io.max"
    if not target.is_file():
        return ""
    line = "{}:{} rbps=max wbps=max riops=max wiops=max\n".format(major, minor)
    target.write_text(line, encoding="utf-8")
    return target.read_text(encoding="utf-8").strip()


def apply_quotas(paths, probe):
    """Pose le profil de chaque LUN exporté. Un échec n'enlève pas l'export."""
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        return {"applied": [], "skipped": [{"volume": "*", "reason": str(exc)}]}
    profiles = load_profiles(paths)
    if not profiles:
        add_alert(paths, "error", "profils de volume illisibles : quotas non posés")
        return {"applied": [], "skipped": [{"volume": "*", "reason": "profils illisibles"}]}
    undone = _undo(paths)
    applied = []
    skipped = []
    for volume in _volumes(document):
        ident = volume.get("id") or "?"
        if ident in undone:
            skipped.append({"volume": ident, "reason": "quota annulé"})
            continue
        current = probe.get_volume(ident) or volume
        limits = limits_for(current, profiles)
        if limits is None:
            skipped.append({"volume": ident, "reason": "profil sans limite"})
            continue
        device = (probe.get_volume(ident) or {}).get("path") or volume.get("path")
        if not device or not os.path.exists(device):
            skipped.append({"volume": ident, "reason": "volume absent pour le quota"})
            continue
        try:
            result = apply_io_max(device, limits["iops"], limits["throughput_bps"])
        except OSError as exc:
            add_alert(paths, "error", "quota non posé sur {} : {}".format(ident, exc.errno))
            skipped.append({"volume": ident, "reason": "io.max refusé errno {}".format(exc.errno)})
            continue
        applied.append({"volume": ident, **limits, "readback": result["readback"]})
    return {"applied": applied, "skipped": skipped}


def undo_quota(paths, probe, volume_id=None):
    """Retire le quota automatique. Ne change pas l'export."""
    try:
        document, _secrets = load_exports(paths)
    except PersistError as exc:
        return {"ok": False, "reason": str(exc)}
    volumes = _volumes(document)
    chosen = _pick(volumes, volume_id)
    if isinstance(chosen, str):
        return {"ok": False, "reason": chosen, "volumes": [item.get("id") for item in volumes]}
    device = (probe.get_volume(chosen.get("id")) or chosen).get("path")
    readback = ""
    if device and os.path.exists(device):
        try:
            readback = clear_io_max(device)
        except OSError as exc:
            return {"ok": False, "reason": "retrait io.max refusé : {}".format(exc.errno)}
    undone = _undo(paths)
    undone.add(chosen.get("id"))
    _write_json(paths.conf / "undo" / "quota.json", {"volumes": sorted(undone)}, 0o644)
    return {"ok": True, "volume": chosen.get("id"), "readback": readback}


def _volumes(document):
    found = []
    for target in document.get("targets") or []:
        if isinstance(target.get("volume"), dict):
            found.append(target["volume"])
        for extra in target.get("extras") or []:
            if isinstance(extra.get("volume"), dict):
                found.append(extra["volume"])
    return found


def _pick(volumes, volume_id):
    if volume_id:
        for item in volumes:
            if item.get("id") == volume_id:
                return item
        return "volume absent du quota"
    if len(volumes) == 1:
        return volumes[0]
    return "plusieurs volumes : préciser le volume"
