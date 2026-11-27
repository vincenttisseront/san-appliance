"""mdadm uniquement pour les membres explicitement enregistrés."""

import os
import subprocess
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import undone
from san_tui.persist import PersistError, _read_json, _write_json
from san_tui.safety import SafetyError, assert_usable


class RaidError(Exception):
    pass


# Ko/s. Le scrub reste plus bas que la reconstruction : les exports gardent du débit.
SCRUB_CAP_KB = 20000
REBUILD_CAP_KB = 50000
KERNEL_SPEED_MAX_KB = 200000
SPARE_LEVELS = {"raid1", "raid4", "raid5", "raid6", "raid10", "1", "4", "5", "6", "10"}


def plan_assemble(document, probe):
    """Ne construit pas d'array. N'assemble que les membres listés, tous présents."""
    if not isinstance(document, dict) or document.get("version") != 1:
        raise RaidError("raid : version refusée")
    planned = []
    skipped = []
    for array in document.get("arrays") or []:
        name = str(array.get("name") or "")
        members = list(array.get("members") or [])
        if not name or "/" in name or name in {".", ".."}:
            skipped.append({"name": name or "?", "reason": "nom d'array refusé"})
            continue
        if len(members) < 2 or len(set(members)) != len(members):
            skipped.append({"name": name, "reason": "au moins deux membres distincts sont requis"})
            continue
        devices = []
        reason = None
        for member in members:
            current = probe.get_volume(member)
            if current is None:
                reason = "membre absent : {}".format(member)
                break
            short = member.split("/")[-1]
            if current.get("id") != member and not str(current.get("id", "")).endswith("/" + short):
                reason = "membre : identifiant by-id différent"
                break
            try:
                assert_usable(current)
            except SafetyError as exc:
                reason = "{} : {}".format(member, exc)
                break
            devices.append(current["id"])
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        planned.append({
            "name": name,
            "devices": devices,
            "level": array.get("level") or "",
        })
    return {"assemble": planned, "skipped": skipped}


class RecordingMd:
    def __init__(self):
        self.assembled = []
        self.scanned = False
        self.speeds = []
        self.spares = []
        self.removed = []

    def assemble(self, name, devices):
        if not devices:
            raise RaidError("aucun membre")
        self.assembled.append((name, list(devices)))

    def scan(self):
        self.scanned = True
        raise RaidError("assemblage global refusé")

    def set_speed(self, name, max_kb):
        self.speeds.append((name, int(max_kb)))

    def add_spare(self, name, device):
        self.spares.append((name, device))

    def remove_spare(self, name, device):
        self.removed.append((name, device))


def pace_cap(sync_action):
    """check et repair utilisent le plafond du scrub. Le reste, celui de la reconstruction."""
    if str(sync_action or "").strip() in {"check", "repair"}:
        return SCRUB_CAP_KB
    return REBUILD_CAP_KB


def apply_sync_cap(md_dir, max_kb):
    """Écrit sync_speed_max s'il existe. Ne crée rien sous /sys."""
    path = md_dir / "sync_speed_max"
    if not path.is_file():
        return False
    path.write_text("{}\n".format(int(max_kb)), encoding="utf-8")
    return True


def apply_global_cap(proc_file, max_kb):
    if not proc_file.is_file():
        return False
    proc_file.write_text("{}\n".format(int(max_kb)), encoding="utf-8")
    return True


def request_scrub(md_dir):
    """Pose le plafond du scrub puis demande un check. N'y touche pas si l'array est absent."""
    action = md_dir / "sync_action"
    if not action.is_file():
        return False
    apply_sync_cap(md_dir, SCRUB_CAP_KB)
    action.write_text("check\n", encoding="utf-8")
    return True


def idle_scrub(md_dir):
    """Annule le check en cours et rend le plafond de reconstruction."""
    action = md_dir / "sync_action"
    if not action.is_file():
        return False
    action.write_text("idle\n", encoding="utf-8")
    apply_sync_cap(md_dir, REBUILD_CAP_KB)
    return True


def role_of(volume, exported, members, support, examined, blank):
    """Classe un disque déjà observé. free n'est posé qu'à la fin."""
    disk_id = volume.get("id") or ""
    if volume.get("boot"):
        return "boot"
    if disk_id in exported:
        return "exported"
    if disk_id in members:
        return "member"
    if disk_id in support:
        return "support"
    if volume.get("mounts"):
        return "mounted"
    if volume.get("holders") or volume.get("active"):
        return "active"
    if examined == "foreign":
        return "foreign"
    if examined != "plain":
        return "unknown"
    if "-part" in disk_id:
        return "partition"
    if not (volume.get("wwn") or volume.get("serial")):
        return "unidentified"
    if blank is not True:
        return "occupied" if blank is False else "unknown"
    return "free"


def classify_disks(volumes, exported, members, support, examine, blank_of):
    """Classe toute la liste avant qu'un spare ne soit choisi."""
    classified = []
    exported = set(exported or [])
    members = set(members or [])
    support = set(support or [])
    for volume in volumes or []:
        disk_id = volume.get("id") or ""
        if disk_id in members or volume.get("boot") or disk_id in exported or disk_id in support:
            examined = "member" if disk_id in members else "plain"
            blank = False
        else:
            try:
                examined = examine(volume) if examine else "unknown"
            except Exception:
                examined = "unknown"
            try:
                blank = blank_of(volume) if blank_of else None
            except Exception:
                blank = None
        role = role_of(volume, exported, members, support, examined, blank)
        classified.append(dict(volume, role=role))
    return classified


