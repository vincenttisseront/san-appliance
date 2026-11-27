"""Rotation CHAP. Le TPG reste enable=1 et les sessions saines restent."""

import secrets
import string

from san_tui.alerts import add_alert
from san_tui.iscsi import check_chap
from san_tui.lio import enable_value, session_count, set_chap_password
from san_tui.persist import PersistError, load_exports, save_exports

ALPHABET = string.ascii_letters + string.digits


def new_secret():
    return "".join(secrets.choice(ALPHABET) for _ in range(16))


def rotate(paths):
    try:
        document, secrets = load_exports(paths)
    except PersistError as exc:
        add_alert(paths, "error", "rotation CHAP : conf illisible")
        return {"rotated": [], "skipped": [{"iqn": "*", "reason": str(exc)}]}
    rotated = []
    skipped = []
    changed = False
    for target in document.get("targets") or []:
        iqn = target.get("iqn") or "?"
        before_enable = enable_value(iqn)
        before = session_count(iqn)
        updated = []
        failed = False
        fresh = {}
        previous = {}
        for acl in target.get("acls") or []:
            ref = acl.get("chap_secret_ref")
            initiator = acl.get("initiator") or ""
            if not ref or not initiator:
                skipped.append({"iqn": iqn, "reason": "ACL incomplète"})
                failed = True
                break
            previous[ref] = secrets.get(ref)
            if ref not in fresh:
                secret = new_secret()
                try:
                    check_chap(secret)
                except Exception as exc:
                    skipped.append({"iqn": iqn, "reason": str(exc)})
                    failed = True
                    break
                fresh[ref] = secret
        if failed:
            continue
        for acl in target.get("acls") or []:
            ref = acl.get("chap_secret_ref")
            initiator = acl.get("initiator") or ""
            try:
                wrote = set_chap_password(iqn, initiator, fresh[ref])
            except OSError:
                add_alert(paths, "error", "rotation CHAP non écrite : {}".format(iqn))
                wrote = False
            if not wrote:
                skipped.append({"iqn": iqn, "reason": "ACL absente, secret conservé"})
                failed = True
                break
            updated.append(initiator)
        if not failed and session_count(iqn) < before:
            for acl in target.get("acls") or []:
                ref = acl.get("chap_secret_ref")
                old = previous.get(ref)
                if old:
                    try:
                        set_chap_password(iqn, acl.get("initiator") or "", old)
                    except OSError:
                        pass
            add_alert(paths, "error", "rotation CHAP annulée : session perdue : {}".format(iqn))
            skipped.append({"iqn": iqn, "reason": "session perdue"})
            failed = True
        if failed:
            continue
        secrets.update(fresh)
        changed = True
        after_enable = enable_value(iqn)
        if failed:
            continue
        rotated.append({
            "iqn": iqn,
            "initiators": updated,
            "sessions_before": before,
            "sessions_after": session_count(iqn),
            "enable_before": before_enable,
            "enable_after": after_enable,
        })
    if changed:
        save_exports(paths, document, secrets)
    return {"rotated": rotated, "skipped": skipped}


def undo(paths):
    """Repose le secret de la sauvegarde précédente, sans désactiver le TPG."""
    from san_tui.persist import _read_json
    try:
        document, current = load_exports(paths)
    except PersistError as exc:
        return {"ok": False, "reason": str(exc)}
    backup = paths.conf / "backups"
    dirs = sorted(path for path in backup.iterdir() if path.is_dir()) if backup.is_dir() else []
    if not dirs:
        return {"ok": False, "reason": "aucune sauvegarde CHAP"}
    previous = _read_json(dirs[-1] / "secrets.json") or {}
    if not isinstance(previous, dict) or not previous:
        return {"ok": False, "reason": "sauvegarde CHAP vide"}
    for target in document.get("targets") or []:
        iqn = target.get("iqn") or ""
        before = session_count(iqn)
        for acl in target.get("acls") or []:
            ref = acl.get("chap_secret_ref")
            secret = previous.get(ref)
            if not secret:
                continue
            try:
                set_chap_password(iqn, acl.get("initiator") or "", secret)
            except OSError:
                return {"ok": False, "reason": "configfs refusé"}
            if session_count(iqn) < before:
                current_secret = current.get(ref)
                if current_secret:
                    set_chap_password(iqn, acl.get("initiator") or "", current_secret)
                return {"ok": False, "reason": "session perdue"}
    save_exports(paths, document, previous)
    return {"ok": True, "enable": {item.get("iqn"): enable_value(item.get("iqn") or "") for item in document.get("targets") or []}}
