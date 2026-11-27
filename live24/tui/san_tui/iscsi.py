"""iSCSI LIO : cible, portail, LUN, ACL et CHAP. Pas de partage automatique."""

import json
import os
import subprocess
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import undone
from san_tui.identity import identity_problem
from san_tui.link import attach_dns, lookup_name
from san_tui.mask import mask_targets
from san_tui.mode import enter_diagnostic
from san_tui.persist import PersistError, load_choice, load_exports, save_exports
from san_tui.portals import bind_address, iser_module_present, modules_dir, serving_portals
from san_tui.retire import retired_ids
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
            "block_size": volume.get("block_size"),
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
        message = "conf invalide : {}".format(exc)
        add_alert(paths, "error", message)
        enter_diagnostic(paths, message)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": message}]}
    if not choice or not choice.get("conf"):
        add_alert(paths, "info", "aucun support de conf choisi : rien n'est exporté")
        return {"exported": [], "skipped": []}
    if choice.get("version") != 2:
        message = "version de support refusée"
        add_alert(paths, "error", message)
        enter_diagnostic(paths, message)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": message}]}
    conf_volume = probe.get_volume(choice["conf"]["id"])
    mismatch = identity_problem(choice["conf"], conf_volume, "support de conf")
    if mismatch:
        add_alert(paths, "error", mismatch)
        enter_diagnostic(paths, mismatch)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": mismatch}]}
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        message = "conf invalide : {}".format(exc)
        add_alert(paths, "error", message)
        if str(exc) != "conf absente":
            enter_diagnostic(paths, message)
        return {"exported": [], "skipped": [{"iqn": "*", "reason": message}]}
    retired = retired_ids(paths)
    visible, hidden = mask_targets(document.get("targets") or [])
    for target in hidden:
        iqn = target.get("iqn") or "?"
        add_alert(paths, "error", "{} : LUN invisible, aucune ACL".format(iqn), secrets.values())
        skipped.append({"iqn": iqn, "reason": "aucune ACL"})
    plan = document.get("network") or {}
    claimed = set()
    extras = not undone(paths, "extra-portals")
    for target in visible:
        iqn = target.get("iqn") or "?"
        volume_id = (target.get("volume") or {}).get("id")
        if volume_id in retired:
            add_alert(paths, "error", "{} : disque retiré".format(iqn), secrets.values())
            skipped.append({"iqn": iqn, "reason": "disque retiré"})
            continue
        reason = _target_problem(target, plan, probe, secrets, claimed, extras, paths)
        if reason:
            add_alert(paths, "error", "{} : {}".format(iqn, reason), secrets.values())
            skipped.append({"iqn": iqn, "reason": reason})
            continue
        ref = target["acls"][0]["chap_secret_ref"]
        runtime = _runtime_target(paths, target, plan, probe, extras)
        if runtime.get("iser") == "skip":
            add_alert(paths, "info", "{} : iSER ignoré : module absent de l'image".format(iqn))
        try:
            attempt = dict(runtime)
            attempt["network"] = plan
            attempt["_secrets"] = secrets
            backend.apply(attempt, secrets[ref], probe)
        except Exception as exc:
            detail = " ".join(str(exc).split())[:180]
            message = "échec LIO : {} {}".format(exc.__class__.__name__, detail).strip()
            add_alert(paths, "error", "{} : {}".format(iqn, message), secrets.values())
            skipped.append({"iqn": iqn, "reason": message})
            continue
        for note in list(getattr(backend, "notes", []) or []):
            level = "info" if ("ignoré" in note or "conservé" in note) else "error"
            add_alert(paths, level, "{} : {}".format(iqn, note))
        if getattr(backend, "notes", None) is not None:
            backend.notes.clear()
        _remember_portals(paths, iqn, runtime.get("portals") or [])
        exported.append(iqn)
        portal = target.get("portal") or {}
        claimed.add((portal.get("address"), int(portal.get("port") or 3260)))
    return {"exported": exported, "skipped": skipped}