def choose_spare(classified, exported, members, min_size):
    """Un seul disque, déjà classé free. boot et exported sont exclus même si le rôle ment."""
    exported = set(exported or [])
    members = set(members or [])
    candidates = []
    for disk in classified or []:
        disk_id = disk.get("id") or ""
        if disk.get("role") != "free":
            continue
        if disk.get("boot") or disk_id in exported or disk_id in members:
            continue
        size = disk.get("size")
        if not isinstance(size, int) or (isinstance(min_size, int) and size < min_size):
            continue
        candidates.append(disk)
    candidates.sort(key=lambda item: (item.get("size") or 0, item.get("id") or ""))
    return candidates[0] if candidates else None


def disk_is_blank(path, size):
    """Vrai si le début et la fin sont encore à zéro. None si le disque ne se lit pas."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        start = os.read(fd, 65536)
        if any(start):
            return False
        if not isinstance(size, int) or size <= 65536:
            return True
        os.lseek(fd, max(0, size - 65536), os.SEEK_SET)
        end = os.read(fd, 65536)
        return not any(end)
    except OSError:
        return None
    finally:
        os.close(fd)


def examine_text(returncode, text):
    """Un disque sans superbloc est plain.

    mdadm --examine --brief se tait et renvoie 0 quand il n'y a pas de
    superbloc. Sans --brief, le même disque renvoie 1 et un message.
    """
    body = (text or "").strip()
    if returncode == 0 and body:
        return "foreign"
    if returncode == 0:
        return "plain"
    if (
        "No md superblock" in (text or "")
        or "No super block" in (text or "")
        or "No superblock" in (text or "")
    ):
        return "plain"
    return "unknown"


def examine_device(volume):
    path = volume.get("path") or volume.get("id") or ""
    if not path:
        return "unknown"
    try:
        proc = subprocess.run(
            ["mdadm", "--examine", "--brief", path],
            text=True, capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = "{} {}".format(proc.stdout or "", proc.stderr or "")
    return examine_text(proc.returncode, text)


def apply_raid_comfort(paths, probe, backend, arrays, examine=None, blank_of=None):
    """Plafond de reconstruction, puis un spare s'il existe un disque libre.

    Un échec ici n'empêche pas l'array assemblé de rester disponible.
    """
    report = {"pace": [], "spare": None, "reason": ""}
    if not arrays:
        return report
    if paths.root == Path("/") and examine is None:
        examine = examine_device
    if paths.root == Path("/") and blank_of is None:
        def blank_of(volume):
            return disk_is_blank(volume.get("path") or "", volume.get("size"))
    if not undone(paths, "pace"):
        for array in arrays:
            try:
                backend.set_speed(array["name"], REBUILD_CAP_KB)
                report["pace"].append(array["name"])
            except Exception as exc:
                add_alert(paths, "error", "plafond RAID ignoré : {}".format(exc.__class__.__name__))
    else:
        report["reason"] = "plafond retiré"
    if undone(paths, "spare"):
        report["reason"] = "spare retiré"
        return report
    eligible = [array for array in arrays if str(array.get("level") or "") in SPARE_LEVELS]
    if not eligible:
        report["reason"] = "niveau sans spare"
        return report
    members = []
    for array in arrays:
        members.extend(array.get("devices") or [])
    exported = _id_set(paths, ("exports.json", "nvme.json"))
    support = _support_ids(paths)
    classified = classify_disks(
        probe.list_volumes(), exported, members, support, examine, blank_of,
    )
    sizes = []
    for array in eligible:
        for device in array.get("devices") or []:
            current = probe.get_volume(device)
            if current and isinstance(current.get("size"), int):
                sizes.append(current["size"])
    min_size = min(sizes) if sizes else 0
    chosen = choose_spare(classified, exported, members, min_size)
    if chosen is None:
        report["reason"] = "aucun disque libre"
        return report
    try:
        backend.add_spare(eligible[0]["name"], chosen["id"])
    except Exception as exc:
        add_alert(paths, "error", "spare ignoré : {}".format(exc.__class__.__name__))
        report["reason"] = "spare ignoré"
        return report
    record = {"array": eligible[0]["name"], "id": chosen["id"]}
    paths.ensure_runtime()
    _write_json(paths.run / "auto-spare.json", record, 0o644)
    try:
        paths.conf.mkdir(parents=True, exist_ok=True)
        _write_json(paths.conf / "auto-spare.json", record, 0o644)
    except OSError:
        pass
    add_alert(paths, "info", "spare ajouté : {}".format(chosen["id"]))
    report["spare"] = record
    report["reason"] = "spare ajouté"
    return report


def _id_set(paths, names):
    found = set()
    for name in names:
        try:
            document = _read_json(paths.conf / name)
        except PersistError:
            continue
        if not isinstance(document, dict):
            continue
        items = document.get("targets") or document.get("subsystems") or []
        for item in items:
            disk_id = ((item or {}).get("volume") or {}).get("id")
            if disk_id:
                found.add(disk_id)
    return found


def _support_ids(paths):
    found = set()
    for path in (paths.choice, paths.conf / "chosen.json"):
        try:
            document = _read_json(path)
        except PersistError:
            continue
        if not isinstance(document, dict):
            continue
        for role in ("conf", "data"):
            disk_id = ((document.get(role) or {}) if isinstance(document.get(role), dict) else {}).get("id")
            if disk_id:
                found.add(disk_id)
    return found
