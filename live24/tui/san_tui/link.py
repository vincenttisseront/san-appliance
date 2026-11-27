"""MTU, agrégat et nom DNS. Un échec laisse l'adresse de stockage en place."""

import os
import re
import socket
import subprocess


JUMBO_MTU = 9000
PLAIN_MTU = 1500
# 9000 - 20 octets IPv4 - 8 octets ICMP. Le bit DF est obligatoire.
DF_PAYLOAD = JUMBO_MTU - 28
BOND_NAME = "sanstor"
LACP_WAIT_S = 3
_ZERO_MAC = {"", "00:00:00:00:00:00"}


def peer_from_route(text, iface):
    """Passerelle déjà annoncée sur l'interface. Rien à demander s'il n'y en a pas."""
    if not iface:
        return ""
    for line in (text or "").splitlines():
        parts = line.split()
        if "via" not in parts or "dev" not in parts:
            continue
        if parts[parts.index("dev") + 1] != iface:
            continue
        return parts[parts.index("via") + 1]
    return ""


def select_mtu(iface, peer, runner):
    """Essaie 9000 seulement après un paquet DF. Sinon revient à 1500."""
    if not iface:
        return {"mtu": PLAIN_MTU, "reason": "interface absente, MTU 1500"}
    if not peer:
        runner.set_mtu(iface, PLAIN_MTU)
        return {"mtu": PLAIN_MTU, "reason": "pas de pair, MTU 1500"}
    if not runner.set_mtu(iface, JUMBO_MTU):
        runner.set_mtu(iface, PLAIN_MTU)
        return {"mtu": PLAIN_MTU, "reason": "la carte refuse 9000, MTU 1500"}
    if not runner.ping_df(iface, peer, DF_PAYLOAD):
        runner.set_mtu(iface, PLAIN_MTU)
        return {"mtu": PLAIN_MTU, "reason": "paquet DF refusé, MTU 1500"}
    return {"mtu": JUMBO_MTU, "reason": "paquet DF accepté, MTU 9000"}


def lacp_partner_seen(text):
    macs = re.findall(r"Partner Mac Address:\s*([0-9a-fA-F:]{17})", text or "")
    return any(mac.lower() not in _ZERO_MAC for mac in macs)


def choose_bond_mode(partner_seen):
    """802.3ad si le commutateur a un partenaire LACP, sinon active-backup."""
    if partner_seen:
        return "802.3ad"
    return "active-backup"


def bond_groups(interfaces, plan):
    """Deux interfaces sans adresse propre s'agrègent. L'admin n'en fait pas partie.

    Une interface qui a déjà une autre adresse reste seule : c'est un portail.
    """
    admin = ((plan or {}).get("admin") or {}).get("iface")
    storage_ip = _host(((plan or {}).get("storage") or {}).get("address"))
    bond = []
    alone = []
    for iface in interfaces or []:
        name = iface.get("name") or ""
        if not name or name in {"lo", admin, BOND_NAME} or name.startswith("bond"):
            continue
        foreign = []
        for cidr in iface.get("addresses") or []:
            ip = _host(cidr)
            if ip and ip != storage_ip:
                foreign.append(ip)
        if foreign:
            alone.append(name)
        else:
            bond.append(name)
    if len(bond) < 2:
        return {"bond": [], "alone": bond + alone}
    return {"bond": bond, "alone": alone}


