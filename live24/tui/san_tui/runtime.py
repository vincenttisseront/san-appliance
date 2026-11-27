"""Actions réelles au boot. Chaque refus est une alerte, pas un export."""

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from san_tui.abupdate import UpdateError, apply_update, plan_update
from san_tui.alerts import add_alert
from san_tui.identity import identity_problem
from san_tui.iscsi import restore
from san_tui.lvm import LvmError, plan_activate
from san_tui.medium import accept_ollama_env, evaluate
from san_tui.nvme import NvmeError, subsystem_problem
from san_tui.ollama import decide, fetch_once, rollback_needed
from san_tui.perf import assert_kept_safe, render_table
from san_tui.persist import PersistError, _read_json, _write_json, load_choice, rollback_files
from san_tui.raid import RaidError, plan_assemble
from san_tui.toram import decide as toram_decide


def diagnostic(paths):
    if paths.root != Path("/"):
        flag = paths.run / "mode"
        if flag.is_file():
            return flag.read_text(encoding="utf-8").strip() == "diagnostic"
        return False
    mode = Path("/run/san-live/mode")
    return mode.is_file() and mode.read_text(encoding="utf-8").strip() == "diagnostic"


def bind_medium(paths, probe):
    """Monte SANCONF seulement si l'identifiant enregistré est celui du disque."""
    paths.ensure_runtime()
    state_path = paths.run / "medium-state.json"
    dev = _find_label("SANCONF")
    if not dev:
        _state(state_path, False, "aucun support étiqueté SANCONF")
        add_alert(paths, "info", "aucun support de conf choisi : rien n'est exporté")
        return {"ok": False, "reason": "aucun support étiqueté SANCONF"}
    device = None
    for _ in range(15):
        device = _volume_for_path(probe, dev)
        if device is not None:
            break
        time.sleep(1)
    probe_dir = paths.run / "conf-probe"
    probe_dir.mkdir(parents=True, exist_ok=True)
    if not _mount(dev, probe_dir, "ro,noatime"):
        _state(state_path, False, "montage en lecture impossible")
        add_alert(paths, "error", "support de conf illisible")
        return {"ok": False, "reason": "montage en lecture impossible"}
    stamp_path = probe_dir / "chosen.json"
    if not stamp_path.is_file():
        _umount(probe_dir)
        _state(state_path, False, "aucun support de conf choisi")
        add_alert(paths, "info", "aucun support de conf choisi : rien n'est exporté")
        return {"ok": False, "reason": "aucun support de conf choisi"}
    try:
        stamp = _read_json(stamp_path)
    except PersistError as exc:
        _umount(probe_dir)
        reason = "support de conf illisible : {}".format(exc)
        _state(state_path, False, reason)
        add_alert(paths, "error", reason)
        return {"ok": False, "reason": reason}
    reason = evaluate(stamp, device)
    if reason:
        _umount(probe_dir)
        _state(state_path, False, reason)
        add_alert(paths, "error", reason)
        return {"ok": False, "reason": reason}
    _umount(probe_dir)
    paths.conf.mkdir(parents=True, exist_ok=True)
    if not _mountpoint(paths.conf):
        if not _mount(dev, paths.conf, "noatime"):
            _state(state_path, False, "montage du support refusé")
            add_alert(paths, "error", "montage du support refusé")
            return {"ok": False, "reason": "montage du support refusé"}
    shutil.copy2(paths.conf / "chosen.json", paths.choice)
    env = paths.conf / "ollama.env"
    if env.is_file():
        text = env.read_text(encoding="utf-8")
        if accept_ollama_env(text):
            target = paths.etc / "ollama.env"
            target.write_text(text, encoding="utf-8")
            os.chmod(target, 0o600)
        else:
            add_alert(paths, "error", "ollama.env refusé : seule SAN_OLLAMA_ENDPOINT est admise")
    _state(state_path, True, "identifiant conforme")
    return {"ok": True, "id": device.get("id"), "reason": "identifiant conforme"}


