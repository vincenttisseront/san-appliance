"""NVMe-oF TCP sur un volume choisi. Même gardes que l'iSCSI."""

from san_tui.identity import identity_problem
from san_tui.safety import SafetyError, assert_usable, check_portal


class NvmeError(Exception):
    pass


def plan_subsystem(volume, portal, nqn, host, network):
    assert_usable(volume)
    if not isinstance(nqn, str) or not nqn.startswith("nqn."):
        raise NvmeError("NQN de sous-système invalide")
    if not isinstance(host, str) or not host.startswith("nqn."):
        raise NvmeError("NQN d'hôte invalide")
    reason = check_portal(portal, network)
    if reason:
        raise NvmeError(reason)
    return {
        "nqn": nqn,
        "host": host,
        "portal": {"address": portal["address"], "port": int(portal.get("port") or 4420)},
        "volume": {
            "id": volume["id"],
            "wwn": volume.get("wwn") or "",
            "serial": volume.get("serial") or "",
            "size": volume.get("size"),
            "block_size": volume.get("block_size"),
        },
        "allow_any_host": False,
    }


def subsystem_problem(item, network, probe):
    reason = check_portal(item.get("portal") or {}, network)
    if reason:
        return reason
    if item.get("allow_any_host"):
        return "hôte quelconque refusé"
    host = item.get("host") or ""
    if not host.startswith("nqn."):
        return "ACL NVMe absente"
    current = probe.get_volume((item.get("volume") or {}).get("id"))
    reason = identity_problem(item.get("volume") or {}, current, "volume")
    if reason:
        return reason
    try:
        assert_usable(current)
    except SafetyError as exc:
        return str(exc)
    storage = (network.get("storage") or {}).get("iface")
    address = (item.get("portal") or {}).get("address")
    if not probe.interface_has_address(storage, address):
        return "réseau stockage absent : {}".format(address)
    return None


class RecordingNvme:
    def __init__(self, modules=True):
        self.modules = modules
        self.applied = []

    def modules_present(self):
        return self.modules

    def apply(self, item, probe):
        if not self.modules:
            raise NvmeError("modules NVMe-oF absents")
        current = probe.get_volume(item["volume"]["id"])
        reason = identity_problem(item["volume"], current, "volume")
        if reason:
            raise NvmeError(reason)
        if item.get("allow_any_host"):
            raise NvmeError("hôte quelconque refusé")
        self.applied.append(item["nqn"])
