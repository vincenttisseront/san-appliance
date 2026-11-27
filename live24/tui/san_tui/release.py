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
ASSET_SIG = "san-appliance-app.tar.gz.sha256.asc"
KERNEL_ASSET = "san-appliance-kernel.tar.gz"
KERNEL_ASSET_SUM = "san-appliance-kernel.tar.gz.sha256"
CHANNELS = {
    "app": {"asset": ASSET, "sum": ASSET_SUM},
    "kernel": {"asset": KERNEL_ASSET, "sum": KERNEL_ASSET_SUM},
}
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
    if state.get("active") not in {"a", "b", "iso"}:
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
                   signer=None, channel="app", asset=None, pin=None, clock=None,
                   writing=False):
    """Vérifie l'empreinte, gèle les exports le temps de l'écriture, puis les rouvre.

    Le slot ne devient actif que si le banc fio passe et si le slot répond.
    Le canal noyau n'accepte qu'une version 6.8, au plus celle suivie par doctor.
    Une session iSCSI en écriture reporte la bascule et laisse le slot actif.
    """
    if channel not in CHANNELS:
        raise ReleaseError("canal refusé")
    asset_name = asset or CHANNELS[channel]["asset"]
    expected = parse_checksum(checksum_text, asset_name)
    digest = verify_archive(archive, expected)
    if writing:
        state = load_state(paths, channel)
        current = state["active"]
        result = {
            "applied": False,
            "slot": current,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "channel": channel,
            "reason": "session iSCSI en écriture",
        }
        append_audit(paths, result, "reporté")
        return result
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
    screened = _screen_release(paths, archive, state, digest, channel)
    if screened is not None:
        return screened
    measured = measure()
    if not isinstance(measured, dict) or not measured.get("pass"):
        raise ReleaseError("banc fio refusé")
    from san_tui.tune import release_refused
    refused, why = release_refused(paths, measured)
    if refused:
        result = {
            "applied": False,
            "slot": current,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "channel": channel,
            "p99_us": measured.get("p99_us"),
            "reason": why,
        }
        append_audit(paths, result, "refusé")
        return result
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
            clock=clock,
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


def _screen_release(paths, archive, state, digest, channel):
    """Version, diff, et session d'écriture, avant le banc et avant de geler iSCSI."""
    current = state["active"]
    target = inactive_slot(current)
    root = channel_dir(paths, channel)
    staging = root / (target + ".screen")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        extract_archive(archive, staging)
        incoming = read_version_text(staging)
        active_version = read_version_text(root / current)
        if version_older(incoming, active_version):
            raise ReleaseError("release plus ancienne que le slot actif")
        diff = tree_diff(root / current, staging)
        if channel == "app":
            write_diff(paths, diff)
        rendered = render_diff(diff)
        if version_same(incoming, active_version):
            return {
                "applied": False,
                "deferred": False,
                "unchanged": True,
                "slot": current,
                "previous": current,
                "rollback": False,
                "sha256": digest,
                "channel": channel,
                "diff": rendered,
                "reason": "déjà en place",
            }
        if channel == "app" and session_is_writing(paths):
            return {
                "applied": False,
                "deferred": True,
                "unchanged": False,
                "slot": current,
                "previous": current,
                "rollback": False,
                "sha256": digest,
                "channel": channel,
                "diff": rendered,
                "reason": "session en écriture",
            }
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _commit_slot(paths, archive, state, digest, measured, check, sign, channel, kernel_version, clock=None):
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
    incoming = read_version_text(staging)
    active_version = read_version_text(root / current)
    if version_older(incoming, active_version):
        shutil.rmtree(staging, ignore_errors=True)
        raise ReleaseError("release plus ancienne que le slot actif")
    diff = tree_diff(root / current, staging)
    if channel == "app":
        write_diff(paths, diff)
    if version_same(incoming, active_version):
        shutil.rmtree(staging, ignore_errors=True)
        return {
            "applied": False,
            "deferred": False,
            "unchanged": True,
            "slot": current,
            "previous": current,
            "rollback": False,
            "sha256": digest,
            "channel": channel,
            "diff": render_diff(diff),
            "reason": "déjà en place",
        }
    if final.exists():
        shutil.rmtree(final)
    staging.rename(final)
    health_script = final / "health"
    if health_script.is_file():
        os.chmod(health_script, 0o755)
    slot_record = {"sha256": digest, "p99_us": measured.get("p99_us"), "installed": utc_stamp(clock), "version": incoming}
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
            "diff": render_diff(diff),
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
        "installed": slot_record.get("installed"),
        "diff": render_diff(diff),
        "newest": newest_slot(updated),
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


def _request(url, token, opener, accept, timeout=60):
    if token and token in url:
        raise ReleaseError("téléchargement refusé")
    headers = {"User-Agent": "san-appliance", "Accept": accept}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as response:
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


