"""Ligne de commande. Le mode sans argument ouvre la TUI sur un terminal."""

import argparse
import json
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
            backend = RecordingBackend() if args.dry_run else RtslibBackend()
            return _print(restore(paths, probe, backend))
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
    except (SafetyError, ExportError, PersistError) as exc:
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


def _print(payload):
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0
