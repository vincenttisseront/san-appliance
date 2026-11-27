"""Actions de boot. Chaque effet de bord est derrière un rappel, pour les tests."""

import json
import os
from pathlib import Path

from san_tui.alerts import add_alert
from san_tui.auto import undone
from san_tui.evolve import (
    assign_exports,
    backup_matches,
    choose_network,
    export_refusal,
    format_refusal,
    generate_chap,
    io_uring_decision,
    persistence_stamp,
    pick_data_disk,
    plan_free_layout,
    public_exports,
    public_nvme,
    seen_names,
)
from san_tui.persist import _write_json


def auto_boot(paths, volumes, interfaces, diagnostic=False, label_present=False,
              create_fs=None, create_layout=None, cpufreq=False,
              rtslib_uring=False, kernel_uring=False, seen_text=""):
    """Choisit disque, réseau, RAID et LVM. N'exporte pas. Sort toujours un dict."""
    paths.ensure_runtime()
    result = {"disk": None, "network": None, "layout": None, "governor": None, "io_uring": None}
    if diagnostic:
        add_alert(paths, "info", "diagnostic : pas de formatage, pas d'export")
        result["skipped"] = "diagnostic"
        _write_runtime(paths, result)
        return result
    if not label_present:
        chosen, reason = pick_data_disk(volumes)
        if chosen is None:
            add_alert(paths, "info", reason)
            result["disk"] = reason
        else:
            refusal = format_refusal(chosen)
            if refusal:
                add_alert(paths, "error", refusal)
                result["disk"] = refusal
            elif create_fs is None:
                result["disk"] = chosen["id"]
            else:
                try:
                    create_fs(chosen, persistence_stamp(chosen))
                    result["disk"] = chosen["id"]
                except Exception as exc:
                    add_alert(paths, "error", "support non créé : {}".format(exc.__class__.__name__))
                    result["disk"] = "support non créé"
    else:
        result["disk"] = "SANCONF déjà présent"
    plan, net_reason = choose_network(interfaces)
    if plan is None:
        add_alert(paths, "info", "réseau non choisi : {}".format(net_reason))
        result["network"] = net_reason
    else:
        _write_json(paths.etc / "network.json", _public_network(plan), 0o644)
        result["network"] = plan["storage"]["iface"]
    reserved = []
    if isinstance(result["disk"], str) and result["disk"].startswith("/dev/"):
        reserved.append(result["disk"])
    layout = plan_free_layout(volumes, reserved)
    result["layout"] = {
        "arrays": len(layout["raid"]["arrays"]),
        "groups": len(layout["lvm"]["groups"]),
    }
    if create_layout is not None and (layout["raid"]["arrays"] or layout["lvm"]["groups"]):
        try:
            create_layout(layout)
        except Exception as exc:
            add_alert(paths, "error", "RAID ou LVM non créé : {}".format(exc.__class__.__name__))
    from san_tui.tune import apply_governor
    if undone(paths, "governor"):
        governor = {"set": False, "reason": "gouverneur retiré"}
    else:
        governor = apply_governor(paths, cpufreq)
    result["governor"] = governor
    if not governor["set"]:
        add_alert(paths, "info", governor["reason"])
    uring = io_uring_decision(rtslib_uring, kernel_uring)
    result["io_uring"] = uring
    if not uring["enabled"]:
        add_alert(paths, "info", uring["reason"])
    (paths.run / "io-uring.json").write_text(
        json.dumps(uring, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    result["initiators"] = seen_names(seen_text, "iqn")
    result["hosts"] = seen_names(seen_text, "nqn")
    _write_runtime(paths, result)
    return result


def ensure_recorded(paths, volumes, seen_initiators, seen_hosts, modules_nvme=False):
    """Complète une conf absente. N'écrase pas un export déjà enregistré."""
    if not (paths.conf / "chosen.json").is_file() and not (paths.etc / "chosen.json").is_file():
        return {"exports": "support absent", "nvme": "support absent"}
    network = _read(paths.etc / "network.json") or {}
    storages = list(network.get("storages") or [])
    if not storages and network.get("storage"):
        storages = [network["storage"]]
    data = [item for item in volumes or [] if not export_refusal(item)]
    conf_id = _conf_id(paths)
    data = [item for item in data if item.get("id") != conf_id]
    out = {"exports": "inchangé", "nvme": "inchangé"}
    exports_path = paths.conf / "exports.json"
    if not exports_path.is_file():
        pairs, skipped = assign_exports(storages, data)
        if not seen_initiators:
            add_alert(paths, "info", "export en attente : aucun initiateur vu")
            out["exports"] = "aucun initiateur"
        elif not pairs:
            reason = skipped[0]["reason"] if skipped else "aucun volume de données"
            add_alert(paths, "error", "export refusé : {}".format(reason))
            out["exports"] = reason
        else:
            secret = generate_chap(os.urandom)
            out["exports"] = _write_exports(paths, network, pairs, seen_initiators[0], secret)
            _reveal_chap(paths, secret)
    if not (paths.conf / "nvme.json").is_file():
        if not modules_nvme:
            add_alert(paths, "info", "NVMe-oF non exporté : modules absents")
            out["nvme"] = "modules absents"
        elif not seen_hosts:
            add_alert(paths, "info", "NVMe-oF en attente : aucun hôte vu")
            out["nvme"] = "aucun hôte"
        else:
            used = _used_volume_ids(paths)
            left = [item for item in data if item.get("id") not in used]
            pairs, skipped = assign_exports(storages, left)
            if not pairs:
                reason = skipped[0]["reason"] if skipped else "aucun volume de données"
                add_alert(paths, "error", "NVMe-oF refusé : {}".format(reason))
                out["nvme"] = reason
            else:
                out["nvme"] = _write_nvme(paths, network, pairs[0], seen_hosts[0])
    created = {"raid": False, "lvm": False, "layout": None}
    if not (paths.conf / "raid.json").is_file() or not (paths.conf / "lvm.json").is_file():
        reserved = [conf_id] if conf_id else []
        reserved.extend(_used_volume_ids(paths))
        layout = plan_free_layout(volumes, reserved)
        created["layout"] = layout
        if not (paths.conf / "raid.json").is_file():
            _write_json(paths.conf / "raid.json", layout["raid"], 0o644)
            created["raid"] = bool(layout["raid"]["arrays"])
        if not (paths.conf / "lvm.json").is_file():
            _write_json(paths.conf / "lvm.json", layout["lvm"], 0o644)
            created["lvm"] = bool(layout["lvm"]["groups"])
    out["created"] = created
    return out


def publish_status(paths):
    """Fichier public pour la WebUI. Aucun secret."""
    paths.ensure_runtime()
    exports = _read(paths.conf / "exports.json") or {"targets": []}
    nvme = _read(paths.conf / "nvme.json") or {"subsystems": []}
    alerts = _read(paths.run / "alerts.json") or []
    if not isinstance(alerts, list):
        alerts = []
    payload = {
        "exports": public_exports(exports, alerts),
        "nvme": public_nvme(nvme, alerts),
        "alerts": [
            {"time": item.get("time"), "level": item.get("level"), "message": item.get("message")}
            for item in alerts
            if isinstance(item, dict)
        ],
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for forbidden in ("chap_secret", "password", "token"):
        if forbidden in text:
            payload = {"exports": [], "nvme": [], "alerts": []}
            text = json.dumps(payload) + "\n"
            break
    path = paths.run / "public-status.json"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o644)
    return payload


def remember_initiator(paths, iqn):
    if not iqn or not iqn.startswith("iqn."):
        raise ValueError("IQN refusé")
    paths.ensure_runtime()
    path = paths.conf / "seen-initiators"
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if iqn not in current.split():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(iqn + "\n")
        os.chmod(path, 0o644)
    return iqn


def _write_exports(paths, network, pairs, initiator, secret):
    from san_tui.iscsi import plan_export
    from san_tui.persist import save_exports
    targets = []
    secrets = {}
    for index, pair in enumerate(pairs):
        drafted = plan_export(
            pair["volume"],
            pair["portal"],
            "iqn.2026-11.local.san:disk{}".format(index),
            initiator,
            "init1",
            0,
        )
        ref = drafted["acls"][0]["chap_secret_ref"]
        secrets[ref] = secret
        targets.append(drafted)
    document = {"version": 1, "network": _public_network(network), "targets": targets}
    save_exports(paths, document, secrets)
    add_alert(paths, "info", "ACL et CHAP créés pour {}".format(initiator))
    return "créé"


def _write_nvme(paths, network, pair, host):
    portal = dict(pair["portal"])
    portal["port"] = 4420
    document = {
        "version": 1,
        "network": _public_network(network),
        "subsystems": [{
            "nqn": "nqn.2026-11.local.san:nvme",
            "host": host,
            "allow_any_host": False,
            "portal": portal,
            "volume": {
                "id": pair["volume"]["id"],
                "wwn": pair["volume"].get("wwn") or "",
                "serial": pair["volume"].get("serial") or "",
                "size": pair["volume"].get("size"),
                "block_size": pair["volume"].get("block_size"),
            },
        }],
    }
    _write_json(paths.conf / "nvme.json", document, 0o644)
    add_alert(paths, "info", "NVMe-oF créé pour {}".format(host))
    return "créé"


def _reveal_chap(paths, secret):
    """Une fois sur la série de l'appliance, jamais dans le journal ni dans un test."""
    if paths.root != Path("/"):
        return
    if not Path("/dev/ttyS0").exists():
        return
    try:
        fd = os.open("/dev/ttyS0", os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    try:
        os.write(fd, "san-chap: user=init1 password={}\n".format(secret).encode())
    except OSError:
        pass
    finally:
        os.close(fd)


def _public_network(plan):
    storage = dict(plan.get("storage") or {})
    admin = dict(plan.get("admin") or {})
    out = {"admin": admin, "storage": storage}
    if plan.get("storages"):
        out["storages"] = [
            {"iface": item.get("iface"), "address": item.get("address"), "mtu": item.get("mtu")}
            for item in plan["storages"]
        ]
    return out


def _conf_id(paths):
    for path in (paths.conf / "chosen.json", paths.etc / "chosen.json"):
        data = _read(path)
        if isinstance(data, dict) and isinstance(data.get("conf"), dict):
            return data["conf"].get("id")
    return None


def _used_volume_ids(paths):
    found = set()
    exports = _read(paths.conf / "exports.json") or {}
    for target in exports.get("targets") or []:
        vol = (target.get("volume") or {}).get("id")
        if vol:
            found.add(vol)
    return found


def _read(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_runtime(paths, result):
    path = paths.run / "auto-boot.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def layout_commands(layout):
    """Commandes de création. L'assemblage au boot suivant reprend les membres enregistrés."""
    commands = []
    for array in (layout or {}).get("raid", {}).get("arrays") or []:
        name = array["name"]
        commands.append([
            "mdadm", "--create", "/dev/md/{}".format(name), "--level=1",
            "--raid-devices=2", "--metadata=1.2", "--force", "--run", *array["members"],
        ])
        commands.append(["mdadm", "--stop", "/dev/md/{}".format(name)])
    for group in (layout or {}).get("lvm", {}).get("groups") or []:
        commands.append(["pvcreate", "-ff", "-y", *group["pvs"]])
        commands.append(["vgcreate", group["name"], *group["pvs"]])
        commands.append(["lvcreate", "-y", "-n", group["lvs"][0], "-l", "100%FREE", group["name"]])
        commands.append(["vgchange", "-an", group["name"]])
    return commands


def volumes_allow_restore(document, volumes):
    return backup_matches(document, volumes)
