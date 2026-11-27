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
    if state.get("active") not in {"a", "b"}:
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
    if active == "b":
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


def apply_verified(paths, archive, checksum_text, health=None):
    """Vérifie l'empreinte, écrit seulement le slot inactif, puis bascule une fois."""
    expected = parse_checksum(checksum_text, ASSET)
    digest = verify_archive(archive, expected)
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
        return {
            "applied": True,
            "slot": target,
            "previous": current,
            "rollback": False,
            "sha256": digest,
        }
    updated["active"] = current
    updated["rolled_back"] = True
    _write_state(paths, updated)
    return {
        "applied": False,
        "slot": current,
        "previous": current,
        "rollback": True,
        "sha256": digest,
    }


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


def _request(url, token, opener, accept):
    if token and token in url:
        raise ReleaseError("téléchargement refusé")
    headers = {"User-Agent": "san-appliance", "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=60) as response:
            data = response.read(MAX_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseError("téléchargement refusé") from exc
    if len(data) > MAX_BYTES:
        raise ReleaseError("archive trop grande")
    return data


def fetch_release(api, tag, token, opener):
    base = allowed_api(api)
    if tag:
        if not all(char.isalnum() or char in "._-" for char in tag) or len(tag) > 64:
            raise ReleaseError("version refusée")
        meta_url = "{}/repos/{}/releases/tags/{}".format(base, REPO, tag)
    else:
        meta_url = "{}/repos/{}/releases/latest".format(base, REPO)
    raw = _request(meta_url, token, opener, "application/vnd.github+json")
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
    for name in (ASSET, ASSET_SUM):
        if not allowed_download(assets[name], base):
            raise ReleaseError("adresse de téléchargement refusée")
    archive = _request(assets[ASSET], token, opener, "application/octet-stream")
    checksum = _request(assets[ASSET_SUM], token, opener, "text/plain")
    try:
        text = checksum.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("empreinte illisible") from exc
    return archive, text


def rollback_slot(paths):
    """Remet le pointeur sur l'autre slot, une fois. N'efface ni données ni exports."""
    state = load_state(paths)
    current = state["active"]
    if state.get("rolled_back"):
        return {
            "rollback": False,
            "slot": current,
            "reason": "déjà sur le slot précédent",
        }
    previous = inactive_slot(current)
    slot_dir = app_dir(paths) / previous
    if not slot_dir.is_dir() or not any(slot_dir.iterdir()):
        raise ReleaseError("slot précédent absent")
    updated = {
        "version": 1,
        "active": previous,
        "rolled_back": True,
        "previous": current,
        "slots": {
            "a": dict((state.get("slots") or {}).get("a") or {}),
            "b": dict((state.get("slots") or {}).get("b") or {}),
        },
    }
    _write_state(paths, updated)
    return {"rollback": True, "slot": previous, "previous": current}


def fetch_and_apply(paths, tag="", api="", token="", opener=None, health=None):
    client = opener or urllib.request.build_opener()
    try:
        archive, checksum = fetch_release(api, tag, token, client)
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("téléchargement refusé") from exc
    return apply_verified(paths, archive, checksum, health=health)


def handle_request(paths, payload, token_file="/etc/san/github.token"):
    if not isinstance(payload, dict):
        raise ReleaseError("demande refusée")
    tag = payload.get("tag") or ""
    api = payload.get("api") or ""
    if not isinstance(tag, str) or not isinstance(api, str):
        raise ReleaseError("demande refusée")
    token = read_token(token_file) if Path(token_file).is_file() else ""
    result = fetch_and_apply(paths, tag=tag, api=api, token=token)
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
