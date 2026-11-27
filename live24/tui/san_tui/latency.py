"""Latence p99 par LUN, plus le chiffre global. Une mesure ratée n'arrête pas l'export."""

import json
import os
import subprocess
import tempfile

from san_tui.alerts import add_alert
from san_tui.persist import PersistError, _read_json
from san_tui.scsilog import boards_dir


def board_path(paths):
    return boards_dir(paths) / "latency.json"


def p99_from_fio(document):
    jobs = (document or {}).get("jobs") or []
    if not jobs or not isinstance(jobs[0], dict):
        return None
    job = jobs[0]
    side = job.get("read") or job.get("write") or {}
    pct = (side.get("clat_ns") or {}).get("percentile") or {}
    raw = pct.get("99.000000", pct.get("99.0"))
    if raw is None:
        return None
    return float(raw) / 1000.0


def measure_board(luns, runner):
    rows = []
    values = []
    for lun in luns or []:
        ident = {
            "iqn": (lun or {}).get("iqn") or "",
            "lun": (lun or {}).get("lun"),
        }
        try:
            metric = runner(lun)
            p99 = metric.get("p99_us") if isinstance(metric, dict) else None
            if not isinstance(p99, (int, float)):
                raise ValueError("p99 absent")
            ident.update({"p99_us": float(p99), "ok": True})
            values.append(float(p99))
        except Exception as exc:
            ident.update({"ok": False, "error": exc.__class__.__name__})
        rows.append(ident)
    return {
        "global_p99_us": max(values) if values else None,
        "luns": rows,
    }


def fio_one(path):
    if not path:
        raise OSError("chemin absent")
    proc = subprocess.run(
        [
            "fio", "--name=lun", "--filename", str(path),
            "--rw=randread", "--bs=4k", "--size=4M",
            "--ioengine=psync", "--direct=0",
            "--runtime=1", "--time_based=1",
            "--lat_percentiles=1", "--percentile_list=99",
            "--group_reporting", "--output-format=json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise OSError("fio en échec")
    p99 = p99_from_fio(json.loads(proc.stdout))
    if p99 is None:
        raise OSError("p99 absent")
    return {"p99_us": p99}


def _write(paths, board):
    path = board_path(paths)
    path.write_text(json.dumps(board, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def load(paths):
    path = board_path(paths)
    if not path.is_file():
        return {"global_p99_us": None, "luns": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"global_p99_us": None, "luns": []}
    if not isinstance(data, dict):
        return {"global_p99_us": None, "luns": []}
    return data


def clear(paths):
    board = {"global_p99_us": None, "luns": []}
    _write(paths, board)
    return board


def sample_exports(paths, probe, runner=None):
    """Mesure chaque export. Un LUN illisible reste une ligne d'erreur."""
    targets = []
    try:
        from san_tui.persist import load_exports

        document, _secrets = load_exports(paths)
        targets = document.get("targets") or []
    except Exception:
        targets = []
    luns = []
    for target in targets:
        volume = target.get("volume") or {}
        current = None
        if probe is not None:
            try:
                current = probe.get_volume(volume.get("id"))
            except Exception:
                current = None
        path = ((current or volume).get("path")) or ""
        luns.append({
            "iqn": target.get("iqn") or "",
            "lun": target.get("lun"),
            "path": path,
        })

    def run(lun):
        if runner is not None:
            return runner(lun)
        return fio_one(lun.get("path"))

    board = measure_board(luns, run)
    try:
        _write(paths, board)
    except OSError:
        return board
    return board


def bench_file():
    """Banc court sur un fichier, pas sur un disque physique."""
    handle = tempfile.NamedTemporaryFile(prefix="san-fio-", delete=False)
    handle.close()
    os.truncate(handle.name, 8 * 1024 * 1024)
    try:
        metric = fio_one(handle.name)
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
    return metric

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
