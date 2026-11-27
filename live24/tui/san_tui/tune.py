"""Réglages iSCSI automatiques.

Aucun formulaire. Une commande ne fait qu'annuler un choix déjà posé.
Une étape qui échoue est ignorée : la cible déjà ouverte reste ouverte.
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import mark_undone, undone
from san_tui.evolve import choose_governor, io_uring_decision
from san_tui.perf import summary_p99
from san_tui.queues import storage_cores


class BackstoreSkipped(Exception):
    """Fichier non exporté. Une cible déjà servie n'est pas retirée."""


def iscsi_queues(cores):
    """Le nombre de files iSCSI est le nombre de cœurs de stockage."""
    try:
        count = int(cores)
    except (TypeError, ValueError):
        count = 1
    if count < 1:
        count = 1
    return count


def io_uring_plan(names, config_text, undone_flag=False, note=""):
    """io_uring seulement si rtslib et le noyau de l'image l'exposent."""
    if undone_flag:
        return {"enabled": False, "reason": "io_uring retiré"}
    decision = io_uring_decision(
        rtslib_exposes(names, note),
        kernel_exposes(config_text),
    )
    return decision


def rtslib_exposes(names, note=""):
    found = set(names or [])
    if "uring" in found or "io_uring" in found:
        return True
    return "expose un paramètre" in (note or "")


def kernel_exposes(config_text):
    text = config_text or ""
    return "CONFIG_IO_URING=y" in text or "CONFIG_IO_URING=m" in text


def rtslib_uring_names():
    try:
        import inspect
        from rtslib_fb.tcm import FileIOStorageObject
    except Exception:
        return set()
    try:
        return set(inspect.signature(FileIOStorageObject.__init__).parameters)
    except (TypeError, ValueError):
        return set()


def image_kernel_config():
    """Config du noyau démarré, celui que l'image a installé."""
    try:
        release = os.uname().release
    except AttributeError:
        return ""
    path = Path("/boot") / "config-{}".format(release)
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def image_uring_note():
    path = Path("/usr/share/san/io-uring.txt")
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def apply_governor(paths, cpufreq_present, sysfs=None):
    """performance seulement quand cpufreq existe. Sinon rien n'est écrit."""
    if undone(paths, "governor"):
        return {"set": False, "reason": "gouverneur retiré"}
    choice = choose_governor(cpufreq_present)
    if not choice["set"]:
        return choice
    root = Path(sysfs) if sysfs else Path("/sys/devices/system/cpu")
    governors = sorted(root.glob("cpu[0-9]*/cpufreq/scaling_governor"))
    if not governors:
        return {"set": False, "reason": "gouverneur non posé"}
    for path in governors:
        available = path.parent / "scaling_available_governors"
        try:
            names = available.read_text(encoding="utf-8").split() if available.is_file() else None
        except OSError:
            names = None
        if names is not None and "performance" not in names:
            return {"set": False, "reason": "gouverneur performance absent"}
    previous = []
    try:
        for path in governors:
            previous.append({
                "path": str(path),
                "governor": path.read_text(encoding="utf-8").strip(),
            })
            path.write_text("performance\n", encoding="utf-8")
    except OSError:
        return {"set": False, "reason": "gouverneur non posé"}
    _save_previous(paths, "governor", previous)
    return {"set": True, "governor": "performance"}


def undo_governor(paths):
    mark_undone(paths, "governor")
    restored = []
    for item in _previous(paths, "governor"):
        path = Path(item.get("path") or "")
        name = item.get("governor") or ""
        if not name or not path.is_file():
            continue
        try:
            path.write_text(name + "\n", encoding="utf-8")
        except OSError:
            continue
        restored.append(name)
    add_alert(paths, "info", "gouverneur retiré")
    return {"set": False, "reason": "gouverneur retiré", "restored": restored}


def portals_for_nics(storages, port=3260):
    """Une adresse de portail par NIC de stockage. L'admin n'est pas dans cette liste."""
    found = []
    seen = set()
    for nic in storages or []:
        raw = str((nic or {}).get("address") or "")
        address = raw.split("/", 1)[0].strip()
        if not address or address in seen:
            continue
        if not _ipv4(address):
            continue
        seen.add(address)
        found.append({
            "address": address,
            "port": int(port or 3260),
            "iface": (nic or {}).get("iface") or "",
            "dns": "",
        })
    return found


