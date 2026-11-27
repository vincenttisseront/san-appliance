"""NTP au démarrage. Un serveur injoignable laisse le journal local s'écrire."""

import os
import socket
from pathlib import Path


def local_log(path, message):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def probe_ntp(server, timeout):
    packet = bytes([0x1B]) + bytes(47)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(packet, (server, 123))
        data, _addr = sock.recvfrom(128)
        return len(data) >= 48
    except OSError:
        return False
    finally:
        sock.close()


def timesyncd_dropin(server):
    return (
        "[Time]\n"
        "NTP={}\n"
        "FallbackNTP=0.ubuntu.pool.ntp.org 1.ubuntu.pool.ntp.org\n"
    ).format(server)


def apply_ntp(paths, server="ntp.ubuntu.com", timeout=1.0, log_path=None):
    """Écrit le journal local avant l'essai NTP. Ne lève pas si le réseau manque."""
    log_path = Path(log_path) if log_path else paths.root / "var" / "log" / "san" / "local.log"
    local_log(log_path, "journal local")
    if (paths.etc / "ntp-undo").is_file():
        local_log(log_path, "ntp annulé")
        return {"ntp": False, "undo": True, "logged": True}
    try:
        reached = probe_ntp(server, timeout)
    except Exception:
        reached = False
    local_log(log_path, "ntp ok" if reached else "ntp injoignable")
    try:
        dropin = paths.root / "etc" / "systemd" / "timesyncd.conf.d"
        dropin.mkdir(parents=True, exist_ok=True)
        conf = dropin / "san.conf"
        conf.write_text(timesyncd_dropin(server), encoding="utf-8")
        os.chmod(conf, 0o644)
    except OSError:
        pass
    return {"ntp": bool(reached), "undo": False, "logged": True}


def undo_ntp(paths):
    paths.ensure_runtime()
    marker = paths.etc / "ntp-undo"
    marker.write_text("undo\n", encoding="utf-8")
    os.chmod(marker, 0o644)
    return {"undo": True}
