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
        description="Inventaire local et exports iSCSI LIO. Le boot n'exporte rien.",
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
    sub.add_parser("release-status", help="slots, dates et diff, sans rien appliquer")
    sub.add_parser("release-undo", help="revenir au slot précédent")
    sub.add_parser("config-export", help="reconstruire le fichier de conf déjà téléchargeable")
    sub.add_parser("config-import", help="importer la conf déposée, par WWN, sans écran de mapping")
    sub.add_parser("factory", help="effacer la conf si le marqueur usine est présent")
    sub.add_parser("ollama-advise", help="conseiller optionnel, sans bloquer les exports")
    sub.add_parser("rollback", help="revenir à la sauvegarde précédente puis rétablir")
    sub.add_parser("release-rollback", help="revenir au slot d'application précédent")
    sub.add_parser("boot-inventory", help="écrire une fois l'inventaire disques, cartes et firmware")
    sub.add_parser("syslog-apply", help="relayer le syslog s'il répond, sinon garder les journaux locaux")
    sub.add_parser("syslog-local", help="annuler le syslog distant")
    sub.add_parser("webui-unlock", help="lever le verrou de la WebUI")
    sub.add_parser("toram-measure", help="mesurer la RAM, sans activer toram")
    sub.add_parser("comfort", help="pair, témoin, groupes, QoS, audit et latence")
    rep = sub.add_parser("replicate", help="répliquer si un pair est déjà écrit")
    rep.add_argument("--follow", action="store_true")
    sub.add_parser("replicate-stop", help="suspendre la réplication")
    sub.add_parser("replicate-resume", help="reprendre la réplication automatique")
    sub.add_parser("witness", help="interroger le témoin, ou rester local")
    sub.add_parser("witness-stay", help="annuler une bascule et rester sur le slot local")
    sub.add_parser("witness-resume", help="laisser de nouveau le témoin décider")
    sub.add_parser("snapshot-groups", help="instantané des LUN qui partagent déjà un groupe")
    sub.add_parser("qos-apply", help="appliquer le profil de volume déjà présent")
    sub.add_parser("audit-prepare", help="préparer l'export d'audit signé")
    sub.add_parser("inventory-api", help="inventaire sur 127.0.0.1")
    sub.add_parser("inventory-stop", help="arrêter l'inventaire local")
    sub.add_parser("inventory-resume", help="rétablir l'inventaire local")
    sub.add_parser("metrics", help="métriques sur l'adresse d'administration")
    sub.add_parser("metrics-stop", help="arrêter les métriques")
    sub.add_parser("metrics-resume", help="rétablir les métriques")
    sub.add_parser("latency-check", help="alerter si le p99 dépasse le banc")
    sub.add_parser("retire", help="retirer un disque de données en échec")
    sub.add_parser("retire-undo", help="annuler le retrait automatique")

    args = parser.parse_args(argv)
    paths = SanPaths(args.root)
    probe = CommandProbe()
    try:
        if args.command in {
            "comfort", "replicate", "replicate-stop", "replicate-resume",
            "witness", "witness-stay", "witness-resume", "snapshot-groups",
            "qos-apply", "audit-prepare", "inventory-api", "inventory-stop",
            "inventory-resume", "metrics", "metrics-stop", "metrics-resume",
            "latency-check", "retire", "retire-undo",
        }:
            from san_tui.comfort import main_command
            return main_command(args, paths, probe)
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
            ))
        if args.command == "release-status":
            from san_tui.release import release_status
            return _print(release_status(paths))
        if args.command == "release-undo":
            from san_tui.release import undo_slot
            return _print(undo_slot(paths))
        if args.command == "config-export":
            from san_tui.configio import refresh_export
            try:
                ready = refresh_export(paths)
            except PersistError as exc:
                if "absente" in str(exc):
                    return _print({"ready": False, "reason": str(exc)})
                raise
            return _print({"ready": True, "path": ready})
        if args.command == "config-import":
            from san_tui.configio import import_incoming
            return _print(import_incoming(paths, probe))
        if args.command == "factory":
            from san_tui.configio import factory_reset
            return _print(factory_reset(paths))
        if args.command == "ollama-advise":
            from san_tui.runtime import advise
            return _print(advise(paths))
        if args.command == "rollback":
            from san_tui.runtime import rollback
            backend = RecordingBackend() if os.environ.get("SAN_DRY_RUN") == "1" else RtslibBackend()
            return _print(rollback(paths, probe, backend))
        if args.command == "release-rollback":
            from san_tui.release import rollback_slot
            return _print(rollback_slot(paths))
        if args.command == "boot-inventory":
            from san_tui.probe import write_boot_inventory
            try:
                return _print(write_boot_inventory(paths, probe))
            except Exception:
                return _print({
                    "disks": [],
                    "nics": [],
                    "firmware": {},
                    "exports": [],
                    "reason": "inventaire incomplet",
                })
        if args.command == "syslog-apply":
            from san_tui.syslog import apply_remote
            try:
                return _print(apply_remote(paths))
            except Exception:
                return _print({"remote": False, "local": True, "reason": "erreur interne"})
        if args.command == "syslog-local":
            from san_tui.syslog import disable_remote
            return _print(disable_remote(paths))
        if args.command == "webui-unlock":
            return _print(_unlock_webui(paths))
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


def _unlock_webui(paths):
    path = paths.root / "var" / "lib" / "san" / "webui" / "lock.json"
    if path.is_file():
        path.unlink()
    return {"locked": False}


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