def medium_ok(paths):
    path = paths.run / "medium-state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("ok"))


def assemble_raid(paths, probe, backend):
    if diagnostic(paths):
        add_alert(paths, "info", "diagnostic : pas d'assemblage RAID")
        return {"assemble": [], "skipped": [{"name": "*", "reason": "diagnostic"}]}
    if paths.root == Path("/") and not medium_ok(paths):
        add_alert(paths, "info", "RAID non assemblé : support de conf non accepté")
        return {"assemble": [], "skipped": [{"name": "*", "reason": "support de conf non accepté"}]}
    try:
        document = _read_json(paths.conf / "raid.json")
    except PersistError as exc:
        add_alert(paths, "error", "raid illisible : {}".format(exc))
        return {"assemble": [], "skipped": [{"name": "*", "reason": str(exc)}]}
    if not document:
        return {"assemble": [], "skipped": []}
    try:
        plan = plan_assemble(document, probe)
    except RaidError as exc:
        add_alert(paths, "error", str(exc))
        return {"assemble": [], "skipped": [{"name": "*", "reason": str(exc)}]}
    done = []
    for array in plan["assemble"]:
        try:
            backend.assemble(array["name"], array["devices"])
        except Exception as exc:
            add_alert(paths, "error", "RAID {} refusé : {}".format(array["name"], exc.__class__.__name__))
            plan["skipped"].append({"name": array["name"], "reason": exc.__class__.__name__})
            continue
        done.append(array["name"])
    for item in plan["skipped"]:
        add_alert(paths, "error", "RAID {} : {}".format(item["name"], item["reason"]))
    plan["assemble"] = done
    return plan


def activate_lvm(paths, probe, backend):
    if diagnostic(paths):
        add_alert(paths, "info", "diagnostic : pas d'activation LVM")
        return {"activate": [], "skipped": [{"name": "*", "reason": "diagnostic"}]}
    if paths.root == Path("/") and not medium_ok(paths):
        add_alert(paths, "info", "LVM non activé : support de conf non accepté")
        return {"activate": [], "skipped": [{"name": "*", "reason": "support de conf non accepté"}]}
    try:
        document = _read_json(paths.conf / "lvm.json")
    except PersistError as exc:
        add_alert(paths, "error", "lvm illisible : {}".format(exc))
        return {"activate": [], "skipped": [{"name": "*", "reason": str(exc)}]}
    if not document:
        return {"activate": [], "skipped": []}
    try:
        plan = plan_activate(document, probe)
    except LvmError as exc:
        add_alert(paths, "error", str(exc))
        return {"activate": [], "skipped": [{"name": "*", "reason": str(exc)}]}
    done = []
    for group in plan["activate"]:
        try:
            backend.activate(group["name"], group["pvs"])
        except Exception as exc:
            add_alert(paths, "error", "LVM {} refusé : {}".format(group["name"], exc.__class__.__name__))
            plan["skipped"].append({"name": group["name"], "reason": exc.__class__.__name__})
            continue
        done.append(group["name"])
    for item in plan["skipped"]:
        add_alert(paths, "error", "LVM {} : {}".format(item["name"], item["reason"]))
    plan["activate"] = done
    return plan


