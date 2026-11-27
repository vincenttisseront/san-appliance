"""Image de secours sur le disque de données si le slot actif ne démarre pas."""

import json
import os
import shutil
from pathlib import Path


def rescue_dir(paths):
    return paths.data / "rescue" / "image"


def _boot_path(paths):
    return paths.run / "boot.json"


def _write_boot(paths, result):
    paths.ensure_runtime()
    path = _boot_path(paths)
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o644)
    return result


def _private_key(path):
    if not path.is_file() or path.stat().st_size > 65536:
        return False
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "PRIVATE KEY" in text


def refresh_rescue(paths, slot_dir):
    """Copie le slot sain. Une clé privée ne part pas dans l'image de secours."""
    src = Path(slot_dir)
    dest = rescue_dir(paths)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    if not src.is_dir():
        raise OSError("slot absent")
    for item in src.iterdir():
        if _private_key(item):
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("*.pem"))
        elif item.is_file():
            shutil.copy2(item, target)
            if item.name in {"health", "start"}:
                os.chmod(target, 0o755)
    marker = paths.data / "rescue" / "boot.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"version": 1, "source": src.name}) + "\n",
        encoding="utf-8",
    )
    os.chmod(marker, 0o644)
    return dest


def select_boot(paths, health=None):
    """Choisit le secours si le slot actif ne répond pas. N'arrête pas iSCSI."""
    paths.ensure_runtime()
    undo = paths.run / "rescue-undo"
    if undo.is_file():
        try:
            undo.unlink()
        except OSError:
            pass
        return _write_boot(paths, {"chosen": "slot", "undo": True, "automatic": False})
    try:
        from san_tui.release import channel_dir, load_state, run_health

        state = load_state(paths, "app")
        slot_name = state["active"]
        slot = channel_dir(paths, "app") / slot_name
        check = health or run_health
    except Exception as exc:
        return _write_boot(paths, {
            "chosen": "none",
            "automatic": True,
            "error": exc.__class__.__name__,
        })
    if check(slot) == 0:
        try:
            refresh_rescue(paths, slot)
        except Exception:
            pass
        return _write_boot(paths, {
            "chosen": "slot",
            "slot": slot_name,
            "automatic": True,
        })
    rescue = rescue_dir(paths)
    if rescue.is_dir() and check(rescue) == 0:
        return _write_boot(paths, {
            "chosen": "rescue",
            "automatic": True,
            "reason": "slot actif en échec",
        })
    return _write_boot(paths, {
        "chosen": "slot",
        "slot": slot_name,
        "automatic": True,
        "rescue_failed": True,
    })


def arm_undo(paths):
    paths.ensure_runtime()
    marker = paths.run / "rescue-undo"
    marker.write_text("undo\n", encoding="utf-8")
    os.chmod(marker, 0o644)
    return {"undo": True}
