"""Choix automatiques au boot. Aucun formulaire n'est exigé.

Un contrôle manuel ne fait qu'annuler un choix déjà posé.
Un échec de téléchargement, de conseiller ou de journal distant ne retire pas iSCSI.
"""

import re
from san_tui.identity import identity_problem
from san_tui.perf import regressed
from san_tui.redact import SECRET_KEYS, redact

MIN_DATA_BYTES = 512 * 1024 * 1024
SIGNING_GAP = (
    "La clé de signature est créée au runtime sur le disque data, hors de "
    "l'ISO. La clé privée n'entre pas dans l'image. Avant de basculer le "
    "pointeur de slot, l'appliance vérifie la signature détachée de "
    "l'empreinte. Une signature GPG san-appliance-app.tar.gz.sha256.asc "
    "n'est contrôlée que si l'opérateur a déposé une clé publique dans "
    "/etc/san/release-pubkey.asc, hors de l'ISO."
)

_PART = re.compile(r"^(?:nvme\d+n\d+p\d+|mmcblk\d+p\d+|(?:sd|vd|xvd|hd)[a-z]+\d+)$")
_VERSION = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")
_IQN = re.compile(r"iqn\.[0-9]{4}-[0-9]{2}(?:\.[a-z0-9-]+)+(?::[a-z0-9._:-]+)?", re.I)
_NQN = re.compile(r"nqn\.[a-z0-9.-]+(?::[a-z0-9._:-]+)?", re.I)


def is_partition(kernel):
    return bool(_PART.match(kernel or ""))


def format_refusal(volume):
    """None si ce disque entier, vierge et assez grand, peut devenir un support."""
    if not volume:
        return "volume absent"
    if volume.get("boot"):
        return "disque de boot refusé"
    if volume.get("mounts") or volume.get("holders") or volume.get("active"):
        return "disque occupé"
    if is_partition(volume.get("kernel") or ""):
        return "partition refusée"
    if volume.get("signature") not in ("", None, "blank"):
        if volume.get("signature"):
            return "disque non vierge"
    if volume.get("signature") is None:
        return "signature inconnue"
    size = volume.get("size") or 0
    if size < MIN_DATA_BYTES:
        return "disque trop petit pour un support de données"
    if not (volume.get("wwn") or volume.get("serial")):
        return "WWN et série absents"
    if not str(volume.get("id") or "").startswith("/dev/disk/by-id/"):
        return "identifiant stable manquant"
    return None


def export_refusal(volume):
    """None si le disque peut être exporté. Il n'a pas besoin d'être vierge."""
    if not volume:
        return "volume absent"
    if volume.get("boot"):
        return "disque de boot refusé"
    if volume.get("mounts") or volume.get("holders") or volume.get("active"):
        return "disque occupé"
    if is_partition(volume.get("kernel") or ""):
        return "partition refusée"
    size = volume.get("size") or 0
    if size < MIN_DATA_BYTES:
        return "disque trop petit pour un support de données"
    if not (volume.get("wwn") or volume.get("serial")):
        return "WWN et série absents"
    if not str(volume.get("id") or "").startswith("/dev/disk/by-id/"):
        return "identifiant stable manquant"
    return None


def pick_data_disk(volumes):
    """Le plus grand disque vierge qui n'est pas le disque de boot. Pas de menu."""
    chosen = None
    for volume in volumes or []:
        if format_refusal(volume):
            continue
        if chosen is None or (volume.get("size") or 0) > (chosen.get("size") or 0):
            chosen = volume
    if chosen is None:
        return None, "aucun disque de données libre"
    return chosen, None


def persistence_stamp(volume):
    return {
        "version": 2,
        "conf": {
            "id": volume["id"],
            "wwn": volume.get("wwn") or "",
            "serial": volume.get("serial") or "",
            "size": volume.get("size"),
            "block_size": volume.get("block_size"),
        },
        "data": None,
    }


def plan_free_layout(volumes, reserved_ids=()):
    """RAID1 puis LVM, seulement sur des disques explicitement vierges."""
    reserved = set(reserved_ids or [])
    free = []
    for volume in volumes or []:
        if volume.get("id") in reserved:
            continue
        if format_refusal(volume):
            continue
        free.append(volume)
    free.sort(key=lambda item: item.get("size") or 0, reverse=True)
    used = set()
    arrays = []
    for index, left in enumerate(free):
        if left["id"] in used:
            continue
        for right in free[index + 1:]:
            if right["id"] in used:
                continue
            size = left.get("size") or 1
            if abs((left.get("size") or 0) - (right.get("size") or 0)) / size > 0.10:
                continue
            arrays.append({
                "name": "san0",
                "level": "raid1",
                "members": [left["id"], right["id"]],
            })
            used.add(left["id"])
            used.add(right["id"])
            break
        if arrays:
            break
    lvm = []
    for volume in free:
        if volume["id"] in used:
            continue
        lvm.append({"name": "sanvg", "pvs": [volume["id"]], "lvs": ["sanlv"]})
        break
    return {
        "raid": {"version": 1, "arrays": arrays},
        "lvm": {"version": 1, "groups": lvm},
    }