def restore_nvme(paths, probe, backend):
    if diagnostic(paths):
        add_alert(paths, "info", "diagnostic : pas d'export NVMe-oF")
        return {"exported": [], "skipped": [{"nqn": "*", "reason": "diagnostic"}]}
    if not backend.modules_present():
        add_alert(paths, "error", "NVMe-oF arrêté : modules absents de l'image")
        return {"exported": [], "skipped": [{"nqn": "*", "reason": "modules absents"}], "stopped": True}
    if paths.root == Path("/") and not medium_ok(paths):
        add_alert(paths, "info", "NVMe-oF non exporté : support de conf non accepté")
        return {"exported": [], "skipped": [{"nqn": "*", "reason": "support de conf non accepté"}]}
    try:
        document = _read_json(paths.conf / "nvme.json")
    except PersistError as exc:
        add_alert(paths, "error", "nvme illisible : {}".format(exc))
        return {"exported": [], "skipped": [{"nqn": "*", "reason": str(exc)}]}
    if not document:
        return {"exported": [], "skipped": []}
    network = document.get("network") or {}
    exported = []
    skipped = []
    for item in document.get("subsystems") or []:
        nqn = item.get("nqn") or "?"
        reason = subsystem_problem(item, network, probe)
        if reason:
            add_alert(paths, "error", "{} : {}".format(nqn, reason))
            skipped.append({"nqn": nqn, "reason": reason})
            continue
        try:
            backend.apply(item, probe)
        except NvmeError as exc:
            add_alert(paths, "error", "{} : {}".format(nqn, exc))
            skipped.append({"nqn": nqn, "reason": str(exc)})
            continue
        except Exception as exc:
            detail = " ".join(str(exc).split())[:180]
            add_alert(paths, "error", "{} : échec NVMe-oF : {}".format(nqn, detail))
            skipped.append({"nqn": nqn, "reason": detail or "échec NVMe-oF"})
            continue
        exported.append(nqn)
    return {"exported": exported, "skipped": skipped}


def apply_measured(paths, writer):
    """Applique le réglage unique embarqué. N'invente rien si le banc est vide."""
    if diagnostic(paths):
        return {"applied": None, "reason": "diagnostic"}
    table = _share(paths) / "perf-results.json"
    try:
        document = json.loads(table.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"applied": None, "reason": "banc absent"}
    if document.get("status") != "measured":
        return {"applied": None, "reason": "banc non mesuré"}
    try:
        assert_kept_safe(
            (document.get("trials") or {}).get("baseline") or {},
            document.get("trials") or {},
            document.get("keep") or [],
        )
    except ValueError as exc:
        add_alert(paths, "error", "banc refusé : {}".format(exc))
        return {"applied": None, "reason": str(exc)}
    keep = document.get("keep") or []
    if not keep:
        return {"applied": None, "reason": "aucun réglage meilleur"}
    change = keep[0]["change"]
    writer(change)
    return {"applied": change, "reason": keep[0]["name"]}


def run_ab(paths, probe):
    if diagnostic(paths):
        add_alert(paths, "info", "diagnostic : pas de mise à jour")
        return {"applied": False, "reason": "diagnostic"}
    try:
        state = _read_json(paths.conf / "ab.json")
        pending = _read_json(paths.conf / "pending-update.json")
    except PersistError as exc:
        add_alert(paths, "error", "mise à jour illisible : {}".format(exc))
        return {"applied": False, "reason": str(exc)}
    if not pending:
        return {"applied": False, "reason": "rien à appliquer"}
    volumes = {item["id"]: item for item in probe.list_volumes()}
    mode = "diagnostic" if diagnostic(paths) else "live"
    try:
        plan = plan_update(state, pending, volumes, mode)
    except UpdateError as exc:
        add_alert(paths, "error", str(exc))
        return {"applied": False, "reason": str(exc)}
    if plan is None:
        return {"applied": False, "reason": "rien à appliquer"}

    def read_image(path):
        return Path(path).read_bytes()

    def write_device(dev, payload):
        _write_block(dev, payload)

    def read_device(dev, size):
        with open(dev, "rb") as handle:
            return handle.read(size)

    def write_state(updated):
        _write_json(paths.conf / "ab.json", updated, 0o644)

    try:
        apply_update(plan, read_image, write_device, read_device, write_state, state)
    except (OSError, UpdateError) as exc:
        add_alert(paths, "error", "mise à jour non appliquée : {}".format(exc))
        return {"applied": False, "reason": str(exc)}
    return {"applied": True, "slot": plan["slot"], "data": plan["data"]}