def boot_portals(target, plan, probe, extras=True):
    """Portails créés au boot : un par NIC de stockage."""
    from san_tui.portals import serving_portals

    port = int(((target or {}).get("portal") or {}).get("port") or 3260)
    storages = list((plan or {}).get("storages") or [])
    if storages:
        planned = portals_for_nics(storages, port)
        if not extras:
            return planned[:1]
        if planned:
            return planned
    found = serving_portals(target, plan, probe, extras=extras)
    if found:
        return found
    single = (plan or {}).get("storage")
    if not single:
        return []
    planned = portals_for_nics([single], port)
    return planned if extras else planned[:1]


def ensure_boot_portals(configfs, portals):
    """Crée les répertoires de portail manquants. N'écrit pas enable."""
    root = Path(configfs)
    if not root.is_dir():
        return {"created": [], "reason": "configfs iSCSI absent"}
    targets = [entry for entry in root.iterdir() if entry.is_dir()]
    if not targets:
        return {"created": [], "reason": "cible absente, portails non créés"}
    created = []
    errors = []
    for iqn in targets:
        portal_dir = iqn / "tpgt_1" / "np"
        if not portal_dir.is_dir():
            errors.append(iqn.name)
            continue
        for portal in portals or []:
            name = "{}:{}".format(portal["address"], int(portal["port"]))
            dest = portal_dir / name
            if dest.exists():
                continue
            try:
                dest.mkdir()
            except OSError:
                errors.append(name)
                continue
            created.append(name)
    reason = "un portail par NIC de stockage"
    if errors and not created:
        reason = "portail non créé, iSCSI conservé"
    return {"created": created, "errors": errors, "reason": reason}


def cache_mode(flush_ok):
    """Write-back seulement si le flush du volume a réussi."""
    if flush_ok:
        return "write-back"
    return "write-through"


def flush_succeeds(path):
    if not path:
        return False
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    if stat.S_ISBLK(mode):
        proc = subprocess.run(
            ["blockdev", "--flushbufs", path],
            check=False,
            capture_output=True,
        )
        return proc.returncode == 0
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        os.fsync(fd)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def plan_backstore(kind, allocate_ok, undone_flag=False):
    """Un fichier est préalloué. Il ne grandit pas pendant une écriture."""
    if kind == "block":
        return {
            "preallocated": True,
            "grow": False,
            "skip": False,
            "kind": "block",
            "reason": "périphérique bloc, taille fixe",
        }
    if kind != "file":
        return {
            "preallocated": False,
            "grow": False,
            "skip": False,
            "kind": kind or "absent",
            "reason": "chemin traité comme bloc",
        }
    if undone_flag:
        return {
            "preallocated": False,
            "grow": True,
            "skip": False,
            "kind": "file",
            "reason": "préallocation retirée",
        }
    if not allocate_ok:
        return {
            "preallocated": False,
            "grow": False,
            "skip": True,
            "kind": "file",
            "reason": "préallocation impossible, fichier non exporté",
        }
    return {
        "preallocated": True,
        "grow": False,
        "skip": False,
        "kind": "file",
        "reason": "fichier préalloué",
    }


def prepare_backstore(paths, path, size, allocate=None):
    """Préalloue un fichier. Un disque bloc n'est pas réécrit."""
    kind, current = _kind(path)
    if kind != "file":
        return plan_backstore(kind, True, False)
    if undone(paths, "preallocate"):
        return plan_backstore("file", False, True)
    need = _need_size(size, current)
    if need <= 0:
        return plan_backstore("file", False, False)
    do = allocate or _fallocate
    try:
        do(path, need)
    except Exception:
        return plan_backstore("file", False, False)
    result = plan_backstore("file", True, False)
    result["size"] = need
    return result


def undo_preallocate(paths, files=()):
    mark_undone(paths, "preallocate")
    for raw in files or []:
        path = str(raw)
        try:
            if not stat.S_ISREG(os.stat(path).st_mode):
                continue
        except OSError:
            continue
        subprocess.run(["fallocate", "-d", path], check=False, capture_output=True)
    add_alert(paths, "info", "préallocation retirée")
    return {"reason": "préallocation retirée"}