def choose_mtu(local_max, peer_mtu):
    """9000 seulement si les deux extrémités l'annoncent. Sinon 1500."""
    try:
        local = int(local_max)
        peer = int(peer_mtu)
    except (TypeError, ValueError):
        return 1500
    if local >= 9000 and peer >= 9000:
        return 9000
    return 1500


def choose_governor(cpufreq_present):
    if not cpufreq_present:
        return {"set": False, "reason": "cpufreq absent"}
    return {"set": True, "governor": "performance"}


def io_uring_decision(rtslib_exposes, kernel_exposes):
    if rtslib_exposes and kernel_exposes:
        return {"enabled": True, "reason": ""}
    missing = []
    if not rtslib_exposes:
        missing.append("rtslib")
    if not kernel_exposes:
        missing.append("noyau")
    return {
        "enabled": False,
        "reason": "io_uring non exposé ({})".format(", ".join(missing)),
    }


def choose_network(interfaces):
    """Première interface : admin. Les suivantes : stockage, une chacune."""
    nics = [item for item in (interfaces or []) if item.get("name") and item["name"] != "lo"]
    if len(nics) < 2:
        return None, "deux interfaces requises"
    admin = nics[0]
    storages = []
    for nic in nics[1:]:
        address = _first_address(nic)
        storages.append({
            "iface": nic["name"],
            "address": address,
            "mtu": choose_mtu(nic.get("max_mtu"), nic.get("peer_mtu")),
            "peer_mtu": nic.get("peer_mtu"),
        })
    plan = {
        "admin": {"iface": admin["name"], "address": _first_address(admin)},
        "storage": {
            "iface": storages[0]["iface"],
            "address": storages[0]["address"],
            "mtu": storages[0]["mtu"],
        },
        "storages": storages,
    }
    if storages[0].get("peer_mtu") == 9000 and storages[0]["mtu"] == 9000:
        plan["storage"]["peer_mtu"] = 9000
    return plan, None


def _first_address(nic):
    for address in nic.get("addresses") or []:
        host = address.split("/", 1)[0]
        if host.startswith("fe80") or host == "127.0.0.1":
            continue
        return address
    return ""


def assign_exports(storages, volumes):
    """Un portail par NIC de stockage, et un seul écrivain par volume."""
    pairs = []
    reasons = []
    used = set()
    for nic in storages or []:
        address = (nic.get("address") or "").split("/", 1)[0]
        if not address:
            reasons.append({"iface": nic.get("iface"), "reason": "adresse stockage absente"})
            continue
        volume = None
        for candidate in volumes or []:
            if candidate.get("id") in used:
                continue
            volume = candidate
            break
        if volume is None:
            reasons.append({
                "iface": nic.get("iface"),
                "reason": "un seul écrivain par volume",
            })
            continue
        used.add(volume["id"])
        pairs.append({
            "iface": nic.get("iface"),
            "portal": {"address": address, "port": 3260},
            "volume": volume,
        })
    return pairs, reasons


def parse_version(text):
    match = _VERSION.match((text or "").strip())
    if not match:
        return None
    return tuple(int(match.group(index) or 0) for index in (1, 2, 3))


def is_newer(current, candidate):
    left = parse_version(current)
    right = parse_version(candidate)
    if left is None or right is None:
        return False
    return right > left


def session_is_writing(rows):
    """Vrai si une session iSCSI a des commandes en cours, ou si on ne peut pas le mesurer."""
    for row in rows or []:
        if not row.get("present"):
            continue
        cmds = row.get("out_cmds")
        if cmds is None:
            return True, "session iSCSI présente, écritures non mesurables"
        try:
            pending = int(cmds)
        except (TypeError, ValueError):
            return True, "session iSCSI présente, écritures non mesurables"
        if pending > 0:
            return True, "session iSCSI en écriture"
    return False, ""


def portal_problem(listeners, portals):
    """None si aucun portail n'est exigé, ou si l'un des portails configurés écoute."""
    if not portals:
        return None
    haystack = [str(item) for item in (listeners or [])]
    for address, port in portals:
        needle = "{}:{}".format(address, port)
        if any(needle in item for item in haystack):
            return None
    return "portail iSCSI absent"


def backup_matches(document, volumes):
    """None si chaque volume de la sauvegarde est encore le même disque."""
    if not isinstance(document, dict):
        return "sauvegarde illisible"
    live = {item.get("id"): item for item in (volumes or []) if item.get("id")}
    for target in document.get("targets") or []:
        expected = target.get("volume") or {}
        if not expected.get("id"):
            return "volume absent de la sauvegarde"
        reason = identity_problem(expected, live.get(expected["id"]), "volume")
        if reason:
            return reason
    for item in (document.get("nvme_subsystems") or []):
        expected = item.get("volume") or {}
        if not expected.get("id"):
            continue
        reason = identity_problem(expected, live.get(expected["id"]), "volume")
        if reason:
            return reason
    return None