def establish_bond(ifaces, runner):
    """Crée sanstor. Sans partenaire LACP, le mode devient active-backup.

    Un échec détruit l'agrégat et rend la main : les adresses restent sur
    les interfaces physiques, iSCSI n'est pas arrêté.
    """
    names = list(ifaces or [])
    if len(names) < 2:
        return {"iface": "", "mode": "", "reason": "une seule interface, pas d'agrégat"}
    if not runner.module_present():
        return {"iface": "", "mode": "", "reason": "module bonding absent"}
    try:
        runner.create(BOND_NAME)
        runner.set_mode(BOND_NAME, "802.3ad")
        for name in names:
            runner.enslave(BOND_NAME, name)
        runner.up(BOND_NAME)
        runner.wait_lacp()
        mode = choose_bond_mode(lacp_partner_seen(runner.status(BOND_NAME)))
        if mode != "802.3ad":
            runner.down(BOND_NAME)
            runner.release_all(BOND_NAME)
            runner.set_mode(BOND_NAME, "active-backup")
            for name in names:
                runner.enslave(BOND_NAME, name)
            runner.up(BOND_NAME)
        reason = (
            "LACP annoncé"
            if mode == "802.3ad"
            else "le commutateur n'annonce pas LACP, active-backup"
        )
        return {"iface": BOND_NAME, "mode": mode, "reason": reason}
    except Exception as exc:
        try:
            runner.delete(BOND_NAME)
        except Exception:
            pass
        return {
            "iface": "",
            "mode": "",
            "reason": "agrégat ignoré : {}".format(exc.__class__.__name__),
        }


def attach_dns(portal, lookup):
    """Ajoute un nom s'il existe. L'adresse renvoyée reste l'IP."""
    address = str((portal or {}).get("address") or "")
    name = ""
    if lookup is not None and address:
        try:
            found = lookup(address) or ""
        except Exception:
            found = ""
        if found and found != address and not _ipv4(found):
            name = found
    copied = dict(portal or {})
    copied["address"] = address
    copied["dns"] = name
    return copied


def local_names():
    """Noms de la machine. nss-myhostname les renvoie pour toute IP locale."""
    found = {"localhost", "localhost.localdomain"}
    try:
        host = socket.gethostname().strip()
    except OSError:
        host = ""
    if host:
        found.add(host)
        found.add(host.split(".")[0])
    try:
        text = open("/etc/hostname", encoding="utf-8").read().strip()
    except OSError:
        text = ""
    if text:
        found.add(text)
        found.add(text.split(".")[0])
    return {name.lower() for name in found if name}


def name_from_hosts(ip, stdout, names):
    """Premier nom publié pour cette IP. Le nom de la machine n'est pas un DNS de portail."""
    parts = (stdout or "").split()
    if len(parts) < 2 or parts[0] != ip:
        return ""
    name = parts[1]
    if not name or name.lower() in (names or set()) or _ipv4(name):
        return ""
    return name


