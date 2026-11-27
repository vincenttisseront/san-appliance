"""Choix automatiques. Un drapeau retire un choix, il n'ouvre pas de formulaire."""

from san_tui.persist import PersistError, _read_json, _write_json


def _files(paths):
    return (paths.conf / "auto-undo.json", paths.etc / "auto-undo.json")


def undone(paths, key):
    for path in _files(paths):
        try:
            data = _read_json(path)
        except PersistError:
            continue
        if isinstance(data, dict) and data.get(key) is True:
            return True
    return False


def mark_undone(paths, key):
    """Retire un choix déjà pris. N'ajoute aucun réglage à saisir."""
    paths.ensure_runtime()
    current = {}
    for path in _files(paths):
        try:
            data = _read_json(path)
        except PersistError:
            data = None
        if isinstance(data, dict):
            current.update(data)
    current[key] = True
    for path in _files(paths):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(path, current, 0o644)
        except OSError:
            continue
    return current
