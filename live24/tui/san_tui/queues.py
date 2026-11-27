"""Files NVMe-oF : une par cœur de stockage, seulement si le noyau les expose."""

import os
from pathlib import Path


QUEUE_ATTRIBUTES = ("attr_qid_max", "qid_max")


def storage_cores():
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def queue_attribute(subsys):
    directory = Path(subsys)
    for name in QUEUE_ATTRIBUTES:
        path = directory / name
        if path.exists():
            return path
    return None


def apply_nvme_queues(subsys):
    """Pose le nombre de files. L'absence de l'attribut n'empêche pas l'export."""
    cores = storage_cores()
    attr = queue_attribute(subsys)
    if attr is None:
        present = sorted(entry.name for entry in Path(subsys).iterdir()) if Path(subsys).is_dir() else []
        return {
            "applied": False,
            "reason": "attribut de files absent du noyau",
            "cores": cores,
            "attributes": present,
        }
    previous = attr.read_text(encoding="utf-8").strip()
    try:
        attr.write_text("{}\n".format(cores), encoding="utf-8")
        value = attr.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return {
            "applied": False,
            "reason": "écriture refusée",
            "errno": exc.errno,
            "cores": cores,
            "attribute": attr.name,
            "previous": previous,
        }
    return {
        "applied": value == str(cores),
        "cores": cores,
        "attribute": attr.name,
        "previous": previous,
        "value": value,
    }
