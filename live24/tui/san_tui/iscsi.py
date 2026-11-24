"""iSCSI LIO : cible, portail, LUN, ACL et CHAP. Pas de partage automatique."""

import os

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, load_choice, load_exports, save_exports
from san_tui.safety import SafetyError, assert_usable, check_portal, describe_action, require_confirmation


class ExportError(Exception):
    pass


def plan_export(volume, portal, iqn, initiator, chap_user, lun=0):
    """Contrôle un export avant toute écriture LIO. Ne contacte pas le noyau."""
    assert_usable(volume)
    if not iqn.startswith("iqn."):
        raise ExportError("IQN de cible invalide")
    if not initiator.startswith("iqn."):
        raise ExportError("IQN d'initiateur invalide")
    if not chap_user or any(ch.isspace() for ch in chap_user):
        raise ExportError("utilisateur CHAP invalide")
    if not isinstance(lun, int) or lun < 0 or lun > 255:
        raise ExportError("LUN invalide")
    return {
        "volume": {
            "id": volume["id"],
            "wwn": volume.get("wwn") or "",
            "serial": volume.get("serial") or "",
            "size": volume.get("size"),
            "kind": volume.get("kind") or "block",
        },
        "portal": {"address": portal["address"], "port": int(portal.get("port") or 3260)},
        "iqn": iqn,
        "lun": lun,
        "acls": [{
            "initiator": initiator,
            "chap_user": chap_user,
            "chap_secret_ref": "{}:{}".format(iqn, initiator),
        }],
    }


def check_chap(secret):
    if not isinstance(secret, str) or any(ch.isspace() for ch in secret):
        raise ExportError("secret CHAP invalide")
    if not 12 <= len(secret) <= 16:
        raise ExportError("secret CHAP : 12 à 16 caractères")


def add_export(paths, plan, target, secret, typed):
    """Enregistre un export. Ne l'active pas : c'est le rôle de apply/restore."""
    check_chap(secret)
    require_confirmation(target["volume"]["id"], typed)
    reason = check_portal(target["portal"], plan)
    if reason:
        raise ExportError(reason)
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        if str(exc) == "conf absente":
            document, secrets = {"version": 1, "network": plan, "targets": []}, {}
        else:
            raise
    document["version"] = 1
    document["network"] = plan
    document["targets"] = [item for item in document["targets"] if item.get("iqn") != target["iqn"]]
    public = {
        "iqn": target["iqn"],
        "portal": target["portal"],
        "lun": target["lun"],
        "volume": target["volume"],
        "acls": target["acls"],
        "confirmed_id": typed,
    }
    document["targets"].append(public)
    ref = target["acls"][0]["chap_secret_ref"]
    secrets[ref] = secret
    save_exports(paths, document, secrets)
    return public


def restore(paths, probe, backend):
    """Rétablit chaque cible seulement si le réseau, le volume et l'identifiant collent.

    Une cible en échec n'est pas exportée. Les autres cibles valides peuvent l'être.
    """
    exported = []
    skipped = []
    try:
        choice = load_choice(paths)
    except PersistError as exc:
        add_alert(paths, "error", "conf invalide : {}".format(exc))
        return {"exported": [], "skipped": [{"iqn": "*", "reason": str(exc)}]}
    if not choice or not choice.get("conf"):
        add_alert(paths, "info", "aucun support de conf choisi : rien n'est exporté")
        return {"exported": [], "skipped": []}
    conf_volume = probe.get_volume(choice["conf"]["id"])
    mismatch = _identity_problem(choice["conf"], conf_volume, "support de conf")
    if mismatch:
        add_alert(paths, "error", mismatch)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": mismatch}]}
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        message = "conf invalide : {}".format(exc)
        add_alert(paths, "error", message)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": message}]}
    plan = document.get("network") or {}
    for target in document["targets"]:
        iqn = target.get("iqn") or "?"
        reason = _target_problem(target, plan, probe, secrets)
        if reason:
            add_alert(paths, "error", "{} : {}".format(iqn, reason), secrets.values())
            skipped.append({"iqn": iqn, "reason": reason})
            continue
        ref = target["acls"][0]["chap_secret_ref"]
        try:
            backend.apply(target, secrets[ref], probe)
        except Exception as exc:
            message = "échec LIO : {}".format(exc.__class__.__name__)
            add_alert(paths, "error", "{} : {}".format(iqn, message), secrets.values())
            skipped.append({"iqn": iqn, "reason": message})
            continue
        exported.append(iqn)
    return {"exported": exported, "skipped": skipped}


