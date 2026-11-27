"""Journal SCSI pour la WebUI. Le contenu du LUN n'est pas conservé."""

import json
import os
import re
from datetime import datetime, timezone

FIELDS = ("time", "iqn", "lun", "opcode", "status", "sense_key", "asc", "ascq")
_IQN = re.compile(r"(iqn\.[A-Za-z0-9._:-]+)")
_OPCODE = re.compile(r"opcode\s+0x([0-9a-fA-F]{1,2})", re.I)
_LUN = re.compile(r"\bLUN\s+(\d+)\b", re.I)
_SENSE = re.compile(
    r"sense\s+0x([0-9a-fA-F]{1,2})/0x([0-9a-fA-F]{1,2})/0x([0-9a-fA-F]{1,2})",
    re.I,
)
_STATUS = re.compile(r"\bstatus\s+([A-Za-z0-9_]+)", re.I)


def boards_dir(paths):
    dest = paths.root / "var" / "lib" / "san" / "webui" / "boards"
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o755)
    return dest


def log_path(paths):
    return boards_dir(paths) / "scsi-errors.json"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_line(line, now=None):
    """Ne renvoie que des champs SCSI. La ligne brute, et donc la charge, est jetée."""
    if not isinstance(line, str):
        return None
    lowered = line.lower()
    if not any(word in lowered for word in ("target", "iscsi", "scsi")):
        return None
    if "sense" not in lowered and "opcode" not in lowered:
        return None
    opcode = _OPCODE.search(line)
    sense = _SENSE.search(line)
    if opcode is None and sense is None:
        return None
    iqn = _IQN.search(line)
    lun = _LUN.search(line)
    status = _STATUS.search(line)
    record = {
        "time": now or _now(),
        "iqn": iqn.group(1) if iqn else "",
        "lun": int(lun.group(1)) if lun else None,
        "opcode": "0x{}".format(opcode.group(1).lower()) if opcode else "",
        "status": status.group(1) if status else "",
        "sense_key": "0x{}".format(sense.group(1).lower()) if sense else "",
        "asc": "0x{}".format(sense.group(2).lower()) if sense else "",
        "ascq": "0x{}".format(sense.group(3).lower()) if sense else "",
    }
    return {key: record[key] for key in FIELDS}


def load(paths):
    path = log_path(paths)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    clean = []
    for item in data:
        if not isinstance(item, dict):
            continue
        clean.append({key: item.get(key) for key in FIELDS})
    return clean


def _write(paths, records):
    path = log_path(paths)
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)


def ingest(paths, lines, now=None):
    records = load(paths)
    stamp = now or _now()
    for line in lines:
        record = parse_line(line, stamp)
        if record is None:
            continue
        records.append(record)
    records = records[-200:]
    _write(paths, records)
    return records


def clear(paths):
    _write(paths, [])
    return []


def collect_dmesg(paths, reader=None):
    """Lit le journal noyau. Une lecture impossible laisse le journal vide et ne lève pas."""
    if reader is None:
        import subprocess

        def reader():
            completed = subprocess.run(
                ["dmesg", "--color=never"],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                return []
            return completed.stdout.splitlines()

    try:
        lines = reader()
    except Exception:
        return load(paths)
    return ingest(paths, lines)