def _runtime_target(paths, target, plan, probe, extras):
    portals = [
        attach_dns(portal, lookup_name)
        for portal in serving_portals(target, plan, probe, extras=extras)
    ]
    runtime = dict(target)
    runtime["portals"] = portals
    if undone(paths, "iser"):
        runtime["iser"] = "off"
    elif iser_module_present(modules_dir()):
        runtime["iser"] = "on"
    else:
        runtime["iser"] = "skip"
    return runtime


def _remember_portals(paths, iqn, portals):
    path = paths.run / "portals.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current[iqn] = [
        {
            "address": item.get("address"),
            "port": item.get("port"),
            "dns": item.get("dns") or "",
        }
        for item in portals
    ]
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _target_problem(target, plan, probe, secrets, claimed, extras=True, paths=None):
    portal = target.get("portal") or {}
    reason = check_portal(portal, plan)
    if reason:
        return reason
    if not serving_portals(target, plan, probe, extras=extras):
        storage_if = (plan.get("storage") or {}).get("iface")
        try:
            from san_tui.portal import storage_reachable
            reachable = storage_reachable(plan, probe, portal.get("address"))
        except Exception:
            reachable = probe.interface_has_address(storage_if, portal.get("address"))
        if not reachable:
            return "réseau stockage absent : {}".format(portal.get("address"))
    volume = target.get("volume") or {}
    current = probe.get_volume(volume.get("id"))
    reason = identity_problem(volume, current, "volume")
    if reason:
        return reason
    try:
        assert_usable(current)
    except SafetyError as exc:
        return str(exc)
    acls = target.get("acls") or []
    if not acls:
        return "une ACL par initiateur est requise"
    for acl in acls:
        ref = acl.get("chap_secret_ref")
        if not ref or ref not in secrets:
            return "secret CHAP absent"
        try:
            check_chap(secrets[ref])
        except ExportError as exc:
            return str(exc)
        if acl.get("initiator", "") == "":
            return "ACL vide"
    if paths is not None:
        reason = _side_refusal(paths, current)
        if reason:
            return reason
    portal = target.get("portal") or {}
    key = (portal.get("address"), int(portal.get("port") or 3260))
    if key in claimed:
        return "un seul portail par interface de stockage"
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

    def __init__(self):
        self.notes = []

    def apply(self, target, secret, probe):
        self.notes = []
        _prepare_lio()
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
        reason = identity_problem(target["volume"], volume, "volume")
        if reason:
            raise ExportError(reason)
        try:
            assert_usable(volume)
        except SafetyError as exc:
            raise ExportError(str(exc)) from exc
        safe = _safe_name(target["iqn"])
        storage = None
        tgt = None
        try:
            storage = BlockStorageObject(safe, dev=_export_device(volume))
            _enable_pr(storage)
            fabric = FabricModule("iscsi")
            tgt = Target(fabric, target["iqn"], mode="create")
            tpg = TPG(tgt, 1, mode="create")
            for guard, value in portal_guards().items():
                tpg.set_attribute(guard, value)
            try:
                tpg.set_parameter("AuthMethod", "CHAP")
            except Exception:
                # AuthMethod est un paramètre iSCSI. Son absence sur un vieux noyau
                # ne doit pas ouvrir le portail sans ACL : authentication reste à 1.
                pass
            bound = []
            portals = list(target.get("portals") or [target.get("portal") or {}])
            try:
                standby = _bind_address(target, probe)
            except Exception:
                standby = None
            if standby and all(item.get("address") != standby for item in portals):
                portals.append({"address": standby, "port": (target.get("portal") or {}).get("port") or 3260})
            for portal in portals:
                try:
                    bound.append(NetworkPortal(
                        tpg,
                        bind_address(portal),
                        int(portal.get("port") or 3260),
                        mode="create",
                    ))
                except Exception as exc:
                    self.notes.append("portail ignoré : {}".format(exc.__class__.__name__))
            if not bound:
                raise ExportError(self.notes[-1] if self.notes else "aucun portail")
            lun = LUN(tpg, int(target["lun"]), storage_object=storage)
            _map_acls(tpg, target, secret, lun, int(target["lun"]))
            _apply_measured_iscsi(tpg)
            # ALUA et UNMAP sont à côté de l'export : leur échec le laisse servi.
            try:
                from san_tui.fabric import apply_export_features
                try:
                    interfaces = probe.list_interfaces()
                except Exception:
                    interfaces = []
                apply_export_features(storage, tgt, target, secret, interfaces)
            except Exception:
                pass
            tpg.enable = True
            if target.get("iser") == "on":
                try:
                    self.notes.extend(_enable_iser(bound))
                except Exception:
                    self.notes.append("iSER non activé : iSCSI TCP conservé")
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
        try:
            _map_extras(tpg, target, probe, secret)
        except Exception:
            pass
        os.environ.pop("SAN_CHAP_SECRET", None)
        return safe