def _target_problem(target, plan, probe, secrets):
    portal = target.get("portal") or {}
    reason = check_portal(portal, plan)
    if reason:
        return reason
    storage_if = (plan.get("storage") or {}).get("iface")
    if not probe.interface_has_address(storage_if, portal.get("address")):
        return "réseau stockage absent : {}".format(portal.get("address"))
    volume = target.get("volume") or {}
    current = probe.get_volume(volume.get("id"))
    reason = _identity_problem(volume, current, "volume")
    if reason:
        return reason
    try:
        assert_usable(current)
    except SafetyError as exc:
        return str(exc)
    acls = target.get("acls") or []
    if len(acls) != 1:
        return "une ACL par initiateur est requise"
    ref = acls[0].get("chap_secret_ref")
    if not ref or ref not in secrets:
        return "secret CHAP absent"
    try:
        check_chap(secrets[ref])
    except ExportError as exc:
        return str(exc)
    if acls[0].get("initiator", "") == "":
        return "ACL vide"
    return None


def _identity_problem(expected, current, label):
    if current is None:
        return "{} absent : {}".format(label, expected.get("id"))
    if current.get("id") != expected.get("id"):
        return "{} : identifiant by-id différent".format(label)
    if expected.get("wwn") and current.get("wwn") != expected.get("wwn"):
        return "{} : WWN différent".format(label)
    if expected.get("serial") and current.get("serial") != expected.get("serial"):
        return "{} : série différente".format(label)
    if expected.get("size") is not None and current.get("size") != expected.get("size"):
        return "{} : taille différente".format(label)
    return None


def impact_for(volume):
    return describe_action(volume, "export")


class RecordingBackend:
    """N'appelle pas le noyau. Les tests vérifient qu'aucune cible refusée n'est appliquée."""

    def __init__(self):
        self.applied = []

    def apply(self, target, secret, probe):
        if secret is None:
            raise ExportError("secret manquant")
        self.applied.append(target["iqn"])


class RtslibBackend:
    """Applique une cible via python3-rtslib-fb. Le portail est une adresse précise."""

    def apply(self, target, secret, probe):
        try:
            from rtslib_fb import (
                BlockStorageObject,
                FabricModule,
                LUN,
                MappedLUN,
                NetworkPortal,
                NodeACL,
                TPG,
                Target,
            )
        except ImportError as exc:
            raise ExportError("python3-rtslib-fb absent") from exc
        volume = probe.get_volume(target["volume"]["id"])
        if volume is None:
            raise ExportError("volume absent")
        name = _safe_name(target["iqn"])
        storage = None
        tgt = None
        try:
            storage = BlockStorageObject(name, dev=volume["path"])
            fabric = FabricModule("iscsi")
            tgt = Target(fabric, target["iqn"], mode="create")
            tpg = TPG(tgt, 1, mode="create")
            tpg.set_attribute("generate_node_acls", 0)
            tpg.set_attribute("cache_dynamic_acls", 0)
            tpg.set_attribute("demo_mode_write_protect", 1)
            tpg.set_attribute("authentication", 1)
            try:
                tpg.set_parameter("AuthMethod", "CHAP")
            except Exception:
                # AuthMethod est un paramètre iSCSI. Son absence sur un vieux noyau
                # ne doit pas ouvrir le portail sans ACL : authentication reste à 1.
                pass
            NetworkPortal(
                tpg,
                target["portal"]["address"],
                int(target["portal"]["port"]),
                mode="create",
            )
            lun = LUN(tpg, int(target["lun"]), storage_object=storage)
            acl = target["acls"][0]
            node = NodeACL(tpg, acl["initiator"], mode="create")
            node.chap_userid = acl["chap_user"]
            node.chap_password = secret
            MappedLUN(node, int(target["lun"]), tpg_lun=lun, write_protect=False)
            tpg.enable = True
        except Exception:
            if tgt is not None:
                try:
                    tgt.delete()
                except Exception:
                    pass
            if storage is not None:
                try:
                    storage.delete()
                except Exception:
                    pass
            raise
        # Le secret ne doit pas rester dans l'environnement du processus.
        os.environ.pop("SAN_CHAP_SECRET", None)
        return name


def _safe_name(iqn):
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in iqn)
    return cleaned[-60:]