def choose_hugepages(before_p99, after_p99, pages=16):
    """Hugepages seulement si le p99 mesuré s'améliore. Sinon zéro."""
    if not _number(before_p99) or not _number(after_p99):
        return {"enabled": False, "pages": 0, "reason": "p99 non mesuré, hugepages arrêtées"}
    if float(after_p99) < float(before_p99):
        return {"enabled": True, "pages": int(pages), "reason": "p99 amélioré"}
    return {"enabled": False, "pages": 0, "reason": "p99 non amélioré, hugepages arrêtées"}


def apply_hugepages(paths, measure, write_pages, pages=16):
    if undone(paths, "hugepages"):
        _quiet(write_pages, 0)
        return {"enabled": False, "pages": 0, "reason": "hugepages retirées"}
    if not _quiet(write_pages, 0):
        return {"enabled": False, "pages": 0, "reason": "hugepages refusées, iSCSI conservé"}
    before = _call(measure)
    if before is None:
        _quiet(write_pages, 0)
        return {"enabled": False, "pages": 0, "reason": "p99 non mesuré, hugepages arrêtées"}
    if not _quiet(write_pages, pages):
        _quiet(write_pages, 0)
        return {"enabled": False, "pages": 0, "reason": "hugepages refusées, iSCSI conservé"}
    after = _call(measure)
    choice = choose_hugepages(before, after, pages)
    if not choice["enabled"]:
        _quiet(write_pages, 0)
    choice["before_p99_us"] = before
    choice["after_p99_us"] = after
    return choice


def undo_hugepages(paths, write_pages):
    mark_undone(paths, "hugepages")
    _quiet(write_pages, 0)
    add_alert(paths, "info", "hugepages retirées")
    return {"enabled": False, "pages": 0, "reason": "hugepages retirées"}


