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
KERNEL_ASSET = "san-appliance-kernel.tar.gz"
KERNEL_ASSET_SUM = "san-appliance-kernel.tar.gz.sha256"
CHANNELS = {
    "app": {"asset": ASSET, "sum": ASSET_SUM},
    "kernel": {"asset": KERNEL_ASSET, "sum": KERNEL_ASSET_SUM},
}
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


def channel_dir(paths, channel="app"):
    if channel not in CHANNELS:
        raise ReleaseError("canal refusé")
    return paths.conf / channel


def app_dir(paths):
    return channel_dir(paths, "app")


def state_path(paths, channel="app"):
    return channel_dir(paths, channel) / "state.json"


def load_state(paths, channel="app"):
    path = state_path(paths, channel)
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


def _write_state(paths, state, channel="app"):
    root = channel_dir(paths, channel)
    root.mkdir(parents=True, exist_ok=True)
    path = state_path(paths, channel)
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


def _resume(gate, frozen):
    if not frozen:
        return
    try:
        gate.resume()
    except Exception:
        try:
            gate.resume()
        except Exception:
            return


def apply_verified(paths, archive, checksum_text, health=None, gate=None, bench=None,
                   signer=None, channel="app", asset=None, pin=None):
    """Vérifie l'empreinte, gèle les exports le temps de l'écriture, puis les rouvre.

    Le slot ne devient actif que si le banc fio passe et si le slot répond.
    Le canal noyau n'accepte qu'une version 6.8, au plus celle suivie par doctor.
    """
    if channel not in CHANNELS:
        raise ReleaseError("canal refusé")
    asset_name = asset or CHANNELS[channel]["asset"]
    expected = parse_checksum(checksum_text, asset_name)
    digest = verify_archive(archive, expected)
    kernel_version = None
    if channel == "kernel":
        from san_tui.kernelpin import kernel_allowed, kernel_release_of

        kernel_version = kernel_release_of(archive)
        if not kernel_allowed(kernel_version, pin if pin is not None else _installed_pin(paths)):
            raise ReleaseError("noyau hors 6.8")
    check = health or run_health
    measure = bench or _default_bench
    sign = signer or _default_sign
    state = load_state(paths, channel)
    current = state["active"]
    active_sha = ((state.get("slots") or {}).get(current) or {}).get("sha256")
    if active_sha and active_sha == digest:
        return {
            "applied": False,
            "unchanged": True,
            "slot": current,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "channel": channel,
        }
    measured = measure()
    if not isinstance(measured, dict) or not measured.get("pass"):
        raise ReleaseError("banc fio refusé")
    use_gate = gate if gate is not None else _null_gate()
    frozen = False
    if channel == "app":
        try:
            use_gate.freeze()
            frozen = True
        except Exception as exc:
            _resume(use_gate, True)
            raise ReleaseError("gel des exports impossible") from exc
    try:
        return _commit_slot(
            paths, archive, state, digest, measured, check, sign, channel, kernel_version,
        )
    finally:
        _resume(use_gate, frozen)


def _null_gate():
    from san_tui.fabric import NullGate
    return NullGate()


def _default_bench():
    from san_tui.promote import fio_bench
    return fio_bench()


def _default_sign(paths, digest, measured):
    from san_tui.promote import sign_promotion
    return sign_promotion(paths, digest, measured)


