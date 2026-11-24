"""Garde-fous avant toute action qui expose ou modifie un volume."""

BOOT_MOUNTS = {"/", "/boot", "/boot/efi", "/run/live", "/run/live/medium", "/cdrom"}
WILDCARD_ADDRS = {"", "0.0.0.0", "::", "::0", "*", "all"}


class SafetyError(Exception):
    """Le volume ou le réseau demandé ne peut pas être utilisé."""


def require_confirmation(stable_id, typed):
    if not stable_id or typed != stable_id:
        raise SafetyError(
            "confirmation refusée : saisir l'identifiant stable exact du volume"
        )


def refusal_reason(volume):
    """Retourne une cause, ou None si le volume peut être choisi."""
    if not volume:
        return "volume absent"
    stable = volume.get("id") or ""
    if not stable.startswith(("/dev/disk/by-id/", "by-id/")) and volume.get("kind") != "file":
        return "identifiant stable manquant (by-id, WWN ou série)"
    if volume.get("kind") != "file" and not (volume.get("wwn") or volume.get("serial")):
        return "WWN et série absents : identifiant insuffisant"
    if volume.get("boot"):
        return "disque de boot refusé"
    mounts = volume.get("mounts") or []
    if mounts:
        return "volume monté refusé ({})".format(", ".join(mounts))
    holders = volume.get("holders") or []
    if holders or volume.get("active"):
        return "volume actif refusé"
    return None


def assert_usable(volume):
    reason = refusal_reason(volume)
    if reason:
        raise SafetyError(reason)


def describe_action(volume, action):
    """Texte montré avant confirmation : disque exact, dépendances, impact."""
    mounts = volume.get("mounts") or []
    holders = volume.get("holders") or []
    if action == "export":
        impact = (
            "Les données existantes restent sur le volume. "
            "Les initiateurs de l'ACL pourront les lire et les modifier."
        )
    elif action == "format":
        impact = "Le contenu actuel du volume sera effacé par le formatage."
    else:
        impact = "Le volume sera utilisé comme support de persistance."
    return {
        "action": action,
        "id": volume.get("id"),
        "wwn": volume.get("wwn") or "",
        "serial": volume.get("serial") or "",
        "size": volume.get("size"),
        "model": volume.get("model") or "",
        "mounts": mounts,
        "holders": holders,
        "impact": impact,
    }


def check_portal(portal, plan):
    """Le portail est l'adresse stockage, jamais l'admin ni toutes les interfaces."""
    address = (portal or {}).get("address", "")
    if address in WILDCARD_ADDRS:
        return "écoute sur toutes les interfaces refusée"
    admin = _host((plan.get("admin") or {}).get("address"))
    storage = _host((plan.get("storage") or {}).get("address"))
    admin_if = (plan.get("admin") or {}).get("iface")
    storage_if = (plan.get("storage") or {}).get("iface")
    if not storage or not storage_if:
        return "réseau stockage non choisi"
    if not admin or not admin_if:
        return "réseau admin non choisi"
    if admin_if == storage_if:
        return "réseau admin et réseau stockage doivent utiliser des interfaces distinctes"
    if admin == storage:
        return "adresses admin et stockage identiques"
    if address == admin:
        return "le portail ne doit pas utiliser l'adresse d'administration"
    if address != storage:
        return "le portail doit être l'adresse du réseau stockage"
    return None


def _host(cidr):
    if not cidr:
        return ""
    return str(cidr).split("/", 1)[0].strip()
