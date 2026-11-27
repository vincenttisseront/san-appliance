"""Réglages bloc du chemin rapide. Un échec n'arrête pas iSCSI.

Chaque choix est pris tout seul. Une commande ne fait que le retirer.
Un seul changement est posé à la fois. S'il empire le p99, il est retiré.
"""

import json
import os
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import mark_undone, undone
from san_tui.perf import render_table
from san_tui.persist import PersistError, _read_json, _write_json

OFFLOAD_KEYS = ("tcp-segmentation-offload", "generic-receive-offload")
BURST_PARAMETERS = (("ImmediateData", "Yes"), ("FirstBurstLength", "65536"))
SNAPSHOT = "block-tune.json"


def apply_tuning(paths, probe, sysfs="/sys", proc="/proc", configfs="/sys/kernel/config", measure=None):
    """Pose les réglages automatiques. Ne lève pas : iSCSI reste servi."""
    if measure is None:
        measure = _default_measure
    report = {"iscsi": "unchanged", "steps": [], "offload": None, "table": None}
    try:
        report["table"] = write_perf_table(paths)
    except Exception as exc:
        report["steps"].append({"name": "table", "action": "skip", "reason": exc.__class__.__name__})
    try:
        from san_tui.mode import diagnostic
        if diagnostic(paths):
            report["reason"] = "diagnostic"
            return report
    except Exception:
        pass
    try:
        volumes = exported_block_volumes(paths, probe)
    except Exception as exc:
        volumes = []
        report["steps"].append({"name": "volumes", "action": "skip", "reason": exc.__class__.__name__})
    network = _network(paths)
    storage = ((network.get("storage") or {}).get("iface") or "")
    admin = ((network.get("admin") or {}).get("iface") or "")
    baseline = _measure(measure)
    current = baseline

    def run(name, key, change, revert):
        nonlocal current
        if undone(paths, key):
            report["steps"].append({"name": name, "action": "skip", "reason": "retiré"})
            return
        try:
            detail = change()
        except Exception as exc:
            _quiet(revert)
            report["steps"].append({"name": name, "action": "skip", "reason": exc.__class__.__name__})
            add_alert(paths, "error", "réglage {} ignoré : {}".format(name, exc.__class__.__name__))
            return
        if detail is not None and detail.get("action") == "skip":
            report["steps"].append({"name": name, "action": "skip", "reason": detail.get("reason") or "rien à poser"})
            return
        after = _measure(measure)
        if _worse(current, after):
            _quiet(revert)
            report["steps"].append({
                "name": name,
                "action": "rollback",
                "reason": "p99 pire",
                "before": current,
                "after": after,
            })
            add_alert(paths, "info", "réglage {} retiré : p99 pire".format(name))
            return
        if isinstance(after, (int, float)):
            current = after
        report["steps"].append({"name": name, "action": "kept", "detail": detail, "p99_us": after})

    scheduler = _Scheduler(sysfs, volumes)
    run("scheduler", "scheduler", scheduler.apply, scheduler.revert)
    ahead = _ReadAhead(sysfs, volumes)
    run("read-ahead", "read_ahead", ahead.apply, ahead.revert)
    irqs = _IrqAffinity(sysfs, proc, storage, admin)
    run("irq", "irq", irqs.apply, irqs.revert)
    depth = _QueueDepth(sysfs, volumes)
    run("nr-requests", "nr_requests", depth.apply, depth.revert)
    unmap = _Unmap(configfs)
    run("unmap", "unmap", unmap.apply, unmap.revert)
    burst = _Burst(paths, configfs)
    run("burst", "burst", burst.apply, burst.revert)
    try:
        report["offload"] = observe_offload(_read_offload(storage, sysfs))
        _remember_offload(paths, report["offload"])
    except Exception as exc:
        report["offload"] = {"advertised": {}, "changed": False, "reason": exc.__class__.__name__}
    try:
        _save_snapshot(paths, {
            "scheduler": scheduler.previous,
            "read_ahead": ahead.previous,
            "irq": irqs.previous,
            "nr_requests": depth.previous,
            "unmap": unmap.previous,
            "burst": burst.previous,
        })
    except Exception:
        pass
    report["p99_us"] = current
    report["baseline_p99_us"] = baseline
    return report