def _installed_pin(paths):
    path = paths.root / "usr" / "share" / "san" / "kernel-pin"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _commit_slot(paths, archive, state, digest, measured, check, sign, channel, kernel_version):
    current = state["active"]
    target = inactive_slot(current)
    root = channel_dir(paths, channel)
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
    slot_record = {"sha256": digest, "p99_us": measured.get("p99_us")}
    if kernel_version:
        slot_record["kernel"] = kernel_version
    updated = {
        "version": 1,
        "active": target,
        "rolled_back": False,
        "previous": current,
        "slots": {
            "a": dict((state.get("slots") or {}).get("a") or {}),
            "b": dict((state.get("slots") or {}).get("b") or {}),
        },
    }
    updated["slots"][target] = slot_record
    try:
        healthy = check(final) == 0
    except Exception:
        healthy = False
    if not healthy:
        updated["active"] = current
        updated["previous"] = state.get("previous")
        updated["rolled_back"] = True
        _write_state(paths, updated, channel)
        return {
            "applied": False,
            "slot": current,
            "previous": current,
            "rollback": True,
            "sha256": digest,
            "channel": channel,
            "p99_us": measured.get("p99_us"),
        }
    signature, gap = "", ""
    try:
        signature, gap = sign(paths, digest, measured)
    except Exception:
        gap = "signature indisponible"
    if signature:
        updated["signature"] = signature
    elif gap:
        updated["signing_gap"] = gap
    _write_state(paths, updated, channel)
    if channel == "app":
        try:
            from san_tui.rescue import refresh_rescue
            refresh_rescue(paths, final)
        except Exception:
            pass
    return {
        "applied": True,
        "slot": target,
        "previous": current,
        "rollback": False,
        "sha256": digest,
        "channel": channel,
        "p99_us": measured.get("p99_us"),
        "signed": bool(signature),
        "signing_gap": gap,
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


def fetch_release(api, tag, token, opener, asset=ASSET, asset_sum=ASSET_SUM):
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
    if asset not in assets or asset_sum not in assets:
        raise ReleaseError("archive ou empreinte absente de la release")
    for name in (asset, asset_sum):
        if not allowed_download(assets[name], base):
            raise ReleaseError("adresse de téléchargement refusée")
    archive = _request(assets[asset], token, opener, "application/octet-stream")
    checksum = _request(assets[asset_sum], token, opener, "text/plain")
    try:
        text = checksum.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("empreinte illisible") from exc
    return archive, text


def fetch_and_apply(paths, tag="", api="", token="", opener=None, health=None,
                    channel="app", gate=None, bench=None, signer=None, pin=None):
    if tag:
        raise ReleaseError("la mise à jour est automatique")
    spec = CHANNELS.get(channel)
    if spec is None:
        raise ReleaseError("canal refusé")
    client = opener or urllib.request.build_opener()
    try:
        archive, checksum = fetch_release(
            api, "", token, client, asset=spec["asset"], asset_sum=spec["sum"],
        )
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("téléchargement refusé") from exc
    return apply_verified(
        paths, archive, checksum, health=health, gate=gate, bench=bench,
        signer=signer, channel=channel, pin=pin,
    )


def channel_status(paths):
    found = {}
    for channel in CHANNELS:
        try:
            state = load_state(paths, channel)
        except ReleaseError as exc:
            found[channel] = {"error": str(exc)}
            continue
        active = state.get("active")
        slot = (state.get("slots") or {}).get(active) or {}
        found[channel] = {
            "active": active,
            "previous": state.get("previous"),
            "rolled_back": bool(state.get("rolled_back")),
            "sha256": slot.get("sha256") or "",
            "p99_us": slot.get("p99_us"),
            "kernel": slot.get("kernel") or "",
            "signed": bool(state.get("signature")),
            "signing_gap": state.get("signing_gap") or "",
        }
    return {"channels": found}


def undo_channel(paths, channel):
    if channel not in CHANNELS:
        raise ReleaseError("canal refusé")
    state = load_state(paths, channel)
    previous = state.get("previous")
    if previous not in {"a", "b"} or previous == state.get("active"):
        raise ReleaseError("rien à annuler")
    state["active"] = previous
    state["previous"] = None
    state["undo"] = True
    _write_state(paths, state, channel)
    return {"undo": True, "channel": channel, "slot": previous}


def _boot_choice(paths):
    path = paths.run / "boot.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def sync_channels(paths, fetcher, gate=None, bench=None, health=None, signer=None, pin=None):
    """Applique chaque canal. L'échec de l'un laisse l'autre, et iSCSI, en place."""
    results = []
    names = ("app", "kernel")
    if _boot_choice(paths).get("chosen") == "rescue":
        results.append({"channel": "app", "applied": False, "skipped": "secours"})
        names = ("kernel",)
    for channel in names:
        try:
            archive, checksum = fetcher(channel)
            results.append(apply_verified(
                paths, archive, checksum, health=health, gate=gate, bench=bench,
                signer=signer, channel=channel,
                asset=CHANNELS[channel]["asset"], pin=pin,
            ))
        except ReleaseError as exc:
            results.append({"channel": channel, "applied": False, "error": str(exc)})
        except Exception:
            results.append({"channel": channel, "applied": False, "error": "canal en échec"})
    return {"channels": results}


def handle_request(paths, payload, token_file="/etc/san/github.token"):
    if not isinstance(payload, dict):
        raise ReleaseError("demande refusée")
    if payload.get("token") or payload.get("github_token") or payload.get("tag"):
        raise ReleaseError("la mise à jour est automatique")
    undo = payload.get("undo") or ""
    op = payload.get("op") or ""
    if undo:
        if not isinstance(undo, str):
            raise ReleaseError("demande refusée")
        return undo_channel(paths, undo)
    if op == "clear-scsi":
        from san_tui.scsilog import clear
        clear(paths)
        return {"cleared": "scsi"}
    if op == "clear-latency":
        from san_tui.latency import clear
        clear(paths)
        return {"cleared": "latency"}
    if op in ("", "status"):
        return channel_status(paths)
    raise ReleaseError("demande refusée")


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
