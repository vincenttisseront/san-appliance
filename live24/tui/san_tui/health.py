"""SMART et température. Une alerte ne retire pas un export."""

import json
import shutil
import subprocess

from san_tui.alerts import add_alert


# Seuil fixe. Le dépasser prévient, il ne change ni un portail ni un LUN.
DISK_TEMP_ALERT_C = 55


def temperature_of(report):
    if not isinstance(report, dict):
        return None
    current = (report.get("temperature") or {}).get("current")
    if isinstance(current, int) and not isinstance(current, bool):
        return current
    table = ((report.get("ata_smart_attributes") or {}).get("table") or [])
    for row in table:
        if not isinstance(row, dict):
            continue
        if row.get("id") != 194 and row.get("name") != "Temperature_Celsius":
            continue
        raw = (row.get("raw") or {}).get("value")
        if isinstance(raw, int) and not isinstance(raw, bool):
            return raw
    return None


def judge_disk(report, disk_id):
    """Décide les alertes d'un disque. Ne connaît aucun export."""
    alerts = []
    passed = None
    if isinstance(report, dict):
        status = report.get("smart_status") or {}
        if isinstance(status, dict):
            passed = status.get("passed")
    if passed is False:
        alerts.append(("error", "SMART en échec : {}".format(disk_id)))
    temp = temperature_of(report)
    if temp is not None and temp > DISK_TEMP_ALERT_C:
        alerts.append((
            "warning",
            "température {}°C au-dessus de {}°C : {}".format(temp, DISK_TEMP_ALERT_C, disk_id),
        ))
    return {
        "alerts": alerts,
        "temperature": temp,
        "passed": passed,
        "exports_changed": False,
    }


def health_check(paths, volumes, run_smart):
    """Écrit des alertes. run_smart vaut None si smartctl n'est pas là.

    Le dictionnaire renvoyé porte exports_changed à False : cet appel ne
    désactive pas une cible et ne réécrit pas exports.json.
    """
    paths.ensure_runtime()
    if run_smart is None:
        _once(paths, "info", "SMART ignoré : smartctl absent")
        report = {"skipped": "smartctl absent", "exports_changed": False, "disks": {}}
        _store(paths, report)
        return report
    disks = {}
    for volume in volumes or []:
        disk_id = volume.get("id") or volume.get("path") or "?"
        try:
            raw = run_smart(volume)
        except Exception:
            raw = None
        if not isinstance(raw, dict) or raw.get("smartctl_missing"):
            continue
        judged = judge_disk(raw, disk_id)
        disks[disk_id] = {
            "temperature": judged["temperature"],
            "passed": judged["passed"],
        }
        for level, message in judged["alerts"]:
            add_alert(paths, level, message)
    report = {"exports_changed": False, "disks": disks}
    _store(paths, report)
    return report


def smartctl_runner():
    """None si l'outil n'est pas installé. N'élève pas si un disque ignore SMART."""
    if shutil.which("smartctl") is None:
        return None

    def run(volume):
        path = volume.get("path") or ""
        if not path.startswith("/dev/"):
            return {}
        proc = subprocess.run(
            ["smartctl", "-j", "-H", "-A", path],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    return run


def _once(paths, level, message):
    marker = paths.run / "health-skip"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() == message:
        return
    add_alert(paths, level, message)
    marker.write_text(message + "\n", encoding="utf-8")


def _store(paths, report):
    path = paths.run / "health.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