def undo(paths, sysfs="/sys", proc="/proc", configfs="/sys/kernel/config"):
    """Retire les choix automatiques. Ne pose aucun réglage nouveau."""
    for key in ("scheduler", "read_ahead", "irq", "nr_requests", "unmap", "burst"):
        mark_undone(paths, key)
    restored = []
    snapshot = _load_snapshot(paths)
    for item in snapshot.get("scheduler") or []:
        if _restore_text(item.get("path"), item.get("previous"), newline=False):
            restored.append(item["path"])
    for item in snapshot.get("read_ahead") or []:
        if _restore_text(item.get("path"), item.get("previous")):
            restored.append(item["path"])
    for item in snapshot.get("irq") or []:
        if _restore_text(item.get("path"), item.get("previous")):
            restored.append(item["path"])
    for item in snapshot.get("nr_requests") or []:
        if _restore_text(item.get("path"), item.get("previous")):
            restored.append(item["path"])
    for item in snapshot.get("unmap") or []:
        if _restore_text(item.get("path"), item.get("previous")):
            restored.append(item["path"])
    if _force_unmap_off(configfs):
        restored.append("unmap")
    for item in snapshot.get("burst") or []:
        if _restore_text(item.get("path"), item.get("previous")):
            restored.append(item["path"])
    add_alert(paths, "info", "réglages bloc retirés")
    return {"restored": restored, "iscsi": "unchanged"}


def observe_offload(features):
    """Relève TSO et GRO tels que la carte les annonce. N'écrit rien."""
    advertised = {}
    source = features if isinstance(features, dict) else {}
    for key in OFFLOAD_KEYS:
        if key in source and source[key] not in (None, ""):
            advertised[key] = str(source[key]).split()[0]
    return {"advertised": advertised, "changed": False}


def burst_parameters(paths):
    """ImmediateData et FirstBurstLength, seulement si le banc a bougé."""
    if paths is not None and undone(paths, "burst"):
        return {}
    document = _bench(paths)
    if not bench_moves(document):
        return {}
    return {name: value for name, value in BURST_PARAMETERS}


def bench_moves(document):
    """Vrai si le seul réglage gardé est la rafale et qu'un chiffre a changé."""
    if not isinstance(document, dict) or document.get("status") != "measured":
        return False
    keep = document.get("keep") or []
    if len(keep) != 1 or not isinstance(keep[0], dict):
        return False
    change = keep[0].get("change") or {}
    if not isinstance(change, dict) or len(change) != 1 or change.get("burst") != "64k":
        return False
    trials = document.get("trials") or {}
    return _workloads_moved(trials.get("baseline"), trials.get(keep[0].get("name")))