def portal_guards():
    """Aucun LUN n'est visible sans ACL. Le mode démo reste fermé."""
    return {
        "generate_node_acls": 0,
        "cache_dynamic_acls": 0,
        "demo_mode_write_protect": 1,
        "authentication": 1,
    }


def _enable_iser(portals):
    """Active iSER si l'attribut existe. Un échec laisse le portail TCP."""
    proc = subprocess.run(["modprobe", "ib_isert"], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        return ["iSER non activé : iSCSI TCP conservé"]
    wrote = False
    for portal in portals:
        path = getattr(portal, "path", None)
        if not path:
            continue
        flag = Path(str(path)) / "iser"
        try:
            if flag.is_file():
                flag.write_text("1\n", encoding="utf-8")
                wrote = True
        except OSError:
            continue
    if not wrote:
        return ["iSER non activé : iSCSI TCP conservé"]
    return []


def map_lun(target, secrets, volume, lun):
    """Ajoute un LUN à une cible déjà active, sans écrire enable."""
    _prepare_lio()
    from rtslib_fb import BlockStorageObject, FabricModule, LUN, MappedLUN, NodeACL, TPG, Target
    _ = (MappedLUN, NodeACL)
    device = _export_device(volume)
    storage = BlockStorageObject(_safe_name("{}-{}".format(target.get("iqn"), lun)), dev=device)
    _enable_pr(storage)
    fabric = FabricModule("iscsi")
    tgt = Target(fabric, target["iqn"], mode="lookup")
    tpg = TPG(tgt, 1, mode="lookup")
    lun_obj = LUN(tpg, int(lun), storage_object=storage)
    stored = secrets if isinstance(secrets, dict) else {}
    _map_acls(tpg, target, stored.get((target.get("acls") or [{}])[0].get("chap_secret_ref")), lun_obj, int(lun), stored)


def _map_acls(tpg, target, secret, lun_obj, number, stored=None):
    from rtslib_fb import MappedLUN, NodeACL
    secrets = stored if isinstance(stored, dict) else {}
    inline = target.get("_secrets") if isinstance(target.get("_secrets"), dict) else {}
    secrets = {**secrets, **inline}
    for acl in target.get("acls") or []:
        node = NodeACL(tpg, acl["initiator"], mode="any")
        node.chap_userid = acl["chap_user"]
        node.chap_password = secrets.get(acl.get("chap_secret_ref"), secret)
        MappedLUN(node, number, tpg_lun=lun_obj, write_protect=False)


def _map_extras(tpg, target, probe, secret):
    from rtslib_fb import BlockStorageObject, LUN
    notes = []
    for extra in target.get("extras") or []:
        volume = extra.get("volume") or {}
        ident = volume.get("id") or "?"
        number = int(extra.get("lun") or 0)
        current = probe.get_volume(ident)
        reason = None if current else "volume absent"
        if current and not reason:
            reason = identity_problem(volume, current, "volume")
        if current and not reason:
            try:
                assert_usable(current)
            except SafetyError as exc:
                reason = str(exc)
        if current and not reason:
            reason = _side_refusal(None, current)
        if reason:
            notes.append({"volume": ident, "lun": number, "skipped": reason})
            continue
        try:
            storage = BlockStorageObject(_safe_name("{}-{}".format(target.get("iqn"), number)), dev=_export_device(current))
            _enable_pr(storage)
            lun_obj = LUN(tpg, number, storage_object=storage)
            _map_acls(tpg, target, secret, lun_obj, number)
        except Exception as exc:
            notes.append({"volume": ident, "lun": number, "skipped": exc.__class__.__name__})
            continue
        notes.append({"volume": ident, "lun": number, "exported": True})
    _write_runtime("lun-extras.json", {"luns": notes})


def _bind_address(target, probe):
    plan = target.get("network") or {}
    if not plan:
        return target["portal"]["address"]
    try:
        from san_tui.portal import _assign, bind_address, primary_down, standby_address
        storage_if = (plan.get("storage") or {}).get("iface")
        if primary_down(probe, storage_if):
            cidr = standby_address(plan, probe)
            if cidr:
                _assign(probe, plan, cidr)
        return bind_address(target, plan, probe)
    except Exception:
        return target["portal"]["address"]


def _export_device(volume):
    try:
        from san_tui.luks import exported_path
        path = exported_path(volume)
    except Exception:
        path = None
    return path or volume.get("path")


def _enable_pr(storage):
    result = {"ok": False}
    try:
        storage.set_attribute("emulate_pr", "1")
        result = {"ok": True, "value": str(storage.get_attribute("emulate_pr"))}
    except Exception as exc:
        result = {"ok": False, "reason": exc.__class__.__name__}
    try:
        storage.set_attribute("force_pr_aptpl", "1")
    except Exception:
        pass
    _write_runtime("pr.json", result)


def _side_refusal(paths, volume):
    try:
        from san_tui.thin import refusal_for as thin_refusal
        reason = thin_refusal(volume)
    except Exception:
        reason = None
    if reason:
        return reason
    if paths is None:
        try:
            from san_tui.paths import SanPaths
            paths = SanPaths()
        except Exception:
            return None
    try:
        from san_tui.luks import refusal_for as luks_refusal
        return luks_refusal(paths, volume)
    except Exception:
        return None


def _write_runtime(name, payload):
    path = Path("/run/san") / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def _prepare_lio():
    """Charge LIO avant que rtslib ne crée un répertoire configfs vide."""
    subprocess.run(["modprobe", "configfs"], check=False, capture_output=True)
    if not os.path.ismount("/sys/kernel/config"):
        subprocess.run(
            ["mount", "-t", "configfs", "none", "/sys/kernel/config"],
            check=False,
            capture_output=True,
        )
    target = Path("/sys/kernel/config/target")
    if target.is_dir() and not (target / "core").is_dir():
        try:
            target.rmdir()
        except OSError:
            pass
    subprocess.run(["modprobe", "target_core_mod"], check=False, capture_output=True)
    subprocess.run(["modprobe", "iscsi_target_mod"], check=False, capture_output=True)
    os.makedirs("/etc/rtslib-fb-target", exist_ok=True)


def _apply_measured_iscsi(tpg):
    """Le digest suit le banc. La rafale est posée plus tard, et seulement si le banc bouge."""
    try:
        change = _measured_change()
        if change.get("digest") == "crc32c":
            tpg.set_parameter("HeaderDigest", "CRC32C")
            tpg.set_parameter("DataDigest", "CRC32C")
    except Exception:
        return


def _measured_change():
    path = Path("/usr/share/san/perf-results.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if data.get("status") != "measured":
        return {}
    keep = data.get("keep") or []
    if len(keep) != 1 or not isinstance(keep[0], dict):
        return {}
    change = keep[0].get("change") or {}
    if not isinstance(change, dict) or len(change) != 1:
        return {}
    return change


def _safe_name(iqn):
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in iqn)
    return cleaned[-60:]
