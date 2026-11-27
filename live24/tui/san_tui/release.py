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
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO = "vincenttisseront/san-appliance"
ASSET = "san-appliance-app.tar.gz"
ASSET_SUM = "san-appliance-app.tar.gz.sha256"
API_DEFAULT = "https://api.github.com"
MAX_BYTES = 256 * 1024 * 1024
WRITING_TTL = 120
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


def writing_marker(paths):
    return paths.root / "var" / "lib" / "san" / "webui" / "writing"


def session_is_writing(paths, now=None):
    """True seulement pendant une écriture WebUI récente. Un fichier ancien n'arrête rien."""
    path = writing_marker(paths)
    if not path.is_file():
        return False
    try:
        stamped = path.stat().st_mtime
    except OSError:
        return False
    moment = time.time() if now is None else now
    return moment - stamped < WRITING_TTL


def utc_stamp(clock=None):
    if clock is not None:
        return clock()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_version_text(directory):
    path = Path(directory) / "version"
    if not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    line = text.strip().splitlines()
    return line[0].strip() if line else ""


def parse_version(text):
    if not text:
        return None
    raw = text[1:] if text[:1] in ("v", "V") else text
    numbers = []
    for part in raw.split("."):
        if not part.isdigit():
            return None
        numbers.append(int(part))
    return tuple(numbers) if numbers else None


def version_older(incoming, current):
    new = parse_version(incoming)
    old = parse_version(current)
    if new is None or old is None:
        return False
    return new < old


def version_same(incoming, current):
    new = parse_version(incoming)
    old = parse_version(current)
    if new is None or old is None:
        return False
    return new == old


def _files(directory):
    found = {}
    root = Path(directory)
    if not root.is_dir():
        return found
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        found[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def tree_diff(old, new):
    before = _files(old)
    after = _files(new)
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": sorted(name for name in set(before) & set(after) if before[name] != after[name]),
    }


def render_diff(diff):
    lines = []
    for name in diff.get("added") or []:
        lines.append("+ {}".format(name))
    for name in diff.get("removed") or []:
        lines.append("- {}".format(name))
    for name in diff.get("changed") or []:
        lines.append("~ {}".format(name))
    return ("\n".join(lines) + "\n") if lines else ""