def session_plan(cpu_percent, cores):
    """Sessions supplémentaires seulement si un LUN sature un cœur."""
    try:
        count = int(cores)
    except (TypeError, ValueError):
        count = 1
    if count < 1:
        count = 1
    if cpu_percent is None:
        return {"sessions": 1, "reason": "charge non mesurée, une session"}
    try:
        cpu = float(cpu_percent)
    except (TypeError, ValueError):
        return {"sessions": 1, "reason": "charge non mesurée, une session"}
    if cpu < 100:
        return {"sessions": 1, "reason": "le LUN ne sature pas un cœur"}
    if count < 2:
        return {"sessions": 1, "reason": "un seul cœur, pas de session supplémentaire"}
    sessions = min(count, 1 + int(cpu // 100))
    return {"sessions": sessions, "reason": "LUN saturé, sessions supplémentaires"}


def apply_sessions(paths, cpu_percent, cores, configfs):
    if undone(paths, "sessions"):
        plan = {"sessions": 1, "reason": "sessions supplémentaires retirées"}
    else:
        plan = session_plan(cpu_percent, cores)
    if not configfs:
        plan["applied"] = False
        return plan
    wrote = _write_tree(configfs, "param", "MaxConnections", plan["sessions"])
    plan["applied"] = wrote
    if plan["sessions"] > 1 and not wrote:
        plan["sessions"] = 1
        plan["applied"] = False
        plan["reason"] = "sessions supplémentaires non posées, une session, iSCSI conservé"
    return plan


def undo_sessions(paths, configfs=None):
    mark_undone(paths, "sessions")
    if configfs:
        _write_tree(configfs, "param", "MaxConnections", 1)
    add_alert(paths, "info", "sessions supplémentaires retirées")
    return {"sessions": 1, "reason": "sessions supplémentaires retirées"}


def chosen_mtu(iface, runner, allow_jumbo=True):
    """9000 seulement après un paquet DF. Sinon 1500."""
    from san_tui.link import PLAIN_MTU, select_mtu

    if not allow_jumbo:
        try:
            runner.set_mtu(iface, PLAIN_MTU)
        except Exception:
            pass
        return {"mtu": PLAIN_MTU, "reason": "MTU 1500, choix retiré"}
    try:
        peer = runner.route_peer(iface)
        return select_mtu(iface, peer, runner)
    except Exception:
        try:
            runner.set_mtu(iface, PLAIN_MTU)
        except Exception:
            pass
        return {"mtu": PLAIN_MTU, "reason": "paquet DF non mesuré, MTU 1500"}


def stored_p99(document):
    """p99 de référence de la table fio déjà enregistrée."""
    if not isinstance(document, dict):
        return None
    baseline = (document.get("trials") or {}).get("baseline")
    if isinstance(baseline, dict):
        try:
            value = summary_p99(baseline)
        except (KeyError, TypeError, ValueError):
            value = None
        if _number(value):
            return float(value)
    for item in document.get("keep") or []:
        if isinstance(item, dict) and _number(item.get("p99_us")):
            return float(item["p99_us"])
    raw = document.get("baseline_p99_us")
    if _number(raw):
        return float(raw)
    return None


def load_perf_table(paths):
    path = paths.root / "usr" / "share" / "san" / "perf-results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def release_refused(paths, measured):
    """Vrai si le p99 fio de la release est pire que la table enregistrée."""
    table = load_perf_table(paths)
    if not table:
        return False, ""
    stored = stored_p99(table)
    got = (measured or {}).get("p99_us") if isinstance(measured, dict) else None
    if stored is None or not _number(got):
        return False, ""
    if float(got) > float(stored):
        return True, "p99 pire que la table ({:g} > {:g})".format(float(got), float(stored))
    return False, ""


def write_iscsi_queues(configfs, depth):
    """Pose default_cmdsn_depth. L'absence de l'attribut laisse la cible servie."""
    root = Path(configfs)
    if not root.is_dir():
        return {"applied": False, "queues": depth, "reason": "configfs iSCSI absent, iSCSI conservé"}
    wrote = []
    missing = []
    previous = []
    targets = [entry for entry in root.iterdir() if entry.is_dir()]
    if not targets:
        return {"applied": False, "queues": depth, "reason": "aucune cible, files non posées"}
    for iqn in sorted(targets, key=lambda item: item.name):
        attr = iqn / "tpgt_1" / "attrib" / "default_cmdsn_depth"
        if not attr.is_file():
            missing.append(iqn.name)
            continue
        try:
            previous.append({"path": str(attr), "value": attr.read_text(encoding="utf-8").strip()})
            attr.write_text("{}\n".format(int(depth)), encoding="utf-8")
        except OSError:
            missing.append(iqn.name)
            continue
        wrote.append(iqn.name)
    if not wrote:
        return {
            "applied": False,
            "queues": depth,
            "reason": "attribut de files absent du noyau, iSCSI conservé",
        }
    result = {"applied": True, "queues": depth, "targets": wrote, "previous": previous}
    if missing:
        result["skipped"] = missing
    return result


def undo_queues(paths, configfs=None):
    mark_undone(paths, "queues")
    for item in _previous(paths, "queues"):
        path = Path(item.get("path") or "")
        if path.is_file() and str(item.get("value") or "").isdigit():
            try:
                path.write_text("{}\n".format(item["value"]), encoding="utf-8")
            except OSError:
                continue
    add_alert(paths, "info", "files iSCSI retirées")
    return {"applied": False, "reason": "files iSCSI retirées"}


def undo_uring(paths, core=None):
    mark_undone(paths, "io_uring")
    if core:
        for path in Path(core).glob("*/*/attrib/io_uring"):
            try:
                path.write_text("0\n", encoding="utf-8")
            except OSError:
                continue
    add_alert(paths, "info", "io_uring retiré")
    return {"enabled": False, "reason": "io_uring retiré"}


def undo_cache(paths, core=None):
    mark_undone(paths, "cache")
    if core:
        for path in Path(core).glob("*/*/attrib/emulate_write_cache"):
            try:
                path.write_text("0\n", encoding="utf-8")
            except OSError:
                continue
    add_alert(paths, "info", "cache write-through, choix retiré")
    return {"mode": "write-through", "reason": "cache write-through, choix retiré"}


def open_backstore(name, device, volume, paths):
    """Ouvre le backstore. Un fichier qui grandirait n'est pas exporté."""
    prep = prepare_backstore(paths, device, (volume or {}).get("size"))
    if prep.get("skip"):
        raise BackstoreSkipped(prep.get("reason") or "backstore non préalloué")
    decision = io_uring_plan(
        rtslib_uring_names(),
        image_kernel_config() if paths.root == Path("/") else "",
        undone(paths, "io_uring"),
        image_uring_note() if paths.root == Path("/") else "",
    )
    notes = []
    if prep.get("kind") == "file":
        storage, extra = _fileio(name, device, prep.get("size") or (volume or {}).get("size"), decision)
        if prep.get("preallocated"):
            notes.append("backstore préalloué, iSCSI conservé")
        if extra:
            notes.append(extra)
        elif not decision.get("enabled"):
            notes.append("io_uring ignoré : {}".format(decision.get("reason") or "non exposé"))
        return storage, notes
    from rtslib_fb import BlockStorageObject
    if decision.get("enabled"):
        notes.append("io_uring ignoré : backstore bloc, iSCSI conservé")
    elif decision.get("reason"):
        notes.append("io_uring ignoré : {}".format(decision["reason"]))
    return BlockStorageObject(name, dev=device), notes


def tune_target(paths, storage, tpg, device, cores=None):
    """Pose cache et files. Une erreur ici ne retire pas la cible."""
    notes = []
    try:
        notes.append(_apply_cache(paths, storage, device))
    except Exception:
        notes.append("cache ignoré, iSCSI conservé")
    try:
        notes.append(_apply_queues(paths, tpg, cores))
    except Exception:
        notes.append("files iSCSI ignorées, iSCSI conservé")
    return [note for note in notes if note]


def boot_tune(paths, cores=None, measure=None, write_pages=None, cpu_percent=None,
              configfs=None, sysfs=None, network=None, core=None,
              uring_names=None, kernel_config=None, pages=16):
    """Chaque étape est isolée. Aucune n'écrit enable."""
    live = paths.root == Path("/")
    paths.ensure_runtime()
    if cores is None:
        cores = storage_cores()
    report = {}
    steps = (
        ("queues", lambda: _step_queues(paths, live, configfs, cores)),
        ("io_uring", lambda: _step_uring(paths, live, uring_names, kernel_config)),
        ("governor", lambda: _step_governor(paths, live, sysfs)),
        ("portals", lambda: _step_portals(paths, live, configfs, network)),
        ("cache", lambda: _step_cache(paths, live, core)),
        ("backstore", lambda: {"reason": "backstore préalloué à la création, croissance refusée"}),
        ("hugepages", lambda: _step_hugepages(paths, live, measure, write_pages, pages)),
        ("sessions", lambda: _step_sessions(paths, live, cpu_percent, cores, configfs)),
        ("mtu", lambda: {"mtu": 1500, "reason": "9000 seulement après un paquet DF, sinon 1500"}),
        ("release", lambda: {"reason": "une release au p99 pire que la table n'est pas appliquée"}),
    )
    for name, step in steps:
        try:
            report[name] = step()
        except Exception as exc:
            report[name] = {"applied": False, "reason": "ignoré : {}, iSCSI conservé".format(exc.__class__.__name__)}
        reason = (report[name] or {}).get("reason")
        if reason:
            add_alert(paths, "info", reason)
    path = paths.run / "tune.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    return report


def _step_queues(paths, live, configfs, cores):
    depth = iscsi_queues(cores)
    if undone(paths, "queues"):
        return {"applied": False, "queues": depth, "reason": "files iSCSI retirées"}
    root = configfs
    if root is None and live:
        root = "/sys/kernel/config/target/iscsi"
    if not root:
        return {"applied": False, "queues": depth, "reason": "configfs iSCSI absent, iSCSI conservé"}
    result = write_iscsi_queues(root, depth)
    if result.get("previous"):
        _save_previous(paths, "queues", result["previous"])
    return result


def _step_uring(paths, live, names, config_text):
    if names is None:
        names = rtslib_uring_names() if live else set()
    if config_text is None:
        config_text = image_kernel_config() if live else ""
    note = image_uring_note() if live else ""
    decision = io_uring_plan(names, config_text, undone(paths, "io_uring"), note)
    if not decision.get("enabled"):
        decision["applied"] = False
        if decision.get("reason") and not decision["reason"].startswith("io_uring"):
            decision["reason"] = "io_uring ignoré : {}".format(decision["reason"])
        return decision
    return {
        "enabled": True,
        "applied": False,
        "reason": "io_uring exposé par rtslib et le noyau, posé à la création du fichier",
    }


def _step_governor(paths, live, sysfs):
    if sysfs is None and not live:
        return {"set": False, "reason": "cpufreq absent"}
    if sysfs is not None:
        present = any(Path(sysfs).glob("cpu[0-9]*/cpufreq"))
        return apply_governor(paths, present, sysfs=sysfs)
    present = Path("/sys/devices/system/cpu/cpu0/cpufreq").is_dir()
    return apply_governor(paths, present)


def _step_portals(paths, live, configfs, network):
    if undone(paths, "extra-portals"):
        extras = False
    else:
        extras = True
    plan = network if network is not None else _load_network(paths)
    portals = boot_portals({"portal": {"port": 3260}}, plan, _EmptyProbe(), extras=extras)
    if not portals:
        portals = portals_for_nics((plan or {}).get("storages") or [], 3260)
        if not extras:
            portals = portals[:1]
    root = configfs
    if root is None and live:
        root = "/sys/kernel/config/target/iscsi"
    if not root:
        if not portals:
            return {"created": [], "portals": [], "reason": "aucune NIC de stockage"}
        return {"created": [], "portals": portals, "reason": "un portail par NIC de stockage"}
    result = ensure_boot_portals(root, portals)
    result["portals"] = portals
    return result


def _step_cache(paths, live, core):
    root = core
    if root is None and live:
        root = "/sys/kernel/config/target/core"
    if not root or not Path(root).is_dir():
        return {"reason": "cache décidé à la création du backstore, iSCSI conservé"}
    rows = []
    for udev in Path(root).glob("*/*/udev_path"):
        device = ""
        try:
            device = udev.read_text(encoding="utf-8").strip()
        except OSError:
            device = ""
        if undone(paths, "cache"):
            mode = "write-through"
        else:
            mode = cache_mode(_call(lambda: flush_succeeds(device)) or False)
        attr = udev.parent / "attrib" / "emulate_write_cache"
        applied = False
        if attr.is_file():
            try:
                attr.write_text(("1" if mode == "write-back" else "0") + "\n", encoding="utf-8")
                applied = True
            except OSError:
                applied = False
        rows.append({"mode": mode, "applied": applied})
    if not rows:
        return {"reason": "cache décidé à la création du backstore, iSCSI conservé"}
    return {"volumes": rows, "reason": "write-back si le flush passe, sinon write-through, iSCSI conservé"}


def _step_hugepages(paths, live, measure, write_pages, pages):
    if write_pages is None:
        write_pages = _write_live_pages if live else (lambda _value: True)
    if measure is None:
        measure = _measure_p99 if live else (lambda: None)
    return apply_hugepages(paths, measure, write_pages, pages)


def _step_sessions(paths, live, cpu_percent, cores, configfs):
    if cpu_percent is None and live:
        cpu_percent = sample_lun_percent(cores)
    root = configfs
    if root is None and live:
        root = "/sys/kernel/config/target/iscsi"
    return apply_sessions(paths, cpu_percent, cores, root)


def sample_lun_percent(cores, pause=0.2):
    before = read_cpu_sample()
    if before is None:
        return None
    time.sleep(pause)
    after = read_cpu_sample()
    return percent_from_delta(before, after, cores)


def read_cpu_sample(proc="/proc"):
    root = Path(proc)
    try:
        line = (root / "stat").read_text(encoding="utf-8").splitlines()[0]
    except (OSError, IndexError):
        return None
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    try:
        total = sum(int(item) for item in parts[1:])
    except ValueError:
        return None
    lun = 0
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
            raw = (entry / "stat").read_text(encoding="utf-8")
        except OSError:
            continue
        if "iscsi" not in comm.lower():
            continue
        tail = raw.rsplit(")", 1)[-1].split()
        try:
            lun += int(tail[11]) + int(tail[12])
        except (IndexError, ValueError):
            continue
    return {"total": total, "lun": lun}


def percent_from_delta(before, after, cores):
    if not before or not after:
        return None
    try:
        delta_total = int(after["total"]) - int(before["total"])
        delta_lun = int(after["lun"]) - int(before["lun"])
        count = int(cores)
    except (KeyError, TypeError, ValueError):
        return None
    if delta_total <= 0 or count < 1:
        return None
    one = delta_total / float(count)
    if one <= 0:
        return None
    return 100.0 * delta_lun / one


def _apply_cache(paths, storage, device):
    if undone(paths, "cache"):
        storage.set_attribute("emulate_write_cache", 0)
        return "cache write-through, choix retiré, iSCSI conservé"
    mode = cache_mode(flush_succeeds(device))
    storage.set_attribute("emulate_write_cache", 1 if mode == "write-back" else 0)
    return "cache {}, iSCSI conservé".format(mode)


def _apply_queues(paths, tpg, cores):
    depth = iscsi_queues(storage_cores() if cores is None else cores)
    if undone(paths, "queues"):
        return "files iSCSI retirées"
    tpg.set_attribute("default_cmdsn_depth", depth)
    return "files iSCSI {}, iSCSI conservé".format(depth)


def _fileio(name, device, size, decision):
    import inspect
    from rtslib_fb import FileIOStorageObject

    kwargs = {"dev": device}
    if size:
        kwargs["size"] = int(size)
    names = set(inspect.signature(FileIOStorageObject.__init__).parameters)
    key = "uring" if "uring" in names else ("io_uring" if "io_uring" in names else "")
    note = ""
    if decision.get("enabled") and key:
        kwargs[key] = True
        try:
            return FileIOStorageObject(name, **kwargs), "io_uring activé, iSCSI conservé"
        except TypeError:
            kwargs.pop(key, None)
            note = "io_uring ignoré : refusé par rtslib, iSCSI conservé"
    elif decision.get("enabled"):
        note = "io_uring ignoré : non exposé (rtslib), iSCSI conservé"
    storage = FileIOStorageObject(name, **kwargs)
    return storage, note


def _fallocate(path, size):
    size = int(size)
    if hasattr(os, "posix_fallocate"):
        fd = os.open(path, os.O_RDWR)
        try:
            os.posix_fallocate(fd, 0, size)
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    proc = subprocess.run(
        ["fallocate", "-l", str(size), path],
        check=False,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise OSError("fallocate refusé")


def _kind(path):
    if not path:
        return "absent", 0
    try:
        info = os.stat(path)
    except OSError:
        return "absent", 0
    if stat.S_ISBLK(info.st_mode):
        return "block", info.st_size
    if stat.S_ISREG(info.st_mode):
        return "file", info.st_size
    return "absent", 0


def _need_size(size, current):
    try:
        need = int(size or 0)
    except (TypeError, ValueError):
        need = 0
    if need <= 0:
        need = int(current or 0)
    return need


def _write_tree(configfs, kind, name, value):
    root = Path(configfs)
    if not root.is_dir():
        return False
    wrote = False
    for iqn in root.iterdir():
        path = iqn / "tpgt_1" / kind / name
        if not path.is_file():
            continue
        try:
            path.write_text("{}\n".format(int(value)), encoding="utf-8")
        except OSError:
            continue
        wrote = True
    return wrote


def _write_live_pages(value):
    Path("/proc/sys/vm/nr_hugepages").write_text("{}\n".format(int(value)), encoding="utf-8")
    return True


def _measure_p99():
    """p99 sur un fichier temporaire. Aucun disque physique."""
    try:
        from san_tui.latency import bench_file
        metric = bench_file()
    except Exception:
        return None
    value = metric.get("p99_us") if isinstance(metric, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _load_network(paths):
    for path in (paths.conf / "exports.json", paths.etc / "network.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        network = data.get("network") or data
        if isinstance(network, dict) and (network.get("storages") or network.get("storage")):
            return network
    return {}


def _save_previous(paths, key, values):
    try:
        paths.ensure_runtime()
        path = paths.run / "tune-previous.json"
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        current[key] = values
        path.write_text(json.dumps(current, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        return


def _previous(paths, key):
    path = paths.run / "tune-previous.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    values = data.get(key) or []
    return values if isinstance(values, list) else []


def _quiet(write_pages, value):
    try:
        write_pages(value)
        return True
    except Exception:
        return False


def _call(fn):
    try:
        return fn()
    except Exception:
        return None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ipv4(value):
    parts = str(value or "").split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit():
            return False
        number = int(part)
        if number > 255:
            return False
    return True


class _EmptyProbe:
    def list_interfaces(self):
        return []
