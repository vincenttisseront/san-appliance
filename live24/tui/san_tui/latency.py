"""Alerte si le p99 d'un LUN dépasse le dernier seuil mesuré. L'export ne change pas."""

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json


def threshold_us(document):
    if not isinstance(document, dict) or document.get("status") != "measured":
        return None
    keep = document.get("keep") or []
    if len(keep) != 1 or not isinstance(keep[0], dict):
        return None
    value = keep[0].get("p99_us")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return float(value)


def load_threshold(paths):
    path = paths.root / "usr" / "share" / "san" / "perf-results.json"
    try:
        document = _read_json(path)
    except PersistError:
        return None
    return threshold_us(document)


def check(paths, samples, threshold):
    """Compare et alerte. Ne reçoit pas de backend d'export."""
    if threshold is None:
        return {"action": "skip", "reason": "banc absent", "alerted": [], "exports_changed": False}
    alerted = []
    for sample in samples or []:
        iqn = sample.get("iqn") or "?"
        try:
            current = float(sample.get("p99_us"))
        except (TypeError, ValueError):
            continue
        if current > threshold:
            add_alert(
                paths,
                "error",
                "LUN {} : p99 {} µs au-dessus du banc {}".format(iqn, int(current), int(threshold)),
            )
            alerted.append(iqn)
    if not alerted:
        return {"action": "ok", "alerted": [], "exports_changed": False}
    return {"action": "alert", "alerted": alerted, "exports_changed": False}


def check_from_disk(paths):
    threshold = load_threshold(paths)
    try:
        document = _read_json(paths.run / "lun-p99.json") or {}
    except PersistError:
        document = {}
    samples = document.get("samples") if isinstance(document, dict) else None
    if not samples:
        return {"action": "skip", "reason": "mesure absente", "alerted": [], "exports_changed": False}
    return check(paths, samples, threshold)
