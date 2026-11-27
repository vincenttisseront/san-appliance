"""Mode diagnostic. Il se pose tout seul quand la conf restaurée est invalide."""

from pathlib import Path

from san_tui.alerts import add_alert


# Ces refus ne sont pas une conf restaurée invalide : il n'y a rien à restaurer,
# ou le support n'a pas pu être lu. On n'exporte pas, sans passer en diagnostic.
ABSENT = {
    "aucun support étiqueté SANCONF",
    "aucun support de conf choisi",
    "montage en lecture impossible",
    "montage du support refusé",
}


def diagnostic(paths):
    if paths.root != Path("/"):
        flag = paths.run / "mode"
        if flag.is_file():
            return flag.read_text(encoding="utf-8").strip() == "diagnostic"
        return False
    mode = Path("/run/san-live/mode")
    if mode.is_file() and mode.read_text(encoding="utf-8").strip() == "diagnostic":
        return True
    flag = paths.run / "mode"
    return flag.is_file() and flag.read_text(encoding="utf-8").strip() == "diagnostic"


def enter_diagnostic(paths, reason):
    """Inventaire seulement. Les exports, le RAID et le LVM lisent ce drapeau."""
    paths.ensure_runtime()
    text = "diagnostic\n"
    (paths.run / "mode").write_text(text, encoding="utf-8")
    if paths.root == Path("/"):
        live = Path("/run/san-live")
        live.mkdir(parents=True, exist_ok=True)
        (live / "mode").write_text(text, encoding="utf-8")
    add_alert(paths, "error", "diagnostic : {}".format(reason))


def stamp_needs_diagnostic(reason):
    if not reason or reason in ABSENT:
        return False
    return True


def refuse_invalid_exports(paths):
    """Passe en diagnostic si exports.json est présent et illisible. Absent = rien à faire."""
    from san_tui.persist import PersistError, load_exports

    path = paths.conf / "exports.json"
    if not path.is_file():
        return False
    try:
        load_exports(paths)
    except PersistError as exc:
        enter_diagnostic(paths, "conf restaurée invalide : {}".format(exc))
        return True
    return False