def write_perf_table(paths, document=None):
    """Écrit le tableau IOPS et p99. Aucun bouton n'est nécessaire."""
    if document is None:
        document = _bench(paths) or {"status": "absent", "keep": [], "trials": {}}
    text = render_table(document)
    from san_tui.scsilog import boards_dir

    dest = boards_dir(paths)
    payload = {
        "version": 1,
        "table": text,
        "keep": document.get("keep") or [],
        "status": document.get("status") or "absent",
    }
    (dest / "perf.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dest / "perf.txt").write_text(text, encoding="utf-8")
    os.chmod(dest / "perf.json", 0o644)
    os.chmod(dest / "perf.txt", 0o644)
    paths.ensure_runtime()
    (paths.run / "perf-results.txt").write_text(text, encoding="utf-8")
    share = paths.root / "usr" / "share" / "san"
    try:
        share.mkdir(parents=True, exist_ok=True)
        (share / "perf-results.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass
    return {"written": True, "path": str(dest / "perf.txt"), "iscsi": "unchanged"}


def exported_block_volumes(paths, probe):
    wanted = []
    try:
        document, _secrets = __import__("san_tui.persist", fromlist=["load_exports"]).load_exports(paths)
    except PersistError:
        return []
    for volume in _iter_export_volumes(document):
        ident = volume.get("id")
        current = None
        if ident and probe is not None and hasattr(probe, "get_volume"):
            try:
                current = probe.get_volume(ident)
            except Exception:
                current = None
        if current is None and probe is not None and hasattr(probe, "list_volumes"):
            try:
                for item in probe.list_volumes():
                    if item.get("id") == ident:
                        current = item
                        break
            except Exception:
                current = None
        chosen = current or volume
        if _raw_block(chosen):
            wanted.append(chosen)
    return wanted


def _raw_block(volume):
    if not isinstance(volume, dict):
        return False
    if volume.get("kind") == "file" or volume.get("file"):
        return False
    path = str(volume.get("path") or "")
    if path and not path.startswith("/dev/") and volume.get("kind") != "block":
        return False
    return bool(volume.get("kernel"))


def _workloads_moved(baseline, trial):
    base = (baseline or {}).get("workloads") if isinstance(baseline, dict) else None
    got = (trial or {}).get("workloads") if isinstance(trial, dict) else None
    if not isinstance(base, dict) or not isinstance(got, dict) or not base:
        return False
    moved = False
    for name, before in base.items():
        after = got.get(name)
        if not isinstance(before, dict) or not isinstance(after, dict):
            return False
        if before.get("p99_us") != after.get("p99_us") or before.get("iops") != after.get("iops"):
            moved = True
    return moved


class _Scheduler:
    def __init__(self, sysfs, volumes):
        self.sysfs = Path(sysfs)
        self.volumes = volumes
        self.previous = []

    def apply(self):
        written = []
        for volume in self.volumes:
            path = self.sysfs / "block" / volume["kernel"] / "queue" / "scheduler"
            if not path.is_file():
                continue
            current = path.read_text(encoding="utf-8")
            tokens = current.replace("[", " ").replace("]", " ").split()
            if "none" not in tokens:
                continue
            active = _active_scheduler(current)
            if active == "none":
                continue
            self.previous.append({"path": str(path), "previous": active})
            path.write_text("none", encoding="utf-8")
            written.append(volume["kernel"])
        if not written:
            reason = "aucun disque exporté" if not self.volumes else "scheduler none déjà en place ou indisponible"
            return {"action": "skip", "reason": reason}
        return {"action": "kept", "disks": written}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"], newline=False)


class _ReadAhead:
    def __init__(self, sysfs, volumes):
        self.sysfs = Path(sysfs)
        self.volumes = volumes
        self.previous = []

    def apply(self):
        written = []
        for volume in self.volumes:
            path = self.sysfs / "block" / volume["kernel"] / "queue" / "read_ahead_kb"
            if not path.is_file():
                continue
            current = path.read_text(encoding="utf-8").strip()
            if current == "0":
                continue
            self.previous.append({"path": str(path), "previous": current})
            path.write_text("0\n", encoding="utf-8")
            written.append(volume["kernel"])
        if not written:
            return {"action": "skip", "reason": "aucun LUN bloc brut"}
        return {"action": "kept", "disks": written}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"])


class _IrqAffinity:
    def __init__(self, sysfs, proc, storage, admin):
        self.sysfs = Path(sysfs)
        self.proc = Path(proc)
        self.storage = storage
        self.admin = admin
        self.previous = []

    def apply(self):
        irqs = irq_list(self.sysfs, self.storage)
        if irqs is None:
            return {"action": "skip", "reason": "liste d'IRQ absente"}
        if not irqs:
            return {"action": "skip", "reason": "liste d'IRQ vide"}
        cpu_count = _cpu_count(self.sysfs)
        admin_cpus = admin_cores(self.sysfs, self.proc, self.admin, cpu_count)
        chosen = [cpu for cpu in range(cpu_count) if cpu not in admin_cpus]
        if not chosen:
            return {"action": "skip", "reason": "aucun cœur hors administration"}
        mask = affinity_mask(chosen)
        written = []
        for number in irqs:
            path = self.proc / "irq" / str(number) / "smp_affinity"
            if not path.is_file():
                continue
            current = path.read_text(encoding="utf-8").strip()
            self.previous.append({"path": str(path), "previous": current})
            path.write_text(mask + "\n", encoding="utf-8")
            written.append(number)
        if not written:
            return {"action": "skip", "reason": "affinité introuvable"}
        return {"action": "kept", "irqs": written, "mask": mask, "admin_cores": sorted(admin_cpus)}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"])


class _QueueDepth:
    def __init__(self, sysfs, volumes):
        self.sysfs = Path(sysfs)
        self.volumes = volumes
        self.previous = []

    def apply(self):
        written = []
        for volume in self.volumes:
            block = self.sysfs / "block" / volume["kernel"]
            depth = measured_queue_depth(block)
            if depth is None:
                continue
            path = block / "queue" / "nr_requests"
            if not path.is_file():
                continue
            current = path.read_text(encoding="utf-8").strip()
            if current == str(depth):
                continue
            self.previous.append({"path": str(path), "previous": current})
            path.write_text("{}\n".format(depth), encoding="utf-8")
            written.append({"disk": volume["kernel"], "nr_requests": depth})
        if not written:
            return {"action": "skip", "reason": "profondeur de file non mesurée"}
        return {"action": "kept", "disks": written}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"])


class _Unmap:
    def __init__(self, configfs):
        self.configfs = Path(configfs)
        self.previous = []

    def apply(self):
        found = list((self.configfs / "target" / "core").glob("*/*/attrib/emulate_tpu"))
        if not found:
            return {"action": "skip", "reason": "UNMAP absent du noyau"}
        for path in found:
            current = path.read_text(encoding="utf-8").strip()
            self.previous.append({"path": str(path), "previous": current})
            path.write_text("1\n", encoding="utf-8")
        return {"action": "kept", "value": 1, "count": len(found)}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"])


class _Burst:
    def __init__(self, paths, configfs):
        self.paths = paths
        self.configfs = Path(configfs)
        self.previous = []

    def apply(self):
        values = burst_parameters(self.paths)
        if not values:
            return {"action": "skip", "reason": "banc immobile, défaut conservé"}
        found = []
        for name in values:
            found.extend((self.configfs / "target" / "iscsi").glob("*/tpgt_*/param/{}".format(name)))
        if not found:
            return {"action": "skip", "reason": "paramètres iSCSI absents"}
        for path in found:
            self.previous.append({"path": str(path), "previous": path.read_text(encoding="utf-8").strip()})
            path.write_text(values[path.name] + "\n", encoding="utf-8")
        return {"action": "kept", "parameters": values}

    def revert(self):
        for item in self.previous:
            _restore_text(item["path"], item["previous"])


def irq_list(sysfs, iface):
    """Numéros d'IRQ du lien de stockage, ou None si la liste n'existe pas."""
    if not iface:
        return None
    device = Path(sysfs) / "class" / "net" / iface / "device"
    listed = device / "msi_irqs"
    if listed.is_dir():
        return [int(entry.name) for entry in sorted(listed.iterdir()) if entry.name.isdigit()]
    single = device / "irq"
    if single.is_file():
        raw = single.read_text(encoding="utf-8").strip()
        if raw.isdigit():
            return [int(raw)]
        return []
    return None


def measured_queue_depth(block):
    """Profondeur annoncée par le disque. None si elle n'est pas lisible."""
    block = Path(block)
    for relative in ("device/queue_depth", "queue/hw_queue_depth"):
        path = block / relative
        if not path.is_file():
            continue
        raw = path.read_text(encoding="utf-8").strip()
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
    tags = []
    mq = block / "mq"
    if mq.is_dir():
        for path in mq.glob("*/nr_tags"):
            raw = path.read_text(encoding="utf-8").strip()
            if raw.isdigit() and int(raw) > 0:
                tags.append(int(raw))
    if tags:
        return max(tags)
    return None


def admin_cores(sysfs, proc, admin_iface, cpu_count):
    """Cœurs d'administration. Le cœur 0 sert si l'affinité admin est illisible."""
    if cpu_count <= 1:
        return set(range(cpu_count))
    irqs = irq_list(sysfs, admin_iface) or []
    found = set()
    for number in irqs:
        path = Path(proc) / "irq" / str(number) / "smp_affinity"
        if path.is_file():
            found.update(cpus_from_mask(path.read_text(encoding="utf-8"), cpu_count))
    if found and found != set(range(cpu_count)):
        return found
    return {0}


def affinity_mask(cpus):
    mask = 0
    for cpu in cpus:
        mask |= 1 << int(cpu)
    return format(mask, "x")


def cpus_from_mask(text, cpu_count):
    cleaned = str(text or "").replace(",", "").strip()
    try:
        value = int(cleaned, 16)
    except ValueError:
        return set()
    return {cpu for cpu in range(cpu_count) if value & (1 << cpu)}


def _cpu_count(sysfs):
    present = Path(sysfs) / "devices" / "system" / "cpu" / "present"
    if present.is_file():
        count = _range_count(present.read_text(encoding="utf-8").strip())
        if count:
            return count
    try:
        online = os.sched_getaffinity(0)
        if online:
            return max(online) + 1
    except (AttributeError, OSError):
        pass
    return max(1, os.cpu_count() or 1)


def _range_count(text):
    total = 0
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            if start.isdigit() and end.isdigit():
                total += int(end) - int(start) + 1
        elif part.isdigit():
            total += 1
    return total


def _active_scheduler(text):
    if "[" in text and "]" in text:
        return text.split("[", 1)[1].split("]", 1)[0]
    return text.strip()


def _default_measure():
    """p99 court du backstore. None si fio ne peut pas mesurer : le choix automatique reste."""
    try:
        from san_tui.runtime import fio_p99
    except Exception:
        return None
    try:
        return fio_p99()
    except Exception:
        return None


def _measure(measure):
    if measure is None:
        return None
    try:
        value = measure()
    except Exception:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _worse(before, after):
    return isinstance(before, (int, float)) and isinstance(after, (int, float)) and after > before


def _quiet(revert):
    try:
        revert()
    except Exception:
        return


def _restore_text(path, previous, newline=True):
    if not path or previous is None:
        return False
    target = Path(path)
    if not target.is_file():
        return False
    text = str(previous)
    if newline and not text.endswith("\n"):
        text += "\n"
    try:
        target.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def _force_unmap_off(configfs):
    root = Path(configfs) / "target" / "core"
    if not root.is_dir():
        return False
    wrote = False
    for path in root.glob("*/*/attrib/emulate_tpu"):
        try:
            path.write_text("0\n", encoding="utf-8")
            wrote = True
        except OSError:
            continue
    return wrote


def _plain_iface(name):
    if not isinstance(name, str) or not name or name.startswith("-"):
        return False
    return all(ch.isalnum() or ch in ".:_-" for ch in name)


def _read_offload(iface, sysfs):
    """Lit l'annonce de la carte. N'appelle jamais ethtool -K."""
    _ = sysfs
    if not _plain_iface(iface):
        return {}
    import subprocess
    argv = ["ethtool", "-k", iface]
    if "-K" in argv:
        return {}
    proc = subprocess.run(argv, text=True, capture_output=True, check=False)
    features = {}
    if proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            features[key.strip()] = value.strip()
        return features
    return {}


def _remember_offload(paths, observed):
    paths.ensure_runtime()
    _write_json(paths.run / "nic-offload.json", observed, 0o644)


def _bench(paths):
    if paths is None:
        return {}
    path = paths.root / "usr" / "share" / "san" / "perf-results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _network(paths):
    for candidate in (paths.conf / "exports.json", paths.etc / "network.json"):
        try:
            document = _read_json(candidate)
        except PersistError:
            continue
        if not isinstance(document, dict):
            continue
        network = document.get("network") or document
        if isinstance(network, dict) and (network.get("storage") or network.get("admin")):
            return network
    return {}


def _iter_export_volumes(document):
    for target in document.get("targets") or []:
        if isinstance(target.get("volume"), dict):
            yield target["volume"]
        for extra in target.get("extras") or []:
            if isinstance(extra.get("volume"), dict):
                yield extra["volume"]


def _save_snapshot(paths, snapshot):
    paths.ensure_runtime()
    _write_json(paths.run / SNAPSHOT, snapshot, 0o644)
    try:
        paths.conf.mkdir(parents=True, exist_ok=True)
        _write_json(paths.conf / SNAPSHOT, snapshot, 0o644)
    except OSError:
        return


def _load_snapshot(paths):
    for candidate in (paths.conf / SNAPSHOT, paths.run / SNAPSHOT):
        try:
            data = _read_json(candidate)
        except PersistError:
            continue
        if isinstance(data, dict):
            return data
    return {}