def lookup_name(ip):
    """Nom déjà publié. Pas de question si le DNS ne répond pas."""
    if not ip:
        return ""
    try:
        proc = subprocess.run(
            ["getent", "hosts", ip],
            text=True,
            capture_output=True,
            check=False,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return name_from_hosts(ip, proc.stdout, local_names())


def configure_links(network, runner, interfaces, bond=True, jumbo=True):
    """Pose l'admin et le stockage. Un échec de l'agrégat ou du MTU ne saute pas l'adresse."""
    admin = (network or {}).get("admin") or {}
    storage = (network or {}).get("storage") or {}
    result = {"admin": "", "storage": "", "mtu": PLAIN_MTU, "bond": "", "reasons": []}
    if admin.get("iface") and admin.get("address"):
        _address(runner, admin["iface"], admin["address"], PLAIN_MTU)
        result["admin"] = admin["iface"]
    storage_iface = storage.get("iface") or ""
    storage_addr = storage.get("address") or ""
    if not storage_iface or not storage_addr:
        result["reasons"].append("stockage incomplet")
        return result
    if bond:
        decision = establish_bond(bond_groups(interfaces, network)["bond"], runner)
        result["reasons"].append(decision["reason"])
        if decision["iface"]:
            storage_iface = decision["iface"]
            result["bond"] = decision["mode"]
    if jumbo:
        runner.link_up(storage_iface)
        runner.set_mtu(storage_iface, PLAIN_MTU)
        runner.add_addr(storage_iface, storage_addr)
        peer = runner.route_peer(storage_iface)
        chosen = select_mtu(storage_iface, peer, runner)
        result["mtu"] = chosen["mtu"]
        result["reasons"].append(chosen["reason"])
    else:
        _address(runner, storage_iface, storage_addr, PLAIN_MTU)
        result["reasons"].append("MTU 1500, choix retiré")
    result["storage"] = storage_iface
    return result


def _address(runner, iface, address, mtu):
    runner.link_up(iface)
    runner.set_mtu(iface, mtu)
    runner.add_addr(iface, address)


def _host(cidr):
    if not cidr:
        return ""
    return str(cidr).split("/", 1)[0].strip()


def _ipv4(value):
    return re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", value or "") is not None


def _bin(names):
    for name in names:
        if os.path.isabs(name) and os.path.exists(name):
            return name
        for directory in os.environ.get("PATH", "").split(":") + ["/usr/sbin", "/sbin", "/usr/bin", "/bin"]:
            candidate = os.path.join(directory, name)
            if os.path.exists(candidate):
                return candidate
    return names[-1]


class CommandRunner:
    """Commandes réelles. Chaque méthode échoue sans arrêter l'appelant."""

    def __init__(self):
        self.ip = _bin(("ip",))
        self.ping = _bin(("ping",))

    def module_present(self):
        from pathlib import Path
        release = _read("/proc/sys/kernel/osrelease").strip() or _uname()
        root = Path("/lib/modules") / release
        if root.is_dir() and any(root.rglob("bonding.ko*")):
            return True
        # built-in : pas de .ko, et modprobe ne va pas chercher hors de l'image
        return _modprobe_quiet()

    def create(self, name):
        _check([self.ip, "link", "add", name, "type", "bond"])

    def set_mode(self, name, mode):
        _check([self.ip, "link", "set", name, "type", "bond", "mode", mode])

    def enslave(self, bond, iface):
        _check([self.ip, "link", "set", iface, "down"])
        _check([self.ip, "link", "set", iface, "master", bond])

    def release_all(self, bond):
        text = self.status(bond)
        for line in text.splitlines():
            parts = line.split()
            if parts[:1] == ["Slave"] and len(parts) >= 3 and parts[1] == "Interface:":
                subprocess.run(
                    [self.ip, "link", "set", parts[2], "nomaster"],
                    check=False, capture_output=True, text=True,
                )

    def up(self, name):
        _check([self.ip, "link", "set", name, "up"])

    def down(self, name):
        _check([self.ip, "link", "set", name, "down"])

    def link_up(self, name):
        subprocess.run([self.ip, "link", "set", name, "up"], check=False, capture_output=True)

    def status(self, name):
        path = "/proc/net/bonding/{}".format(name)
        return _read(path)

    def wait_lacp(self):
        import time
        time.sleep(LACP_WAIT_S)

    def delete(self, name):
        self.down(name)
        self.release_all(name)
        subprocess.run([self.ip, "link", "del", name], check=False, capture_output=True)

    def set_mtu(self, iface, mtu):
        proc = subprocess.run(
            [self.ip, "link", "set", iface, "mtu", str(int(mtu))],
            check=False, capture_output=True, text=True,
        )
        return proc.returncode == 0

    def ping_df(self, iface, peer, size):
        proc = subprocess.run(
            [self.ping, "-M", "do", "-s", str(int(size)), "-c", "1", "-W", "2", "-I", iface, peer],
            check=False, capture_output=True, text=True,
        )
        return proc.returncode == 0

    def add_addr(self, iface, address):
        subprocess.run(
            [self.ip, "addr", "replace", address, "dev", iface],
            check=False, capture_output=True, text=True,
        )

    def route_peer(self, iface):
        proc = subprocess.run(
            [self.ip, "route", "show", "dev", iface],
            check=False, capture_output=True, text=True,
        )
        return peer_from_route(proc.stdout, iface)


def _check(args):
    proc = subprocess.run(args, check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "commande refusée"
        raise RuntimeError(detail.splitlines()[-1][:160])


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _uname():
    proc = subprocess.run(["uname", "-r"], check=False, capture_output=True, text=True)
    return (proc.stdout or "").strip()


def _modprobe_quiet():
    proc = subprocess.run(["modprobe", "bonding"], check=False, capture_output=True)
    return proc.returncode == 0
