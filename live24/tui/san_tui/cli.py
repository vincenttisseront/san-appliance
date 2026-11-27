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
    sub.add_parser("ollama-advise", help="conseiller optionnel, sans bloquer les exports")
    sub.add_parser("rollback", help="revenir à la sauvegarde précédente puis rétablir")
    sub.add_parser("toram-measure", help="mesurer la RAM, sans activer toram")
    sub.add_parser("quota-apply", help="poser les quotas du profil de volume")
    quota_undo = sub.add_parser("quota-undo", help="retirer le quota automatique")
    quota_undo.add_argument("--volume", default="")
    sub.add_parser("snapshot-auto", help="prendre les instantanés crash-consistent")
    snap_undo = sub.add_parser("snapshot-rollback", help="revenir un seul LUN à son instantané")
    snap_undo.add_argument("--volume", default="")
    sub.add_parser("clone-auto", help="cloner le LUN vers le seul volume libre")
    sub.add_parser("clone-undo", help="retirer le clone automatique")
    sub.add_parser("thin-auto", help="pool thin, alerte, réserve")
    sub.add_parser("crypt-auto", help="ouvrir ou chiffrer, clé hors de l'ISO")
    sub.add_parser("chap-rotate", help="tourner le CHAP sans couper les sessions")
    sub.add_parser("chap-undo", help="reposer le secret CHAP précédent")
    sub.add_parser("portal-once", help="ouvrir le portail de secours si le lien est tombé")
    sub.add_parser("portal-watch", help="surveiller le lien de stockage")
    sub.add_parser("portal-undo", help="retirer le portail de secours")
    sub.add_parser("lacp-auto", help="agréger le stockage si LACP est annoncé")
    sub.add_parser("lacp-undo", help="retirer l'agrégat automatique")

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
            ))
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
        if args.command in _AUTO_COMMANDS:
            try:
                return _print(_auto(args, paths, probe))
            except Exception as exc:
                return _print({"ok": False, "reason": exc.__class__.__name__})
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


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


_AUTO_COMMANDS = {
    "quota-apply", "quota-undo", "snapshot-auto", "snapshot-rollback",
    "clone-auto", "clone-undo", "thin-auto", "crypt-auto",
    "chap-rotate", "chap-undo", "portal-once", "portal-watch", "portal-undo",
    "lacp-auto", "lacp-undo",
}


def _auto(args, paths, probe):
    if args.command == "quota-apply":
        from san_tui.quota import apply_quotas
        return apply_quotas(paths, probe)
    if args.command == "quota-undo":
        from san_tui.quota import undo_quota
        return undo_quota(paths, probe, args.volume or None)
    if args.command == "snapshot-auto":
        from san_tui.snapshot import take
        return take(paths, probe)
    if args.command == "snapshot-rollback":
        from san_tui.snapshot import rollback
        return rollback(paths, probe, args.volume or None)
    if args.command == "clone-auto":
        from san_tui.clone import clone
        return clone(paths, probe)
    if args.command == "clone-undo":
        from san_tui.clone import undo
        return undo(paths)
    if args.command == "thin-auto":
        from san_tui.thin import ensure_pools
        return ensure_pools(paths)
    if args.command == "crypt-auto":
        from san_tui.luks import prepare
        return prepare(paths, probe)
    if args.command == "chap-rotate":
        from san_tui.chap import rotate
        return rotate(paths)
    if args.command == "chap-undo":
        from san_tui.chap import undo
        return undo(paths)
    if args.command == "portal-once":
        from san_tui.portal import once
        return once(paths, probe)
    if args.command == "portal-watch":
        from san_tui.portal import watch
        watch(paths, probe)
        return {"ok": True}
    if args.command == "portal-undo":
        from san_tui.portal import undo
        return undo(paths, probe)
    if args.command == "lacp-auto":
        from san_tui.lacp import apply_lacp
        return apply_lacp(paths, probe)
    if args.command == "lacp-undo":
        from san_tui.lacp import undo
        return undo(paths, probe)
    return {"ok": False, "reason": "commande inconnue"}


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
