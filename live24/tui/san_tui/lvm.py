"""LVM comme backstore, sans activer les groupes non choisis."""

from san_tui.safety import SafetyError, assert_usable


class LvmError(Exception):
    pass


def plan_activate(document, probe):
    if not isinstance(document, dict) or document.get("version") != 1:
        raise LvmError("lvm : version refusée")
    planned = []
    skipped = []
    for group in document.get("groups") or []:
        name = group.get("name") or ""
        pvs = group.get("pvs") or []
        lvs = group.get("lvs") or []
        if not name or "/" in name:
            skipped.append({"name": name or "?", "reason": "nom de groupe refusé"})
            continue
        if not pvs or not lvs:
            skipped.append({"name": name, "reason": "PV ou LV absent de la conf"})
            continue
        reason = None
        for pv in pvs:
            current = probe.get_volume(pv)
            if current is None:
                reason = "PV absent : {}".format(pv)
                break
            if current.get("id") != pv and not str(current.get("id", "")).endswith("/" + pv.split("/")[-1]):
                reason = "PV : identifiant by-id différent"
                break
            try:
                assert_usable(current)
            except SafetyError as exc:
                reason = str(exc)
                break
        if reason:
            skipped.append({"name": name, "reason": reason})
            continue
        planned.append({"name": name, "pvs": list(pvs), "lvs": list(lvs)})
    return {"activate": planned, "skipped": skipped}


class RecordingLvm:
    def __init__(self):
        self.activated = []

    def activate(self, name, pvs):
        self.activated.append((name, list(pvs)))