def fetch_release(api, tag, token, opener, asset=ASSET, asset_sum=ASSET_SUM, timeout=60):
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
    if asset not in assets or asset_sum not in assets:
        raise ReleaseError("archive ou empreinte absente de la release")
    names = [asset, asset_sum]
    if asset == ASSET and ASSET_SIG in assets:
        names.append(ASSET_SIG)
    for name in names:
        if not allowed_download(assets[name], base):
            raise ReleaseError("adresse de téléchargement refusée")
    archive = _request(assets[asset], token, opener, "application/octet-stream", timeout)
    checksum = _request(assets[asset_sum], token, opener, "text/plain", timeout)
    try:
        text = checksum.decode("utf-8")
    except UnicodeError as exc:
        raise ReleaseError("empreinte illisible") from exc
    signature = None
    if asset == ASSET and ASSET_SIG in assets:
        try:
            signature = _request(assets[ASSET_SIG], token, opener, "text/plain", timeout).decode("utf-8")
        except UnicodeError as exc:
            raise ReleaseError("signature illisible") from exc
    return archive, text, signature


def fetch_and_apply(paths, tag="", api="", token="", opener=None, health=None,
                    channel="app", gate=None, bench=None, signer=None, pin=None, clock=None,
                    writing=False, pubkey=""):
    if tag:
        raise ReleaseError("la mise à jour est automatique")
    spec = CHANNELS.get(channel)
    if spec is None:
        raise ReleaseError("canal refusé")
    client = opener or urllib.request.build_opener()
    try:
        archive, checksum, signature = fetch_release(
            api, "", token, client, asset=spec["asset"], asset_sum=spec["sum"],
        )
    except ReleaseError:
        raise
    except Exception as exc:
        raise ReleaseError("téléchargement refusé") from exc
    decision = None
    if pubkey or signature:
        from san_tui.evolve import signature_decision
        has_key = bool(pubkey) and Path(pubkey).is_file()
        verified = True
        if signature and has_key:
            verified = _gpg_verified(checksum, signature, pubkey)
        decision = signature_decision(bool(signature), has_key, verified)
        if decision["blocked"]:
            raise ReleaseError("signature refusée")
    result = apply_verified(
        paths, archive, checksum, health=health, gate=gate, bench=bench,
        signer=signer, channel=channel, pin=pin, clock=clock, writing=writing,
    )
    if decision and decision["gap"]:
        result = dict(result)
        result["signature"] = "non vérifiée"
        append_audit(paths, {"slot": result.get("slot"), "sha256": result.get("sha256"), "reason": decision["gap"]}, "signature")
    return result


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
        text_value = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    line = text_value.strip().splitlines()
    return line[0].strip() if line else ""


def parse_version(text_value):
    if not text_value:
        return None
    raw = text_value[1:] if text_value[:1] in ("v", "V") else text_value
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


def rollback_slot(paths, health=None):
    """Un pas vers le slot précédent, puis vers l'image ISO si elle est là.

    Sans image, le second appel s'arrête. Les slots et les données restent.
    """
    state = load_state(paths)
    active = state.get("active")
    if active in {None, "iso"} or active not in {"a", "b"}:
        return {
            "applied": False,
            "rollback": False,
            "slot": active,
            "reason": "image ISO déjà active" if active == "iso" else "déjà sur le slot précédent",
        }
    if active == "b":
        target = "a"
        dest = app_dir(paths) / "a"
    else:
        image = image_dir(paths)
        if image.is_dir():
            target = "iso"
            dest = image
        else:
            return {
                "applied": False,
                "rollback": False,
                "slot": active,
                "reason": "déjà sur le slot précédent",
            }
    if not dest.is_dir() or (target != "iso" and not any(dest.iterdir())):
        raise ReleaseError("slot précédent absent")
    if health is not None or (dest / "health").is_file():
        check = health or run_health
        try:
            healthy = check(dest) == 0
        except Exception:
            healthy = False
        if not healthy:
            return {
                "applied": False,
                "rollback": False,
                "slot": active,
                "reason": "slot précédent malsain",
            }
    updated = {
        "version": 1,
        "active": target,
        "rolled_back": True,
        "previous": active,
        "slots": {
            "a": dict((state.get("slots") or {}).get("a") or {}),
            "b": dict((state.get("slots") or {}).get("b") or {}),
        },
    }
    _write_state(paths, updated)
    result = {"applied": False, "rollback": True, "slot": target, "previous": active}
    append_audit(paths, result, "retour")
    return result


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
    if payload.get("token") or payload.get("github_token") or payload.get("tag"):
        raise ReleaseError("la mise à jour est automatique")
    undo = payload.get("undo")
    op = payload.get("op") or ""
    if undo is True:
        return undo_slot(paths)
    if payload.get("status") is True and not op:
        return release_status(paths)
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
    if op == "rollback":
        return rollback_slot(paths)
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
