"""mdadm uniquement pour les membres explicitement enregistrés."""

from san_tui.safety import SafetyError, assert_usable


class RaidError(Exception):
    pass


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

    def assemble(self, name, devices):
        if not devices:
            raise RaidError("aucun membre")
        self.assembled.append((name, list(devices)))

    def scan(self):
        self.scanned = True
        raise RaidError("assemblage global refusé")