def advise(paths, fetcher=None, measurer=None, applier=None):
    """Hors du chemin d'export. Un échec ne fait qu'une alerte."""
    env = paths.etc / "ollama.env"
    if not env.is_file():
        return {"action": "disabled", "reason": "pas de point d'accès"}
    text = env.read_text(encoding="utf-8")
    if not accept_ollama_env(text):
        add_alert(paths, "error", "ollama désactivé : fichier refusé")
        return {"action": "alert", "reason": "fichier refusé"}
    url = ""
    for line in text.splitlines():
        if line.startswith("SAN_OLLAMA_ENDPOINT="):
            url = line.split("=", 1)[1].strip()
    catalog = _catalog(paths)
    current = catalog.get("current") or "baseline"
    fetch = fetcher or (lambda: fetch_once(url, timeout=2))
    try:
        payload = fetch()
    except Exception:
        payload = None
    choice = decide(payload, catalog, current)
    if choice["action"] in {"alert", "rollback"}:
        add_alert(paths, "error", "ollama : {}".format(choice["reason"]))
        return choice
    if choice["action"] != "apply":
        return choice
    apply = applier or MeasuredWriter()
    measure = measurer or fio_p99
    before = measure()
    if rollback_needed(before, before) or before is None:
        add_alert(paths, "error", "ollama : mesure impossible, réglage non appliqué")
        return {"action": "rollback", "reason": "mesure impossible"}
    try:
        apply(choice["change"])
        after = measure()
    except Exception:
        try:
            apply({})
        except Exception:
            add_alert(paths, "error", "ollama : retour au réglage courant impossible")
        add_alert(paths, "error", "ollama : application annulée")
        return {"action": "rollback", "reason": "application annulée"}
    if rollback_needed(before, after):
        try:
            apply({})
        except Exception:
            add_alert(paths, "error", "ollama : retour au réglage courant impossible")
        add_alert(paths, "error", "ollama : p99 pire, réglage retiré")
        return {"action": "rollback", "reason": "p99 pire"}
    return choice


def rollback(paths, probe, backend):
    try:
        try:
            volumes = probe.list_volumes()
        except Exception:
            volumes = []
        rollback_files(paths, volumes)
    except PersistError as exc:
        add_alert(paths, "error", "retour arrière impossible : {}".format(exc))
        return {"exported": [], "skipped": [{"iqn": "*", "reason": str(exc)}]}
    return restore(paths, probe, backend)


