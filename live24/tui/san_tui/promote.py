"""Un slot n'est promu que si fio termine. La signature reste hors de l'image."""

import os
import shutil
import subprocess
from pathlib import Path

from san_tui.latency import bench_file


class PromoteError(Exception):
    pass


def fio_bench():
    if shutil.which("fio") is None:
        return {"pass": False, "reason": "fio absent"}
    try:
        metric = bench_file()
    except Exception:
        return {"pass": False, "reason": "fio en échec"}
    p99 = metric.get("p99_us")
    if not isinstance(p99, (int, float)):
        return {"pass": False, "reason": "p99 absent"}
    return {"pass": True, "p99_us": float(p99)}


def ensure_key(directory):
    """Crée une clé Ed25519 sur le disque de données. Elle n'est pas dans l'ISO."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = directory / "release-key.pem"
    public = directory / "release-key.pub"
    if not key.is_file():
        completed = subprocess.run(
            ["openssl", "genpkey", "-algorithm", "ED25519", "-out", str(key)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0 or not key.is_file():
            raise PromoteError("clé indisponible")
        os.chmod(key, 0o600)
        published = subprocess.run(
            ["openssl", "pkey", "-in", str(key), "-pubout", "-out", str(public)],
            capture_output=True,
            text=True,
            check=False,
        )
        if published.returncode != 0:
            key.unlink(missing_ok=True)
            raise PromoteError("clé indisponible")
        os.chmod(public, 0o644)
    if not public.is_file():
        raise PromoteError("clé indisponible")
    return key, public


def payload_for(digest, measured):
    p99 = measured.get("p99_us") if isinstance(measured, dict) else ""
    return "{} {}\n".format(digest, p99).encode("utf-8")


def sign_bytes(key, payload):
    directory = key.parent
    message = directory / ".sign-msg"
    signature = directory / ".sign-sig"
    try:
        message.write_bytes(payload)
        os.chmod(message, 0o600)
        completed = subprocess.run(
            [
                "openssl", "pkeyutl", "-sign", "-inkey", str(key),
                "-rawin", "-in", str(message), "-out", str(signature),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not signature.is_file():
            raise PromoteError("signature indisponible")
        return signature.read_bytes()
    finally:
        for path in (message, signature):
            try:
                path.unlink()
            except OSError:
                pass


def verify_bytes(public, payload, signature):
    directory = public.parent
    message = directory / ".verify-msg"
    sigfile = directory / ".verify-sig"
    try:
        message.write_bytes(payload)
        sigfile.write_bytes(signature)
        completed = subprocess.run(
            [
                "openssl", "pkeyutl", "-verify", "-pubin", "-inkey", str(public),
                "-rawin", "-in", str(message), "-sigfile", str(sigfile),
            ],
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0
    finally:
        for path in (message, sigfile):
            try:
                path.unlink()
            except OSError:
                pass


def sign_promotion(paths, digest, measured):
    """Signe, ou décrit l'écart si la clé ne peut pas être créée."""
    try:
        key, _public = ensure_key(paths.data)
        signature = sign_bytes(key, payload_for(digest, measured))
        return signature.hex(), ""
    except Exception as exc:
        return "", "signature indisponible: {}".format(exc.__class__.__name__)
