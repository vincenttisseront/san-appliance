"""Mise à jour de l'application depuis une release GitHub.

Le téléchargement et l'empreinte sont vérifiés avant toute écriture.
Un échec laisse le slot actif, les exports iSCSI, le réseau et les données en place.
Un slot nouveau qui ne répond pas est abandonné une fois, au profit du slot précédent.
"""

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO = "vincenttisseront/san-appliance"
ASSET = "san-appliance-app.tar.gz"
ASSET_SUM = "san-appliance-app.tar.gz.sha256"
ASSET_SIG = "san-appliance-app.tar.gz.sha256.asc"
API_DEFAULT = "https://api.github.com"
MAX_BYTES = 256 * 1024 * 1024
LISTEN = ("127.0.0.1", 9753)
GITHUB_ASSET_HOSTS = {
    "github.com",
    "api.github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


class ReleaseError(Exception):
    pass


def app_dir(paths):
    return paths.conf / "app"


def state_path(paths):
    return app_dir(paths) / "state.json"


def load_state(paths):
    path = state_path(paths)
    if not path.is_file():
        return {"version": 1, "active": "a", "slots": {"a": {}, "b": {}}, "rolled_back": False}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError("état des slots illisible") from exc
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ReleaseError("état des slots refusé")
    if state.get("active") not in {"a", "b", "iso"}:
        raise ReleaseError("slot actif inconnu")
    return state


def _write_state(paths, state):
    root = app_dir(paths)
    root.mkdir(parents=True, exist_ok=True)
    path = state_path(paths)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def parse_checksum(text, filename):
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower()
        name = parts[-1].lstrip("*")
        if name != filename and not name.endswith("/" + filename):
            continue
        if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest):
            return digest
    raise ReleaseError("empreinte absente pour l'archive")


def verify_archive(blob, expected):
    if not isinstance(blob, (bytes, bytearray)):
        raise ReleaseError("archive refusée")
    if len(blob) > MAX_BYTES:
        raise ReleaseError("archive trop grande")
    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected:
        raise ReleaseError("empreinte différente")
    return digest


def _member_ok(member):
    name = member.name
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    parts = Path(name).parts
    if ".." in parts:
        return False
    return True


def extract_archive(blob, dest):
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if _member_ok(item)]
            if len(members) != len(archive.getmembers()):
                raise ReleaseError("archive refusée")
            archive.extractall(dest, members=members, filter="data")
    except ReleaseError:
        raise
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ReleaseError("archive refusée") from exc


def inactive_slot(active):
    if active == "a":
        return "b"
    if active in {"b", "iso"}:
        return "a"
    raise ReleaseError("slot actif inconnu")


def run_health(slot_dir):
    script = Path(slot_dir) / "health"
    if not script.is_file():
        return 1
    try:
        completed = subprocess.run(
            [str(script)],
            cwd=str(slot_dir),
            timeout=20,
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1
    return completed.returncode


def apply_verified(paths, archive, checksum_text, health=None, writing=False):
    """Vérifie l'empreinte, écrit seulement le slot inactif, puis bascule une fois."""
    expected = parse_checksum(checksum_text, ASSET)
    digest = verify_archive(archive, expected)
    if writing:
        current = load_state(paths)["active"]
        result = {
            "applied": False,
            "slot": current,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "reason": "session iSCSI en écriture",
        }
        append_audit(paths, result, "reporté")
        return result
    check = health or run_health
    state = load_state(paths)
    current = state["active"]
    target = inactive_slot(current)
    root = app_dir(paths)
    staging = root / (target + ".staging")
    final = root / target
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        extract_archive(archive, staging)
    except ReleaseError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)
    health_script = final / "health"
    if health_script.is_file():
        os.chmod(health_script, 0o755)
    updated = {
        "version": 1,
        "active": target,
        "rolled_back": False,
        "slots": {
            "a": dict((state.get("slots") or {}).get("a") or {}),
            "b": dict((state.get("slots") or {}).get("b") or {}),
        },
    }
    updated["slots"][target] = {"sha256": digest}
    _write_state(paths, updated)
    try:
        healthy = check(final) == 0
    except Exception:
        healthy = False
    if healthy:
        result = {
            "applied": True,
            "slot": target,
            "previous": current,
            "rollback": False,
            "sha256": digest,
        }
        append_audit(paths, result, "appliqué")
        return result
    updated["active"] = current
    updated["rolled_back"] = True
    _write_state(paths, updated)
    result = {
        "applied": False,
        "slot": current,
        "previous": current,
        "rollback": True,
        "sha256": digest,
    }
    append_audit(paths, result, "retour")
    return result