def measure_toram(paths, mem_kib, squash_bytes):
    result = toram_decide(mem_kib, squash_bytes)
    paths.ensure_runtime()
    (paths.run / "toram.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


def restore_iscsi(paths, probe, backend):
    if diagnostic(paths):
        add_alert(paths, "info", "diagnostic : pas d'export iSCSI")
        return {"exported": [], "skipped": [{"iqn": "*", "reason": "diagnostic"}]}
    if paths.root == Path("/") and not medium_ok(paths):
        try:
            choice = load_choice(paths)
        except PersistError:
            choice = None
        if not choice:
            add_alert(paths, "info", "aucun support de conf choisi : rien n'est exporté")
            return {"exported": [], "skipped": []}
        add_alert(paths, "error", "iSCSI non exporté : support de conf refusé")
        return {"exported": [], "skipped": [{"iqn": "*", "reason": "support de conf refusé"}]}
    return restore(paths, probe, backend)


class MdadmBackend:
    def assemble(self, name, devices):
        for device in devices:
            if not str(device).startswith("/dev/disk/by-id/"):
                raise RaidError("membre hors by-id")
        cmd = ["mdadm", "--assemble", "/dev/md/{}".format(name), *devices, "--run"]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RaidError(proc.stderr.strip() or "mdadm a refusé l'assemblage")


class LvmBackend:
    def activate(self, name, pvs):
        accepted = []
        for pv in pvs:
            accepted.append(str(pv))
            try:
                resolved = os.path.realpath(pv)
            except OSError:
                resolved = ""
            if resolved and resolved not in accepted:
                accepted.append(resolved)
        filt = ["a|^{}$|".format(pv.replace("|", "")) for pv in accepted]
        filt.append("r|.*|")
        config = 'devices {{ filter = [ {} ] }}'.format(
            ", ".join('"{}"'.format(item) for item in filt)
        )
        proc = subprocess.run(
            ["vgchange", "-ay", name, "--config", config],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise LvmError(proc.stderr.strip() or "vgchange a refusé le groupe")


class ConfigfsNvme:
    def modules_present(self):
        return nvme_modules_present(Path("/lib/modules") / os.uname().release)

    def apply(self, item, probe):
        if not self.modules_present():
            raise NvmeError("modules NVMe-oF absents")
        current = probe.get_volume(item["volume"]["id"])
        reason = identity_problem(item["volume"], current, "volume")
        if reason:
            raise NvmeError(reason)
        if item.get("allow_any_host"):
            raise NvmeError("hôte quelconque refusé")
        _modprobe("nvmet")
        _modprobe("nvmet-tcp")
        _configfs_subsystem(item, current["id"])


class MeasuredWriter:
    """Applique un seul réglage déjà mesuré, et sait le retirer."""

    def __init__(self):
        self.previous = None

    def __call__(self, change):
        if not change:
            if self.previous:
                self._apply(self.previous)
                self.previous = None
            return
        if len(change) != 1:
            raise ValueError("un seul réglage")
        self.previous = self._snapshot(change)
        self._apply(change)

    def _snapshot(self, change):
        key = next(iter(change))
        if key == "scheduler":
            return {"scheduler": _current_scheduler()}
        if key == "governor":
            return {"governor": _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")}
        if key == "mtu":
            return {"mtu": _storage_mtu()}
        if key == "hugepages":
            return {"hugepages": _read_text("/proc/sys/vm/nr_hugepages")}
        return {key: None}

    def _apply(self, change):
        key, value = next(iter(change.items()))
        if value is None:
            return
        if key == "scheduler":
            if value not in {"none", "mq-deadline"}:
                raise ValueError("scheduler refusé")
            _write_schedulers(value)
            return
        if key == "governor":
            if value != "performance":
                raise ValueError("gouverneur refusé")
            _write_governors(value)
            return
        if key == "mtu":
            if int(value) != 9000 or not _peer_mtu_matches(9000):
                raise ValueError("mtu 9000 refusé sans l'autre extrémité")
            _set_storage_mtu(9000)
            return
        if key == "hugepages":
            Path("/proc/sys/vm/nr_hugepages").write_text(str(int(value)), encoding="utf-8")
            return
        if key == "pin":
            _pin_iscsi(value)
            return
        if key in {"digest", "burst", "write_back", "backstore", "preallocate"}:
            # Ces réglages sont posés à la création de la cible, pas en écrasant un cache.
            return
        raise ValueError("réglage inconnu")


def nvme_modules_present(modules):
    base = Path(modules)
    if not base.is_dir():
        return False
    return any(base.rglob("nvmet.ko*")) and any(base.rglob("nvmet-tcp.ko*"))


def fio_p99():
    """p99 d'une lecture 4K courte. None si fio ne peut pas mesurer."""
    if not shutil.which("fio"):
        return None
    target = _first_backstore()
    if not target:
        return None
    proc = subprocess.run(
        [
            "fio", "--name=san", "--filename", target, "--rw=randread",
            "--bs=4k", "--direct=1", "--ioengine=libaio", "--iodepth=8",
            "--runtime=2", "--time_based=1", "--size=32M",
            "--output-format=json", "--group_reporting",
        ],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        job = json.loads(proc.stdout)["jobs"][0]["read"]
        return float(job["clat_ns"]["percentile"]["99.000000"]) / 1000.0
    except (KeyError, ValueError, IndexError, json.JSONDecodeError):
        return None


def _catalog(paths):
    path = _share(paths) / "perf-results.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"profiles": {}, "current": "baseline"}
    profiles = {"baseline": {"change": {}, "p99_us": data.get("baseline_p99_us")}}
    for item in data.get("keep") or []:
        profiles[item["name"]] = {"change": item["change"], "p99_us": item.get("p99_us")}
    for name, trial in (data.get("trials") or {}).items():
        change = trial.get("change") or {}
        if name == "baseline":
            profiles["baseline"] = {
                "change": {},
                "p99_us": _max_p99(trial),
            }
        elif isinstance(change, dict) and len(change) == 1:
            profiles[name] = {"change": change, "p99_us": _max_p99(trial)}
    current = data.get("current") or "baseline"
    return {"profiles": profiles, "current": current}


def _max_p99(trial):
    loads = (trial or {}).get("workloads") or {}
    values = [item.get("p99_us") for item in loads.values() if isinstance(item, dict)]
    values = [item for item in values if isinstance(item, (int, float))]
    if not values:
        return None
    return max(values)


def _p99_of(catalog, name):
    entry = (catalog.get("profiles") or {}).get(name) or {}
    return entry.get("p99_us")


def _change_of(catalog, name):
    entry = (catalog.get("profiles") or {}).get(name) or {}
    change = entry.get("change") or {}
    return dict(change)


def _share(paths):
    return paths.root / "usr/share/san"


def _state(path, ok, reason):
    path.write_text(
        json.dumps({"ok": ok, "reason": reason}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o644)


def _find_label(label):
    proc = subprocess.run(
        ["findfs", "LABEL={}".format(label)],
        text=True,
        capture_output=True,
        check=False,
    )
    found = proc.stdout.strip()
    return found or ""


def _mount(dev, target, options):
    proc = subprocess.run(
        ["mount", "-o", options, dev, str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def _umount(target):
    subprocess.run(["umount", str(target)], check=False, capture_output=True)


def _mountpoint(path):
    proc = subprocess.run(["mountpoint", "-q", str(path)], check=False)
    return proc.returncode == 0


def _volume_for_path(probe, dev):
    try:
        real = os.path.realpath(dev)
    except OSError:
        real = dev
    name = os.path.basename(real)
    for volume in probe.list_volumes():
        if volume.get("kernel") == name:
            return volume
        try:
            if os.path.realpath(volume.get("path") or "") == real:
                return volume
        except OSError:
            continue
    return None


def _write_block(dev, payload):
    if not str(dev).startswith("/dev/disk/by-id/"):
        raise UpdateError("écriture hors by-id refusée")
    fd = os.open(dev, os.O_WRONLY)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def _modprobe(name):
    proc = subprocess.run(["modprobe", name], check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        raise NvmeError("module {} absent".format(name))


def _configfs_subsystem(item, device):
    root = Path("/sys/kernel/config")
    subprocess.run(["modprobe", "configfs"], check=False, capture_output=True)
    if not os.path.ismount(str(root)):
        subprocess.run(["mount", "-t", "configfs", "none", "/sys/kernel/config"], check=False)
    _modprobe("nvmet")
    _modprobe("nvmet-tcp")
    nvmet = root / "nvmet"
    if not nvmet.is_dir():
        raise NvmeError("configfs nvmet absent après chargement du module")
    nqn = item["nqn"]
    if "/" in nqn or nqn.startswith("."):
        raise NvmeError("NQN refusé")
    subs = root / "nvmet/subsystems" / nqn
    subs.mkdir(parents=True, exist_ok=True)
    (subs / "attr_allow_any_host").write_text("0\n", encoding="utf-8")
    ns = subs / "namespaces/1"
    ns.mkdir(parents=True, exist_ok=True)
    try:
        block = os.path.realpath(device)
    except OSError as exc:
        raise NvmeError("volume NVMe inaccessible") from exc
    (ns / "device_path").write_text(block + "\n", encoding="utf-8")
    (ns / "enable").write_text("1\n", encoding="utf-8")
    host = item["host"]
    host_dir = root / "nvmet/hosts" / host
    host_dir.mkdir(parents=True, exist_ok=True)
    link = subs / "allowed_hosts" / host
    if not link.exists():
        link.symlink_to(host_dir)
    port = root / "nvmet/ports/1"
    port.mkdir(parents=True, exist_ok=True)
    (port / "addr_adrfam").write_text("ipv4\n", encoding="utf-8")
    (port / "addr_trtype").write_text("tcp\n", encoding="utf-8")
    (port / "addr_traddr").write_text(item["portal"]["address"] + "\n", encoding="utf-8")
    (port / "addr_trsvcid").write_text(str(item["portal"]["port"]) + "\n", encoding="utf-8")
    bound = port / "subsystems" / nqn
    if not bound.exists():
        bound.symlink_to(subs)


def _read_text(path):
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_scheduler():
    for path in Path("/sys/block").glob("*/queue/scheduler"):
        text = _read_text(path)
        if "[" in text and "]" in text:
            return text.split("[", 1)[1].split("]", 1)[0]
    return "none"


def _write_schedulers(name):
    written = False
    for path in _backstore_schedulers():
        current = _read_text(path)
        if name not in current.replace("[", "").replace("]", "").split():
            continue
        path.write_text(name, encoding="utf-8")
        written = True
    if not written:
        raise ValueError("scheduler indisponible sur le backstore")


def _backstore_schedulers():
    found = []
    core = Path("/sys/kernel/config/target/core")
    if not core.is_dir():
        return found
    for udev in core.glob("*/*/udev_path"):
        dev = _read_text(udev)
        name = os.path.basename(os.path.realpath(dev)) if dev else ""
        sched = Path("/sys/block") / name / "queue/scheduler"
        if name and sched.is_file():
            found.append(sched)
    return found


def _write_governors(name):
    paths = list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor"))
    if not paths:
        raise ValueError("gouverneur indisponible")
    for path in paths:
        path.write_text(name, encoding="utf-8")


def _storage_mtu():
    plan = _read_json(Path("/var/lib/san/conf/exports.json")) or {}
    iface = ((plan.get("network") or {}).get("storage") or {}).get("iface")
    if not iface:
        return ""
    return _read_text(Path("/sys/class/net") / iface / "mtu")


def _peer_mtu_matches(want):
    plan = _read_json(Path("/etc/san/network.json")) or {}
    exports = _read_json(Path("/var/lib/san/conf/exports.json")) or {}
    network = exports.get("network") or plan
    peer = (network.get("storage") or {}).get("peer_mtu")
    return peer == want


def _set_storage_mtu(value):
    exports = _read_json(Path("/var/lib/san/conf/exports.json")) or {}
    iface = ((exports.get("network") or {}).get("storage") or {}).get("iface")
    if not iface:
        raise ValueError("interface stockage absente")
    proc = subprocess.run(
        ["ip", "link", "set", iface, "mtu", str(value)],
        check=False, capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ValueError("mtu refusé")


def _pin_iscsi(cpus):
    if cpus in ("", None):
        return
    allowed = str(cpus)
    if not all(ch.isdigit() or ch in ",-" for ch in allowed):
        raise ValueError("cpus refusé")
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            comm = (entry / "comm").read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not comm.startswith("iscsi_"):
            continue
        subprocess.run(["taskset", "-pc", allowed, entry.name], check=False, capture_output=True)


def _first_backstore():
    core = Path("/sys/kernel/config/target/core")
    if not core.is_dir():
        return ""
    for udev in sorted(core.glob("*/*/udev_path")):
        dev = _read_text(udev)
        if dev.startswith("/dev/"):
            return dev
    return ""


def publish_table(document, share):
    share.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    (share / "perf-results.json").write_text(text, encoding="utf-8")
    (share / "perf-results.txt").write_text(render_table(document), encoding="utf-8")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return digest
