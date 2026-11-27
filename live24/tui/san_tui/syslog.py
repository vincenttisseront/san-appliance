"""Syslog distant optionnel.

S'il n'est pas configuré, ou s'il ne répond pas, les journaux restent locaux
et la fonction retourne sans erreur. Elle ne touche pas aux exports iSCSI.
Le fichier syslog.off annule le choix automatique.
"""

import json
import shutil
import socket
import subprocess
from pathlib import Path


def load_target(paths):
    """None si le distant est absent, refusé, ou annulé. N'élève pas."""
    if _disabled(paths):
        return None
    for candidate in (paths.conf / "syslog.json", paths.etc / "syslog.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        if any(key in data for key in ("password", "secret", "token", "key")):
            return None
        host = str(data.get("host") or "").strip()
        port = data.get("port") if data.get("port") is not None else 514
        if not _host_ok(host) or not _port_ok(port):
            return None
        return {"host": host, "port": int(port), "proto": "udp"}
    return None


def reachable(host, port, timeout=1.0):
    """True si l'hôte répond. Un délai ou une résolution impossible vaut False.

    Un refus TCP compte comme une réponse : l'hôte est là. Un trou noir, non.
    """
    try:
        socket.getaddrinfo(str(host), int(port), type=socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        sock = socket.create_connection((str(host), int(port)), timeout=timeout)
    except ConnectionRefusedError:
        return True
    except OSError:
        return False
    else:
        sock.close()
        return True


def apply_remote(paths, reach=None, install=None):
    from san_tui.alerts import add_alert

    reach = reach or reachable
    install = install or install_forward
    if _disabled(paths):
        _quiet(install, None)
        _mark(paths, "san-syslog: local")
        return {"remote": False, "local": True, "reason": "local demandé"}
    target = load_target(paths)
    if target is None:
        _quiet(install, None)
        _mark(paths, "san-syslog: local")
        return {"remote": False, "local": True, "reason": "aucun syslog distant"}
    try:
        up = bool(reach(target["host"], target["port"]))
    except Exception:
        up = False
    if not up:
        _quiet(install, None)
        add_alert(paths, "error", "syslog distant injoignable, journaux locaux conservés")
        _mark(paths, "san-syslog: local")
        return {
            "remote": False,
            "local": True,
            "reason": "injoignable",
            "host": target["host"],
        }
    try:
        install(target)
    except Exception:
        _quiet(install, None)
        add_alert(paths, "error", "syslog distant non installé, journaux locaux conservés")
        _mark(paths, "san-syslog: local")
        return {"remote": False, "local": True, "reason": "installation refusée"}
    _mark(paths, "san-syslog: remote")
    return {"remote": True, "local": True, "host": target["host"], "port": target["port"]}


def disable_remote(paths, install=None):
    """Annule le relais automatique. Les journaux restent locaux."""
    install = install or install_forward
    paths.ensure_runtime()
    if paths.conf.is_dir():
        dest = paths.conf / "syslog.off"
    else:
        paths.etc.mkdir(parents=True, exist_ok=True)
        dest = paths.etc / "syslog.off"
    dest.write_text("local\n", encoding="utf-8")
    _quiet(install, None)
    _mark(paths, "san-syslog: local")
    return {"remote": False, "local": True, "reason": "local demandé"}


def install_forward(target):
    path = Path("/etc/rsyslog.d/99-san-remote.conf")
    if target is None:
        if path.is_file():
            path.unlink()
        _restart()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config(target), encoding="utf-8")
    _restart()


def _config(target):
    # La file lâche le message si le distant est plein ou absent.
    # Les règles locales de rsyslog et journald restent en place.
    return (
        'action(type="omfwd" target="{host}" port="{port}" protocol="udp"\n'
        '  queue.type="LinkedList" queue.size="1000" queue.timeoutEnqueue="0"\n'
        '  action.resumeRetryCount="1" action.reportSuspension="on")\n'
    ).format(host=target["host"], port=int(target["port"]))


def _restart():
    if not shutil.which("systemctl") or not shutil.which("rsyslogd"):
        return
    subprocess.run(
        ["systemctl", "try-restart", "rsyslog.service"],
        check=False,
        timeout=8,
    )


def _disabled(paths):
    return (paths.conf / "syslog.off").is_file() or (paths.etc / "syslog.off").is_file()


def _quiet(install, target):
    try:
        install(target)
    except Exception:
        return


def _host_ok(host):
    if not host or len(host) > 253:
        return False
    if any(ch.isspace() or ch in '{}"\'\\/;' for ch in host):
        return False
    return True


def _port_ok(port):
    try:
        value = int(port)
    except (TypeError, ValueError):
        return False
    return 1 <= value <= 65535


def _mark(paths, line):
    if paths.root != Path("/"):
        return
    device = Path("/dev/ttyS0")
    if not device.exists():
        return
    try:
        fd = device.open("wb", buffering=0)
    except OSError:
        return
    try:
        fd.write((line + "\n").encode())
    except OSError:
        pass
    finally:
        fd.close()
