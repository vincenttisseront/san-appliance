"""Ligne de commande. Le mode sans argument ouvre la TUI sur un terminal."""

import argparse
import json
import os
import sys

from san_tui.alerts import load_alerts
from san_tui.iscsi import (
    ExportError,
    RecordingBackend,
    RtslibBackend,
    add_export,
    impact_for,
    plan_export,
    restore,
)
from san_tui.paths import SanPaths
from san_tui.persist import PersistError, select_support
from san_tui.probe import CommandProbe, inventory
from san_tui.release import ReleaseError
from san_tui.safety import SafetyError, check_portal


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="san-tui",
        description="Inventaire, exports et annulation. Le boot choisit disque, réseau et cibles.",
    )
    parser.add_argument("--root", default=None, help="racine alternative (tests)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("inventory", help="inventaire seulement, sans export")
    sub.add_parser("alerts", help="afficher les alertes")

    restore_p = sub.add_parser("restore", help="rétablir les exports après vérifications")
    restore_p.add_argument("--dry-run", action="store_true")

    persist_p = sub.add_parser("persist-select", help="choisir explicitement un support")
    persist_p.add_argument("--role", required=True, choices=("conf", "data"))
    persist_p.add_argument("--id", required=True, help="nom by-id, par exemple wwn-0x...")
    persist_p.add_argument("--confirm", required=True, help="resaisir l'identifiant /dev/disk/by-id/...")

    net_p = sub.add_parser("network-set", help="enregistrer admin et stockage, sans les appliquer")
    net_p.add_argument("--admin-iface", required=True)
    net_p.add_argument("--admin-address", required=True)
    net_p.add_argument("--storage-iface", required=True)
    net_p.add_argument("--storage-address", required=True)

    exp = sub.add_parser("export-add", help="enregistrer un export, sans l'activer")
    exp.add_argument("--iqn", required=True)
    exp.add_argument("--portal", required=True)
    exp.add_argument("--port", type=int, default=3260)
    exp.add_argument("--volume", required=True, help="nom by-id")
    exp.add_argument("--lun", type=int, default=0)
    exp.add_argument("--initiator", required=True)
    exp.add_argument("--chap-user", required=True)
    exp.add_argument("--chap-secret-file", required=True, help="fichier 0600, jamais en argument")
    exp.add_argument("--confirm", required=True)

    sub.add_parser("medium-bind", help="monter le support seulement si l'identifiant colle")
    sub.add_parser("medium-status", help="code 0 si le support de conf est accepté")
    sub.add_parser("raid-assemble", help="assembler les arrays explicitement enregistrés")
    sub.add_parser("lvm-activate", help="activer les groupes explicitement enregistrés")
    sub.add_parser("nvme-restore", help="exporter le NVMe-oF enregistré")
    sub.add_parser("perf-apply", help="appliquer le seul réglage mesuré, s'il existe")
    sub.add_parser("ab-apply", help="écrire le slot inactif si une mise à jour est en attente")
    release_p = sub.add_parser("release-apply", help="appliquer une release GitHub sur le slot inactif")
    release_p.add_argument("--tag", default="")
    release_p.add_argument("--api", default="")
    sub.add_parser("ollama-advise", help="conseiller optionnel, sans bloquer les exports")
    sub.add_parser("rollback", help="annule la conf enregistrée puis rétablit")
    sub.add_parser("auto-boot", help="choisit disque et réseau, sans formulaire")
    sub.add_parser("auto-layout", help="enregistre RAID, LVM et exports manquants")
    sub.add_parser("release-boot", help="applique une release plus récente, sans bloquer iSCSI")
    sub.add_parser("release-health", help="contrôle le portail puis revient d'un slot si besoin")
    sub.add_parser("release-rollback", help="annule le slot actif, un pas à la fois")
    sub.add_parser("publish-status", help="écrit la vue publique des exports")
    sub.add_parser("undo", help="annule la conf enregistrée")
    esos_p = sub.add_parser("esos-import", help="importe IQN, LUN, WWID, bloc et ACL")
    esos_p.add_argument("--file", required=True)
    sub.add_parser("toram-measure", help="mesurer la RAM, sans activer toram")

    args = parser.parse_args(argv)
    paths = SanPaths(args.root)
    probe = CommandProbe()
    try:
        if args.command in (None, "tui"):
            from san_tui.ui import run_tui
            return run_tui(paths, probe)
        if args.command == "inventory":
            return _print(inventory(probe))
        if args.command == "alerts":
            return _print(load_alerts(paths))
        if args.command == "restore":
            from san_tui.runtime import diagnostic
            if diagnostic(paths):
                from san_tui.alerts import add_alert
                add_alert(paths, "info", "diagnostic : pas d'export iSCSI")
                return _print({"exported": [], "skipped": [{"iqn": "*", "reason": "diagnostic"}]})
            backend = RecordingBackend() if args.dry_run else RtslibBackend()
            return _print(restore(paths, probe, backend))
        if args.command == "medium-bind":
            from san_tui.runtime import bind_medium
            return _print(bind_medium(paths, probe))
        if args.command == "medium-status":
            from san_tui.runtime import medium_ok
            state = paths.run / "medium-state.json"
            if state.is_file():
                print(state.read_text(encoding="utf-8"), end="")
            return 0 if medium_ok(paths) else 2
        if args.command == "raid-assemble":
            from san_tui.raid import RecordingMd
            from san_tui.runtime import MdadmBackend, assemble_raid
            backend = RecordingMd() if os.environ.get("SAN_DRY_RUN") == "1" else MdadmBackend()
            return _print(assemble_raid(paths, probe, backend))
        if args.command == "lvm-activate":
            from san_tui.lvm import RecordingLvm
            from san_tui.runtime import LvmBackend, activate_lvm
            backend = RecordingLvm() if os.environ.get("SAN_DRY_RUN") == "1" else LvmBackend()
            return _print(activate_lvm(paths, probe, backend))
        if args.command == "nvme-restore":
            from san_tui.nvme import RecordingNvme
            from san_tui.runtime import ConfigfsNvme, restore_nvme
            backend = RecordingNvme() if os.environ.get("SAN_DRY_RUN") == "1" else ConfigfsNvme()
            return _print(restore_nvme(paths, probe, backend))
        if args.command == "perf-apply":
            from san_tui.runtime import MeasuredWriter, apply_measured
            return _print(apply_measured(paths, MeasuredWriter()))
        if args.command == "ab-apply":
            from san_tui.runtime import run_ab
            return _print(run_ab(paths, probe))
        if args.command == "release-apply":
            from san_tui.release import fetch_and_apply, read_token
            token = ""
            token_file = os.environ.get("SAN_GITHUB_TOKEN_FILE", "")
            if token_file:
                token = read_token(token_file)
            return _print(fetch_and_apply(
                paths,
                tag=args.tag,
                api=args.api,
                token=token,
                pubkey="/etc/san/release-pubkey.asc",
            ))
        if args.command == "release-boot":
            from san_tui.release import boot_apply, read_token
            token = ""
            token_file = os.environ.get("SAN_GITHUB_TOKEN_FILE", "/etc/san/github.token")
            if os.path.isfile(token_file):
                token = read_token(token_file)
            return _print(boot_apply(paths, opener=None, token=token))
        if args.command == "release-health":
            from san_tui.release import post_apply_health
            from san_tui.autoboot import publish_status
            health = post_apply_health(paths, _listen_lines())
            publish_status(paths)
            return _print(health)
        if args.command == "release-rollback":
            from san_tui.release import rollback_slot
            return _print(rollback_slot(paths))
        if args.command == "publish-status":
            from san_tui.autoboot import publish_status
            return _print(publish_status(paths))
        if args.command == "auto-boot":
            return _print(_auto_boot(paths, probe))
        if args.command == "auto-layout":
            return _print(_auto_layout(paths, probe))
        if args.command == "esos-import":
            return _print(_esos_import(paths, probe, args.file))
        if args.command == "undo":
            from san_tui.runtime import rollback
            backend = RecordingBackend() if os.environ.get("SAN_DRY_RUN") == "1" else RtslibBackend()
            return _print(rollback(paths, probe, backend))
        if args.command == "ollama-advise":
            from san_tui.runtime import advise
            return _print(advise(paths))
        if args.command == "rollback":
            from san_tui.runtime import rollback
            backend = RecordingBackend() if os.environ.get("SAN_DRY_RUN") == "1" else RtslibBackend()
            return _print(rollback(paths, probe, backend))
        if args.command == "toram-measure":
            from san_tui.runtime import measure_toram
            return _print(measure_toram(paths, _mem_kib(), _squash_bytes()))
        if args.command == "persist-select":
            volume = _need_volume(probe, args.id)
            choice = select_support(paths, args.role, volume, args.confirm)
            return _print(choice)
        if args.command == "network-set":
            plan = {
                "admin": {"iface": args.admin_iface, "address": args.admin_address},
                "storage": {"iface": args.storage_iface, "address": args.storage_address},
            }
            reason = check_portal(
                {"address": plan["storage"]["address"].split("/", 1)[0]},
                plan,
            )
            if reason:
                raise ExportError(reason)
            paths.ensure_runtime()
            from san_tui.persist import _write_json
            _write_json(paths.etc / "network.json", plan, 0o644)
            return _print(plan)
        if args.command == "export-add":
            volume = _need_volume(probe, args.volume)
            secret = _read_secret(args.chap_secret_file)
            plan = _network_plan(paths)
            drafted = plan_export(
                volume,
                {"address": args.portal, "port": args.port},
                args.iqn,
                args.initiator,
                args.chap_user,
                args.lun,
            )
            print(json.dumps(impact_for(volume), indent=2, ensure_ascii=False))
            saved = add_export(paths, plan, drafted, secret, args.confirm)
            return _print(saved)
    except (SafetyError, ExportError, PersistError, ReleaseError) as exc:
        print("refus : {}".format(exc), file=sys.stderr)
        return 2
    parser.error("commande inconnue")
    return 2


def _need_volume(probe, short_id):
    volume = probe.get_volume(short_id)
    if volume is None:
        raise ExportError("volume absent : {}".format(short_id))
    return volume


def _read_secret(path):
    from pathlib import Path
    file = Path(path)
    try:
        mode = file.stat().st_mode & 0o077
    except OSError as exc:
        raise ExportError("fichier CHAP illisible") from exc
    if mode != 0:
        raise ExportError("fichier CHAP trop ouvert")
    secret = file.read_text(encoding="utf-8").strip("\n")
    return secret


def _network_plan(paths):
    from san_tui.persist import _read_json
    plan = _read_json(paths.etc / "network.json")
    if not isinstance(plan, dict):
        raise ExportError("réseau admin et stockage non enregistrés")
    return plan


def _mem_kib():
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except OSError:
        return 0
    return 0


def _squash_bytes():
    for candidate in ("/cdrom/casper/filesystem.size", "/run/live/medium/casper/filesystem.size"):
        try:
            return int(open(candidate, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            continue
    return 0


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _listen_lines():
    import subprocess
    proc = subprocess.run(["ss", "-H", "-ltn"], text=True, capture_output=True, check=False)
    return proc.stdout.splitlines()


def _auto_boot(paths, probe):
    from pathlib import Path
    import subprocess
    from san_tui.autoboot import auto_boot
    from san_tui.evolve import seen_names
    from san_tui.runtime import diagnostic
    volumes = _annotated(probe)
    interfaces = _annotated_nics(probe)

    def create_fs(volume, stamp):
        if paths.root != Path("/"):
            raise RuntimeError("formatage hors appliance")
        device = volume.get("path") or ""
        if not device.startswith("/dev/"):
            raise RuntimeError("chemin refusé")
        subprocess.run(
            ["mkfs.ext4", "-F", "-L", "SANCONF", "-q", device],
            check=True, stdout=subprocess.DEVNULL,
        )
        mount = Path("/run/san/newconf")
        mount.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mount", device, str(mount)], check=True)
        try:
            (mount / "chosen.json").write_text(
                json.dumps(stamp, indent=2) + "\n", encoding="utf-8",
            )
        finally:
            subprocess.run(["umount", str(mount)], check=False)

    label = False
    if paths.root == Path("/"):
        found = subprocess.run(["findfs", "LABEL=SANCONF"], capture_output=True, check=False)
        label = found.returncode == 0
    cpufreq = Path("/sys/devices/system/cpu/cpu0/cpufreq").is_dir()
    note = ""
    for candidate in (Path("/usr/share/san/io-uring.txt"), paths.root / "usr/share/san/io-uring.txt"):
        if candidate.is_file():
            note = candidate.read_text(encoding="utf-8")
            break
    rtslib = "expose un paramètre" in note
    kernel = False
    if paths.root == Path("/"):
        kernel = _kernel_uring()
    seen = ""
    seen_file = paths.conf / "seen-initiators"
    if seen_file.is_file():
        seen += seen_file.read_text(encoding="utf-8")
    if paths.root == Path("/"):
        logged = subprocess.run(["dmesg"], text=True, capture_output=True, check=False)
        seen += logged.stdout or ""
    try:
        return auto_boot(
            paths, volumes, interfaces,
            diagnostic=diagnostic(paths),
            label_present=label,
            create_fs=create_fs if paths.root == Path("/") else None,
            cpufreq=cpufreq,
            rtslib_uring=rtslib,
            kernel_uring=kernel and rtslib,
            seen_text="\n".join(
                name for name in seen_names(seen, "iqn") if "local.san:" not in name
            ),
        )
    except Exception as exc:
        from san_tui.alerts import add_alert
        add_alert(paths, "error", "auto-boot interrompu : {}".format(exc.__class__.__name__))
        return {"skipped": exc.__class__.__name__}


def _auto_layout(paths, probe):
    from pathlib import Path
    import subprocess
    from san_tui.autoboot import ensure_recorded, layout_commands, publish_status
    from san_tui.evolve import import_esos_iscsi, seen_names
    from san_tui.runtime import diagnostic
    if diagnostic(paths):
        return {"skipped": "diagnostic"}
    seen = ""
    seen_file = paths.conf / "seen-initiators"
    if seen_file.is_file():
        seen += seen_file.read_text(encoding="utf-8")
    initiators = [name for name in seen_names(seen, "iqn") if "local.san:" not in name]
    hosts = seen_names(seen, "nqn")
    modules = False
    note = Path("/usr/share/san/nvme-of.txt")
    if note.is_file() and "présents" in note.read_text(encoding="utf-8"):
        modules = True
    try:
        result = ensure_recorded(paths, _annotated(probe), initiators, hosts, modules_nvme=modules)
    except Exception as exc:
        from san_tui.alerts import add_alert
        add_alert(paths, "error", "préparation export interrompue : {}".format(exc.__class__.__name__))
        result = {"exports": exc.__class__.__name__}
    created = (result or {}).get("created") or {}
    layout = created.get("layout") or {}
    if paths.root == Path("/") and (created.get("raid") or created.get("lvm")):
        for cmd in layout_commands(layout):
            subprocess.run(cmd, input="yes\n", text=True, check=False)
    incoming = paths.conf / "incoming-esos.json"
    if incoming.is_file():
        try:
            _esos_import(paths, probe, str(incoming))
            incoming.rename(incoming.with_suffix(".done"))
        except Exception:
            from san_tui.alerts import add_alert
            add_alert(paths, "error", "import ESOS refusé")
    publish_status(paths)
    return result


def _esos_import(paths, probe, filename):
    from pathlib import Path
    from san_tui.evolve import import_esos_iscsi
    from san_tui.iscsi import ExportError
    text = Path(filename).read_text(encoding="utf-8")
    try:
        record = import_esos_iscsi(text)
    except ValueError as exc:
        raise ExportError(str(exc)) from exc
    matched = None
    for volume in _annotated(probe):
        identity = volume.get("wwn") or volume.get("serial") or ""
        if identity and identity == record["wwid"]:
            matched = volume
            break
    record["matched"] = bool(matched)
    if matched is None:
        from san_tui.alerts import add_alert
        add_alert(paths, "error", "import ESOS : WWID absent, rien n'est exporté")
    paths.ensure_runtime()
    from san_tui.persist import _write_json
    _write_json(paths.conf / "esos-import.json", record, 0o644)
    return record


def _annotated(probe):
    volumes = []
    for volume in probe.list_volumes():
        item = dict(volume)
        item["signature"] = _signature(item.get("kernel") or "")
        volumes.append(item)
    return volumes


def _signature(kernel):
    import subprocess
    from pathlib import Path
    if not kernel or not Path("/dev", kernel).exists():
        return None
    proc = subprocess.run(
        ["blkid", "-p", "-o", "export", "/dev/{}".format(kernel)],
        text=True, capture_output=True, check=False,
    )
    if proc.returncode == 2:
        return ""
    if proc.returncode != 0:
        return None
    kind = ""
    for line in proc.stdout.splitlines():
        if line.startswith("TYPE=") or line.startswith("PTTYPE="):
            kind = line.split("=", 1)[1]
            break
    return kind


def _annotated_nics(probe):
    from pathlib import Path
    found = []
    for item in probe.list_interfaces():
        nic = dict(item)
        nic["max_mtu"] = _sysfs_int(Path("/sys/class/net") / item["name"] / "mtu")
        nic["peer_mtu"] = None
        found.append(nic)
    return found


def _sysfs_int(path):
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _kernel_uring():
    import subprocess
    from pathlib import Path
    proc = subprocess.run(["uname", "-r"], text=True, capture_output=True, check=False)
    release = (proc.stdout or "").strip()
    config = Path("/boot") / "config-{}".format(release)
    if not config.is_file():
        return False
    text = config.read_text(encoding="utf-8", errors="ignore")
    return "CONFIG_IO_URING=y" in text or "CONFIG_IO_URING=m" in text