def write_diff(paths, diff):
    root = app_dir(paths)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "diff.txt"
    tmp = path.with_suffix(".txt.tmp")
    tmp.write_text(render_diff(diff), encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return path


def newest_slot(state):
    best = None
    stamp = ""
    for name in ("a", "b"):
        info = (state.get("slots") or {}).get(name) or {}
        installed = info.get("installed") or ""
        if installed > stamp:
            best = name
            stamp = installed
    return best


def release_status(paths):
    try:
        state = load_state(paths)
    except ReleaseError as exc:
        return {"active": None, "newest": None, "slots": {}, "diff": "", "error": str(exc)}
    diff_path = app_dir(paths) / "diff.txt"
    diff = ""
    if diff_path.is_file():
        try:
            diff = diff_path.read_text(encoding="utf-8")
        except OSError:
            diff = ""
    return {
        "active": state.get("active"),
        "newest": newest_slot(state),
        "slots": state.get("slots") or {},
        "diff": diff,
    }


def undo_slot(paths):
    """Revenir au slot précédent. C'est le seul geste manuel : il annule l'application automatique."""
    state = load_state(paths)
    current = state["active"]
    previous = inactive_slot(current)
    info = (state.get("slots") or {}).get(previous) or {}
    slot_dir = app_dir(paths) / previous
    if not slot_dir.is_dir() and not info.get("sha256"):
        raise ReleaseError("pas de slot précédent")
    state["active"] = previous
    state["rolled_back"] = True
    _write_state(paths, state)
    return {
        "applied": False,
        "undo": True,
        "rollback": True,
        "slot": previous,
        "previous": current,
    }


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


def _kept(current, digest, diff, reason):
    return {
        "applied": False,
        "deferred": False,
        "slot": current,
        "previous": current,
        "rollback": False,
        "sha256": digest,
        "diff": render_diff(diff),
        "reason": reason,
    }


def apply_verified(paths, archive, checksum_text, health=None, clock=None):
    """Vérifie l'empreinte, montre le diff, puis bascule si personne n'écrit.

    Une archive trop grande, trop ancienne, ou une empreinte fausse laisse le slot actif.
    Le diff est écrit avant le changement de pointeur. Il ne demande pas de confirmation.
    """
    expected = parse_checksum(checksum_text, ASSET)
    digest = verify_archive(archive, expected)
    check = health or run_health
    state = load_state(paths)
    current = state["active"]
    target = inactive_slot(current)
    root = app_dir(paths)
    staging = root / (target + ".staging")
    final = root / target
    active_dir = root / current
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        extract_archive(archive, staging)
    except ReleaseError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    incoming = read_version_text(staging)
    active_version = read_version_text(active_dir)
    if version_older(incoming, active_version):
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseError("release plus ancienne que le slot actif")
    diff = tree_diff(active_dir, staging)
    write_diff(paths, diff)
    if session_is_writing(paths):
        shutil.rmtree(staging, ignore_errors=True)
        result = _kept(current, digest, diff, "session en écriture")
        result["deferred"] = True
        return result
    if version_same(incoming, active_version):
        shutil.rmtree(staging, ignore_errors=True)
        return _kept(current, digest, diff, "déjà en place")
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)
    health_script = final / "health"
    if health_script.is_file():
        os.chmod(health_script, 0o755)
    try:
        healthy = check(final) == 0
    except Exception:
        healthy = False
    stamp = utc_stamp(clock)
    updated = {
        "version": 1,
        "active": target if healthy else current,
        "rolled_back": not healthy,
        "slots": {
            "a": dict((state.get("slots") or {}).get("a") or {}),
            "b": dict((state.get("slots") or {}).get("b") or {}),
        },
    }
    updated["slots"][target] = {
        "sha256": digest,
        "installed": stamp,
        "version": incoming,
    }
    _write_state(paths, updated)
    if healthy:
        return {
            "applied": True,
            "deferred": False,
            "slot": target,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "installed": stamp,
            "newest": newest_slot(updated),
            "diff": render_diff(diff),
        }
    return {
        "applied": False,
        "deferred": False,
        "slot": current,
        "previous": current,
        "rollback": True,
        "sha256": digest,
        "diff": render_diff(diff),
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


def _content_length(response):
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Content-Length")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _request(url, token, opener, accept):
    if token and token in url:
        raise ReleaseError("téléchargement refusé")
    headers = {"User-Agent": "san-appliance", "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=60) as response:
            announced = _content_length(response)
            if announced is not None and announced > MAX_BYTES:
                raise ReleaseError("archive trop grande")
            data = response.read(MAX_BYTES + 1)
    except ReleaseError:
        raise
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


def fetch_and_apply(paths, tag="", api="", token="", opener=None, health=None, clock=None):
    client = opener or urllib.request.build_opener()
    try:
        archive, checksum = fetch_release(api, tag, token, client)
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("téléchargement refusé") from exc
    return apply_verified(paths, archive, checksum, health=health, clock=clock)


def public_asset_status(opener=None, timeout=15):
    """Dit si la release publique GitHub contient l'archive et son empreinte."""
    client = opener or urllib.request.build_opener()
    url = "{}/repos/{}/releases/latest".format(API_DEFAULT, REPO)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "san-appliance", "Accept": "application/vnd.github+json"},
    )
    try:
        with client.open(req, timeout=timeout) as response:
            raw = response.read(1024 * 1024)
            code = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"present": False, "reason": "aucune release publique", "status": 404}
        return {"present": False, "reason": "API release HTTP {}".format(exc.code), "status": exc.code}
    except (OSError, urllib.error.URLError):
        return {"present": False, "reason": "API release injoignable", "status": 0}
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {"present": False, "reason": "release illisible", "status": code}
    names = []
    for item in meta.get("assets") or []:
        if isinstance(item, dict) and item.get("name"):
            names.append(item["name"])
    if ASSET in names and ASSET_SUM in names:
        return {
            "present": True,
            "reason": "",
            "status": code,
            "tag": meta.get("tag_name") or "",
        }
    return {
        "present": False,
        "reason": "archive publique absente",
        "status": code,
        "assets": names,
    }


def handle_request(paths, payload, token_file="/etc/san/github.token"):
    if not isinstance(payload, dict):
        raise ReleaseError("demande refusée")
    if payload.get("undo") is True:
        return undo_slot(paths)
    if payload.get("status") is True:
        return release_status(paths)
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
