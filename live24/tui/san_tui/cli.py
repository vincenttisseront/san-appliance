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
    release_p = sub.add_parser("channels-sync", help="appliquer les canaux app et noyau, sans formulaire")
    release_p.add_argument("--api", default="")
    apply_p = sub.add_parser("release-apply", help="appliquer la release la plus récente, sans formulaire")
    apply_p.add_argument("--tag", default="")
    apply_p.add_argument("--api", default="")
    undo_p = sub.add_parser("release-undo", help="annuler la dernière promotion, ou le slot précédent")
    undo_p.add_argument("--channel", choices=("app", "kernel"))
    sub.add_parser("rescue-select", help="basculer sur l'image de secours si le slot ne démarre pas")
    sub.add_parser("rescue-undo", help="annuler le choix de secours de ce démarrage")
    sub.add_parser("latency-sample", help="mesurer le p99 de chaque LUN")
    sub.add_parser("scsi-collect", help="relever les erreurs SCSI sans la charge du LUN")
    sub.add_parser("ntp-apply", help="activer NTP sans bloquer le journal local")
    sub.add_parser("ntp-undo", help="annuler le choix NTP automatique")
    sub.add_parser("release-status", help="slots, dates et diff, sans rien appliquer")
    sub.add_parser("config-export", help="reconstruire le fichier de conf déjà téléchargeable")
    sub.add_parser("config-import", help="importer la conf déposée, par WWN, sans écran de mapping")
    sub.add_parser("factory", help="effacer la conf si le marqueur usine est présent")
    sub.add_parser("ollama-advise", help="conseiller optionnel, sans bloquer les exports")
    sub.add_parser("rollback", help="annule la conf enregistrée puis rétablit")
    sub.add_parser("release-rollback", help="annule le slot actif, un pas à la fois")
    sub.add_parser("boot-inventory", help="écrire une fois l'inventaire disques, cartes et firmware")
    sub.add_parser("syslog-apply", help="relayer le syslog s'il répond, sinon garder les journaux locaux")
    sub.add_parser("syslog-local", help="annuler le syslog distant")
    sub.add_parser("webui-unlock", help="lever le verrou de la WebUI")
    sub.add_parser("auto-boot", help="choisit disque et réseau, sans formulaire")
    sub.add_parser("auto-layout", help="enregistre RAID, LVM et exports manquants")
    sub.add_parser("release-boot", help="applique une release plus récente, sans bloquer iSCSI")
    sub.add_parser("release-health", help="contrôle le portail puis revient d'un slot si besoin")
    sub.add_parser("publish-status", help="écrit la vue publique des exports")
    sub.add_parser("undo", help="annule la conf enregistrée")
    esos_p = sub.add_parser("esos-import", help="importe IQN, LUN, WWID, bloc et ACL")
    esos_p.add_argument("--file", required=True)
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
    sub.add_parser("portal-undo", help="retirer le portail de secours et les portails ajoutés")
    sub.add_parser("lacp-auto", help="agréger le stockage si LACP est annoncé")
    sub.add_parser("lacp-undo", help="retirer l'agrégat automatique")
    sub.add_parser("health-check", help="alertes SMART et température, sans changer les exports")
    sub.add_parser("raid-scrub", help="scrub planifié, avec plafond de débit")
    sub.add_parser("raid-scrub-stop", help="arrêter le scrub automatique")
    sub.add_parser("raid-pace", help="reposer le plafond de reconstruction")
    sub.add_parser("raid-pace-undo", help="retirer le plafond de reconstruction")
    sub.add_parser("raid-spare-undo", help="retirer le spare ajouté automatiquement")
    sub.add_parser("portal-drop", help="retirer un portail dont l'adresse a disparu")
    sub.add_parser("iser-undo", help="désactiver iSER, le portail TCP reste")
    sub.add_parser("bond-undo", help="retirer l'agrégat automatique")
    sub.add_parser("mtu-undo", help="revenir à un MTU de 1500")
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
        if args.command == "channels-sync":
            from san_tui.fabric import ConfigfsGate
            from san_tui.release import read_token, sync_channels
            import urllib.request
            token = ""
            token_file = os.environ.get("SAN_GITHUB_TOKEN_FILE", "")
            if token_file:
                token = read_token(token_file)
            api = args.api or os.environ.get("SAN_RELEASE_API", "")

            def fetcher(channel):
                from san_tui.release import CHANNELS, fetch_release
                spec = CHANNELS[channel]
                archive, checksum, _signature = fetch_release(
                    api, "", token, urllib.request.build_opener(),
                    asset=spec["asset"], asset_sum=spec["sum"],
                )
                return archive, checksum

            return _print(sync_channels(paths, fetcher, gate=ConfigfsGate()))
        if args.command == "release-apply":
            from san_tui.release import fetch_and_apply, read_token
            if args.tag:
                raise ReleaseError("la mise à jour est automatique")
            token = ""
            token_file = os.environ.get("SAN_GITHUB_TOKEN_FILE", "")
            if token_file:
                token = read_token(token_file)
            return _print(fetch_and_apply(
                paths,
                tag="",
                api=args.api,
                token=token,
                pubkey="/etc/san/release-pubkey.asc",
            ))
        if args.command == "release-undo":
            from san_tui.release import undo_channel, undo_slot
            if args.channel:
                return _print(undo_channel(paths, args.channel))
            return _print(undo_slot(paths))
        if args.command == "rescue-select":
            from san_tui.rescue import select_boot
            return _print(select_boot(paths))
        if args.command == "rescue-undo":
            from san_tui.rescue import arm_undo, select_boot
            arm_undo(paths)
            return _print(select_boot(paths))
        if args.command == "latency-sample":
            from san_tui.latency import sample_exports
            return _print(sample_exports(paths, probe))
        if args.command == "scsi-collect":
            from san_tui.scsilog import collect_dmesg
            return _print(collect_dmesg(paths))
        if args.command == "ntp-apply":
            from san_tui.ntp import apply_ntp
            server = os.environ.get("SAN_NTP_SERVER", "ntp.ubuntu.com")
            try:
                timeout = float(os.environ.get("SAN_NTP_TIMEOUT", "1"))
            except ValueError:
                timeout = 1.0
            return _print(apply_ntp(paths, server=server, timeout=timeout))
        if args.command == "ntp-undo":
            from san_tui.ntp import undo_ntp
            return _print(undo_ntp(paths))
        if args.command == "release-status":
            from san_tui.release import release_status
            return _print(release_status(paths))
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
        if args.command in _AUTO_COMMANDS:
            try:
                return _print(_auto(args, paths, probe))
            except Exception as exc:
                return _print({"ok": False, "reason": exc.__class__.__name__})
        if args.command == "health-check":
            from san_tui.comfort import run_health
            return _print(run_health(paths, probe))
        if args.command == "raid-scrub":
            from san_tui.comfort import run_scrub
            return _print(run_scrub(paths))
        if args.command == "raid-scrub-stop":
            from san_tui.comfort import stop_scrub
            return _print(stop_scrub(paths))
        if args.command == "raid-pace":
            from san_tui.comfort import run_pace
            return _print(run_pace(paths))
        if args.command == "raid-pace-undo":
            from san_tui.comfort import undo_pace
            return _print(undo_pace(paths))
        if args.command == "raid-spare-undo":
            from san_tui.comfort import undo_spare
            from san_tui.runtime import MdadmBackend
            return _print(undo_spare(paths, MdadmBackend()))
        if args.command == "portal-drop":
            from san_tui.comfort import drop_absent_portals
            return _print(drop_absent_portals(paths, probe, "/sys/kernel/config"))
        if args.command == "iser-undo":
            from san_tui.comfort import undo_iser
            return _print(undo_iser(paths, "/sys/kernel/config"))
        if args.command == "bond-undo":
            from san_tui.comfort import undo_bond
            from san_tui.link import CommandRunner
            return _print(undo_bond(paths, CommandRunner()))
        if args.command == "mtu-undo":
            from san_tui.comfort import undo_mtu
            from san_tui.link import CommandRunner
            from san_tui.runtime import _storage_iface
            return _print(undo_mtu(paths, CommandRunner(), _storage_iface()))
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
        from san_tui.portal import undo as undo_standby
        from san_tui.comfort import undo_extra_portals
        standby = undo_standby(paths, probe)
        extra = undo_extra_portals(paths, "/sys/kernel/config")
        return {"standby": standby, "extra": extra}
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


def _unlock_webui(paths):
    path = paths.root / "var" / "lib" / "san" / "webui" / "lock.json"
    if path.is_file():
        path.unlink()
    return {"locked": False}


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