def seen_names(text, kind):
    pattern = _IQN if kind == "iqn" else _NQN
    found = []
    for match in pattern.finditer(text or ""):
        name = match.group(0)
        if name not in found:
            found.append(name)
    return found


def generate_chap(draw):
    """12 à 16 caractères, sans espace. `draw` fournit les octets, les tests n'inventent pas un secret fixe."""
    raw = draw(8)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 6:
        raise ValueError("génération CHAP refusée")
    secret = raw.hex()[:16]
    if not 12 <= len(secret) <= 16 or any(ch.isspace() for ch in secret):
        raise ValueError("génération CHAP refusée")
    return secret


def signature_decision(has_signature, has_pubkey, verified):
    if not has_signature or not has_pubkey:
        return {"checked": False, "gap": SIGNING_GAP, "blocked": False}
    if not verified:
        return {"checked": True, "gap": "", "blocked": True}
    return {"checked": True, "gap": "", "blocked": False}


def release_rejected(baseline, trial):
    """Une régression de p99, mesurée, refuse la release. Sans mesure, on ne bloque pas iSCSI."""
    if trial is None:
        return False, "fio non mesuré"
    if regressed(baseline, trial):
        return True, "p99 en régression"
    return False, ""


def safe_audit(event, secrets=()):
    clean = {
        "action": redact(str((event or {}).get("action") or ""), secrets),
        "slot": redact(str((event or {}).get("slot") or ""), secrets),
        "result": redact(str((event or {}).get("result") or ""), secrets),
        "reason": redact(str((event or {}).get("reason") or ""), secrets),
    }
    for key in SECRET_KEYS:
        if key in (event or {}):
            clean["reason"] = "champ secret retiré"
    sha = str((event or {}).get("sha256") or "").lower()
    if len(sha) >= 12 and all(char in "0123456789abcdef" for char in sha):
        clean["sha256"] = sha[:12]
    else:
        clean["sha256"] = ""
    for secret in secrets:
        if secret and secret in clean["reason"]:
            clean["reason"] = clean["reason"].replace(str(secret), "[redacted]")
    return clean


def public_exports(document, alerts):
    """Vue WebUI : volume, LUN, initiateur, état, alerte. Jamais de secret."""
    rows = []
    messages = [item.get("message") or "" for item in (alerts or [])]
    for target in (document or {}).get("targets") or []:
        iqn = target.get("iqn") or ""
        acls = target.get("acls") or [{}]
        alert = ""
        state = "exporté"
        for message in messages:
            if iqn and iqn in message:
                alert = message
                state = "refusé"
                break
        volume = (target.get("volume") or {}).get("id") or ""
        if any("absent" in message and (volume in message or iqn in message) for message in messages):
            state = "refusé"
            if not alert:
                alert = "disque absent"
        rows.append({
            "volume": volume,
            "lun": target.get("lun"),
            "initiator": acls[0].get("initiator") or "",
            "state": state,
            "alert": alert,
        })
    return rows


def public_nvme(document, alerts):
    rows = []
    messages = [item.get("message") or "" for item in (alerts or [])]
    for item in (document or {}).get("subsystems") or []:
        nqn = item.get("nqn") or ""
        alert = ""
        state = "exporté"
        for message in messages:
            if nqn and nqn in message:
                alert = message
                state = "refusé"
                break
        volume = (item.get("volume") or {}).get("id") or ""
        rows.append({
            "volume": volume,
            "lun": 0,
            "initiator": item.get("host") or "",
            "state": state,
            "alert": alert,
        })
    return rows


ESOS_FIELDS = ("iqn", "lun", "wwid", "block_size", "acl")


def import_esos_iscsi(text):
    """Champs iSCSI seulement. Un fichier SCST est refusé, rien n'est ouvert en écriture."""
    raw = text or ""
    lowered = raw.lower()
    if "target_driver" in lowered or "scst" in lowered or "handler" in lowered:
        raise ValueError("import SCST refusé")
    import json
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("import iSCSI illisible") from exc
    if not isinstance(data, dict):
        raise ValueError("import iSCSI illisible")
    extra = set(data) - set(ESOS_FIELDS)
    if extra:
        raise ValueError("champ refusé")
    iqn = data.get("iqn") or ""
    acl = data.get("acl") or ""
    if not iqn.startswith("iqn.") or not acl.startswith("iqn."):
        raise ValueError("IQN refusé")
    if iqn == acl:
        raise ValueError("ACL identique à la cible")
    try:
        lun = int(data.get("lun"))
        block = int(data.get("block_size"))
    except (TypeError, ValueError) as exc:
        raise ValueError("LUN ou taille de bloc refusé") from exc
    if lun < 0 or lun > 255 or block not in (512, 4096):
        raise ValueError("LUN ou taille de bloc refusé")
    wwid = str(data.get("wwid") or "")
    if not wwid or any(ch.isspace() for ch in wwid):
        raise ValueError("WWID refusé")
    return {
        "iqn": iqn,
        "lun": lun,
        "wwid": wwid,
        "block_size": block,
        "acl": acl,
        "writable_with_peer": False,
    }
