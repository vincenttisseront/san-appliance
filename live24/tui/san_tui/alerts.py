"""Alertes locales. Le texte est rédigé avant d'être écrit."""

import json
from datetime import datetime, timezone

from san_tui.redact import redact


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_alerts(paths):
    path = paths.run / "alerts.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return data


def add_alert(paths, level, message, secrets=()):
    paths.ensure_runtime()
    alerts = load_alerts(paths)
    alerts.append({
        "time": _now(),
        "level": level,
        "message": redact(message, secrets),
    })
    path = paths.run / "alerts.json"
    path.write_text(json.dumps(alerts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o640)
    return alerts[-1]
