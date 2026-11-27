"""ALUA, UNMAP/VAAI, et gel des portails.

Un échec de ces réglages ne retire pas une cible déjà créée.
Le gel mémorise les portails qui étaient ouverts et les rouvre.
"""

import os

VAAI_ATTRIBUTES = (
    ("emulate_tpu", 1),
    ("emulate_tpws", 1),
    ("emulate_caw", 1),
    ("emulate_3pc", 1),
)

ALUA_PREFERRED = 0
ALUA_STANDBY = 2


class GateError(Exception):
    pass


def choose_standby(portal, interfaces, admin=None):
    """Autre adresse IPv4 de l'interface qui porte déjà le portail préféré.

    L'adresse d'administration n'est pas un chemin de stockage.
    """
    primary = str(portal or "").split("/")[0]
    forbidden = str(admin or "").split("/")[0]
    if not primary:
        return None
    for iface in interfaces or []:
        addresses = [str(item).split("/")[0] for item in (iface.get("addresses") or [])]
        if primary not in addresses:
            continue
        for address in addresses:
            if not address or address == primary or address == forbidden:
                continue
            if ":" in address:
                continue
            return address
    return None


def apply_vaai(storage):
    """Active UNMAP, WRITE SAME, ATS et XCOPY. Une option refusée n'arrête pas les autres."""
    errors = []
    for name, value in VAAI_ATTRIBUTES:
        try:
            storage.set_attribute(name, value)
        except Exception as exc:
            errors.append("{}: {}".format(name, exc.__class__.__name__))
    return errors


def _alua_group(storage, name, tag):
    from rtslib_fb import ALUATargetPortGroup

    if tag is None:
        return ALUATargetPortGroup(storage, name)
    try:
        return ALUATargetPortGroup(storage, name, tag=tag)
    except Exception:
        return ALUATargetPortGroup(storage, name)


def _arm_group(group, state, preferred):
    group.alua_access_type = 1
    group.alua_support_active_optimized = 1
    group.alua_support_standby = 1
    # Le bit préféré passe avant l'état : un groupe preferred=1 refuse Standby.
    group.preferred = 1 if preferred else 0
    group.alua_access_state = state


def apply_alua(storage, standby_lun=None):
    """Annonce un groupe préféré et un groupe standby. Ne lève pas."""
    errors = []
    try:
        preferred = _alua_group(storage, "default_tg_pt_gp", None)
        _arm_group(preferred, ALUA_PREFERRED, True)
    except Exception as exc:
        errors.append("preferred: {}".format(exc.__class__.__name__))
    try:
        standby = _alua_group(storage, "standby", 2)
        _arm_group(standby, ALUA_STANDBY, False)
    except Exception as exc:
        errors.append("standby: {}".format(exc.__class__.__name__))
        return errors
    if standby_lun is not None:
        try:
            standby_lun.alua_tg_pt_gp_name = "standby"
        except Exception as exc:
            errors.append("map: {}".format(exc.__class__.__name__))
    return errors


def _protect_tpg(tpg):
    tpg.set_attribute("generate_node_acls", 0)
    tpg.set_attribute("cache_dynamic_acls", 0)
    tpg.set_attribute("demo_mode_write_protect", 1)
    tpg.set_attribute("authentication", 1)
    try:
        tpg.set_parameter("AuthMethod", "CHAP")
    except Exception:
        pass


def _standby_portal(tgt, storage, spec, secret, address):
    from rtslib_fb import LUN, MappedLUN, NetworkPortal, NodeACL, TPG

    acl = spec["acls"][0]
    tpg = TPG(tgt, 2, mode="create")
    try:
        _protect_tpg(tpg)
        NetworkPortal(tpg, address, int(spec["portal"]["port"]), mode="create")
        lun = LUN(tpg, int(spec["lun"]), storage_object=storage)
        node = NodeACL(tpg, acl["initiator"], mode="create")
        node.chap_userid = acl["chap_user"]
        node.chap_password = secret
        MappedLUN(node, int(spec["lun"]), tpg_lun=lun, write_protect=False)
        tpg.enable = True
    except Exception:
        try:
            tpg.delete()
        except Exception:
            pass
        raise
    return lun


def apply_export_features(storage, tgt, spec, secret, interfaces, admin=None):
    """Réglages à côté du portail préféré. Ne retire pas la cible."""
    errors = []
    try:
        errors.extend(apply_vaai(storage))
    except Exception as exc:
        errors.append("vaai: {}".format(exc.__class__.__name__))
    standby_lun = None
    try:
        extra = choose_standby((spec.get("portal") or {}).get("address"), interfaces, admin)
        if extra:
            standby_lun = _standby_portal(tgt, storage, spec, secret, extra)
    except Exception as exc:
        errors.append("portail: {}".format(exc.__class__.__name__))
    try:
        errors.extend(apply_alua(storage, standby_lun))
    except Exception as exc:
        errors.append("alua: {}".format(exc.__class__.__name__))
    _note(errors, secret)
    return errors


def _note(errors, secret):
    if not errors:
        return
    try:
        from san_tui.alerts import add_alert
        from san_tui.paths import SanPaths

        add_alert(
            SanPaths(os.environ.get("SAN_ROOT")),
            "error",
            "fonction secondaire : " + "; ".join(errors),
            (secret,) if secret else (),
        )
    except Exception:
        return


class NullGate:
    def freeze(self):
        return None

    def resume(self):
        return None


class RecordingGate:
    def __init__(self, fail_freeze=False):
        self.events = []
        self.fail_freeze = fail_freeze

    def freeze(self):
        self.events.append("freeze")
        if self.fail_freeze:
            raise GateError("gel refusé")

    def resume(self):
        self.events.append("resume")


class ConfigfsGate:
    """Ferme les portails ouverts, puis les rouvre."""

    def __init__(self):
        self._open = []

    def freeze(self):
        try:
            tpgs = list(_iter_tpgs())
        except Exception as exc:
            raise GateError("configfs illisible") from exc
        opened = []
        try:
            for tpg in tpgs:
                if not tpg.enable:
                    continue
                tpg.enable = False
                opened.append(tpg)
        except Exception as exc:
            for tpg in opened:
                try:
                    tpg.enable = True
                except Exception:
                    pass
            raise GateError("gel incomplet") from exc
        self._open = opened

    def resume(self):
        for tpg in list(self._open):
            try:
                tpg.enable = True
            except Exception:
                continue
        self._open = []


def _iter_tpgs():
    from rtslib_fb import RTSRoot

    root = RTSRoot()
    for target in root.targets:
        for tpg in target.tpgs:
            yield tpg