def allowed_api(api):
    if not api:
        return API_DEFAULT
    parsed = urlparse(api)
    if parsed.scheme == "https" and parsed.hostname == "api.github.com":
        return api.rstrip("/")
    if parsed.scheme == "http" and parsed.hostname == "127.0.0.1":
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None and 8000 <= port <= 8999:
            return api.rstrip("/")
    raise ReleaseError("adresse de release refusée")


def allowed_download(url, api):
    parsed = urlparse(url)
    api_host = urlparse(api).hostname
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        return False
    if parsed.hostname == api_host:
        return True
    return parsed.scheme == "https" and parsed.hostname in GITHUB_ASSET_HOSTS


def read_token(path):
    file = Path(path)
    if not file.is_file():
        return ""
    mode = file.stat().st_mode & 0o077
    if mode != 0:
        raise ReleaseError("jeton GitHub trop ouvert")
    return file.read_text(encoding="utf-8").strip()


def _request(url, token, opener, accept, timeout=60):
    if token and token in url:
        raise ReleaseError("téléchargement refusé")
    headers = {"User-Agent": "san-appliance", "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError("téléchargement refusé") from exc
    if len(data) > MAX_BYTES:
        raise ReleaseError("archive trop grande")
    return data


def fetch_release(api, tag, token, opener, timeout=60):
    base = allowed_api(api)
    if tag:
        if not all(char.isalnum() or char in "._-" for char in tag) or len(tag) > 64:
            raise ReleaseError("version refusée")
        meta_url = "{}/repos/{}/releases/tags/{}".format(base, REPO, tag)
    else:
        meta_url = "{}/repos/{}/releases/latest".format(base, REPO)
    raw = _request(meta_url, token, opener, "application/vnd.github+json", timeout)
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release illisible") from exc
    assets = {}
    for item in meta.get("assets") or []:
        if isinstance(item, dict) and item.get("name") and item.get("browser_download_url"):
            assets[item["name"]] = item["browser_download_url"]
    if ASSET not in assets or ASSET_SUM not in assets:
        raise ReleaseError("archive ou empreinte absente de la release")
    names = [ASSET, ASSET_SUM]
    if ASSET_SIG in assets:
        names.append(ASSET_SIG)
    for name in names:
        if not allowed_download(assets[name], base):
            raise ReleaseError("adresse de téléchargement refusée")
    archive = _request(assets[ASSET], token, opener, "application/octet-stream", timeout)
    checksum = _request(assets[ASSET_SUM], token, opener, "text/plain", timeout)
    try:
        text = checksum.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("empreinte illisible") from exc
    signature = None
    if ASSET_SIG in assets:
        try:
            signature = _request(assets[ASSET_SIG], token, opener, "text/plain", timeout).decode("utf-8")
        except UnicodeError as exc:
            raise ReleaseError("signature illisible") from exc
    return archive, text, signature


def fetch_and_apply(paths, tag="", api="", token="", opener=None, health=None, writing=False, pubkey=""):
    client = opener or urllib.request.build_opener()
    try:
        archive, checksum, signature = fetch_release(api, tag, token, client)
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("téléchargement refusé") from exc
    from san_tui.evolve import signature_decision
    has_key = bool(pubkey) and Path(pubkey).is_file()
    verified = True
    if signature and has_key:
        verified = _gpg_verified(checksum, signature, pubkey)
    decision = signature_decision(bool(signature), has_key, verified)
    if decision["blocked"]:
        raise ReleaseError("signature refusée")
    result = apply_verified(paths, archive, checksum, health=health, writing=writing)
    if decision["gap"]:
        result = dict(result)
        result["signature"] = "non vérifiée"
        append_audit(paths, {"slot": result.get("slot"), "sha256": result.get("sha256"), "reason": decision["gap"]}, "signature")
    return result


def handle_request(paths, payload, token_file="/etc/san/github.token"):
    if not isinstance(payload, dict):
        raise ReleaseError("demande refusée")
    if payload.get("op") == "rollback":
        return rollback_slot(paths)
    tag = payload.get("tag") or ""
    api = payload.get("api") or ""
    if not isinstance(tag, str) or not isinstance(api, str):
        raise ReleaseError("demande refusée")
    from san_tui.evolve import session_is_writing
    writing, why = session_is_writing(read_session_rows("/"))
    if writing:
        result = {"applied": False, "rollback": False, "reason": why}
        append_audit(paths, result, "reporté")
        return result
    token = read_token(token_file) if Path(token_file).is_file() else ""
    result = fetch_and_apply(
        paths, tag=tag, api=api, token=token, pubkey="/etc/san/release-pubkey.asc",
    )
    if token and token in json.dumps(result):
        raise ReleaseError("demande refusée")
    return result


def serve(paths, token_file="/etc/san/github.token"):
    host, port = LISTEN
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    bound = sock.getsockname()
    if bound[0] != "127.0.0.1":
        sock.close()
        raise ReleaseError("écoute hors loopback")
    sock.listen(4)
    while True:
        conn, addr = sock.accept()
        if addr[0] != "127.0.0.1":
            conn.close()
            continue
        try:
            raw = b""
            while b"\n" not in raw and len(raw) < 8192:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                raw += chunk
            line = raw.split(b"\n", 1)[0]
            try:
                payload = json.loads(line.decode("utf-8")) if line else {}
                result = handle_request(paths, payload, token_file=token_file)
            except ReleaseError as exc:
                result = {"applied": False, "rollback": False, "error": str(exc)}
            body = json.dumps(result).encode("utf-8") + b"\n"
            conn.sendall(body)
        finally:
            conn.close()


def append_audit(paths, event, result):
    """Journal de mise à jour sans secret. Un échec d'écriture ne bloque pas iSCSI."""
    try:
        from datetime import datetime, timezone
        from san_tui.evolve import safe_audit
        payload = dict(event or {})
        payload["result"] = result
        row = safe_audit(payload)
        row["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        path = app_dir(paths) / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.chmod(path, 0o644)
    except Exception:
        return


def image_dir(paths):
    return Path(paths.root) / "usr" / "lib" / "san-app" / "image"


def installed_version(paths):
    active = "iso"
    try:
        active = load_state(paths).get("active") or "iso"
    except ReleaseError:
        active = "iso"
    if active == "iso":
        path = image_dir(paths) / "version"
    else:
        path = app_dir(paths) / active / "version"
        if not path.is_file():
            path = image_dir(paths) / "version"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def archive_version(blob):
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            member = next((item for item in archive.getmembers() if item.name == "version"), None)
            if member is None:
                return ""
            extracted = archive.extractfile(member)
            if extracted is None:
                return ""
            return extracted.read(64).decode("utf-8", "replace").strip()
    except (tarfile.TarError, OSError):
        return ""


def read_session_rows(root):
    base = Path(root) / "sys" / "kernel" / "config" / "target" / "iscsi"
    rows = []
    if not base.is_dir():
        return rows
    for session in base.glob("*/*/acls/*/sessions/*"):
        if not session.is_dir():
            continue
        out = session / "out_cmds"
        if not out.is_file():
            rows.append({"present": True, "out_cmds": None})
            continue
        text = ""
        try:
            text = out.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        rows.append({"present": True, "out_cmds": int(text) if text.isdigit() else None})
    return rows


def _gpg_verified(checksum_text, signature, pubkey):
    if not signature or not pubkey or not Path(pubkey).is_file():
        return False
    try:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "checksum"
            sig = root / "checksum.asc"
            data.write_text(checksum_text, encoding="utf-8")
            sig.write_text(signature, encoding="utf-8")
            imported = subprocess.run(
                ["gpg", "--homedir", str(root), "--batch", "--import", str(pubkey)],
                capture_output=True, check=False,
            )
            if imported.returncode != 0:
                return False
            checked = subprocess.run(
                ["gpg", "--homedir", str(root), "--batch", "--verify", str(sig), str(data)],
                capture_output=True, check=False,
            )
            return checked.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def rollback_slot(paths, health=None):
    """Un pas en arrière : b vers a, puis a vers l'image ISO. N'efface pas les slots."""
    state = load_state(paths)
    active = state.get("active")
    if active == "b":
        target = "a"
        dest = app_dir(paths) / "a"
    elif active == "a":
        target = "iso"
        dest = image_dir(paths)
    else:
        return {"applied": False, "rollback": False, "slot": active, "reason": "image ISO déjà active"}
    if not dest.is_dir():
        return {"applied": False, "rollback": False, "slot": active, "reason": "slot précédent absent"}
    check = health or run_health
    try:
        healthy = check(dest) == 0
    except Exception:
        healthy = False
    if not healthy:
        return {"applied": False, "rollback": False, "slot": active, "reason": "slot précédent malsain"}
    state["active"] = target
    state["rolled_back"] = True
    _write_state(paths, state)
    result = {"applied": False, "rollback": True, "slot": target, "previous": active}
    append_audit(paths, result, "retour")
    return result


def boot_apply(paths, opener, token="", api="", pubkey="/etc/san/release-pubkey.asc", sessions=None):
    """Applique une release plus récente. Un échec laisse iSCSI tel quel."""
    from san_tui.evolve import is_newer, session_is_writing, signature_decision
    try:
        rows = read_session_rows(paths.root) if sessions is None else sessions
        writing, why = session_is_writing(rows)
        if writing:
            result = {"applied": False, "rollback": False, "reason": why}
            append_audit(paths, result, "reporté")
            return result
        archive, checksum, signature = fetch_release(api, "", token, opener or urllib.request.build_opener(), timeout=20)
        version = archive_version(archive)
        if not is_newer(installed_version(paths), version):
            result = {"applied": False, "rollback": False, "reason": "pas plus récent"}
            append_audit(paths, result, "ignoré")
            return result
        has_key = bool(pubkey) and Path(pubkey).is_file()
        verified = _gpg_verified(checksum, signature, pubkey) if signature and has_key else True
        decision = signature_decision(bool(signature), has_key, verified)
        if decision["blocked"]:
            result = {"applied": False, "rollback": False, "reason": "signature refusée"}
            append_audit(paths, result, "refusé")
            return result
        result = apply_verified(paths, archive, checksum)
        if decision["gap"]:
            result = dict(result)
            result["signature"] = "non vérifiée"
            append_audit(paths, {"slot": result.get("slot"), "sha256": result.get("sha256"), "reason": decision["gap"]}, "signature")
        paths.ensure_runtime()
        (paths.run / "release-applied.json").write_text(
            json.dumps({
                "applied": bool(result.get("applied")),
                "previous": result.get("previous"),
                "slot": result.get("slot"),
            }) + "\n",
            encoding="utf-8",
        )
        return result
    except ReleaseError as exc:
        result = {"applied": False, "rollback": False, "reason": str(exc)}
        append_audit(paths, result, "refusé")
        return result
    except Exception:
        result = {"applied": False, "rollback": False, "reason": "téléchargement refusé"}
        append_audit(paths, result, "refusé")
        return result


def post_apply_health(paths, listeners, health=None):
    """Le portail iSCSI fait partie du contrôle. Le retour ne démonte pas la cible."""
    from san_tui.evolve import portal_problem
    marker_path = paths.run / "release-applied.json"
    if not marker_path.is_file():
        return {"ok": True, "reason": "aucune release appliquée", "iscsi": "inchangé"}
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": True, "reason": "marqueur illisible", "iscsi": "inchangé"}
    if not marker.get("applied"):
        return {"ok": True, "reason": "release non basculée", "iscsi": "inchangé"}
    portals = []
    exports_path = paths.conf / "exports.json"
    if exports_path.is_file():
        try:
            document = json.loads(exports_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
        for target in document.get("targets") or []:
            portal = target.get("portal") or {}
            if portal.get("address"):
                portals.append((portal["address"], int(portal.get("port") or 3260)))
    problem = portal_problem(listeners, portals)
    if not problem:
        return {"ok": True, "iscsi": "inchangé"}
    check = health or run_health
    previous = marker.get("previous") if marker.get("previous") in {"a", "b", "iso"} else "a"
    state = load_state(paths)
    state["active"] = previous
    state["rolled_back"] = True
    _write_state(paths, state)
    previous_dir = image_dir(paths) if previous == "iso" else app_dir(paths) / previous
    try:
        previous_ok = check(previous_dir) == 0
    except Exception:
        previous_ok = False
    slot = previous
    if not previous_ok:
        image = image_dir(paths)
        try:
            image_ok = image.is_dir() and check(image) == 0
        except Exception:
            image_ok = False
        if image_ok:
            state["active"] = "iso"
            _write_state(paths, state)
            slot = "iso"
    append_audit(paths, {"slot": slot, "reason": problem}, "retour")
    return {"ok": False, "reason": problem, "slot": slot, "iscsi": "inchangé"}
