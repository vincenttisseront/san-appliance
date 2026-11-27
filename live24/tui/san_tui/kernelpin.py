"""Noyau Ubuntu 24.04 suivi par doctor. La série reste 6.8."""

import re
import tarfile
import io

_PIN = re.compile(r"^6\.8\.0-\d+\.\d+$")
_RELEASE = re.compile(r"^6\.8\.0-\d+(?:\.\d+)?$")
_IMAGE = re.compile(r"^linux-image-6\.8\.0-\d+-generic$")


def version_key(text):
    raw = str(text or "").strip()
    if not _RELEASE.fullmatch(raw):
        raise ValueError("version hors 6.8")
    upstream, abi = raw.split("-", 1)
    parts = [int(item) for item in upstream.split(".")]
    parts.extend(int(item) for item in abi.split("."))
    return tuple(parts)


def kernel_allowed(release, pin=None):
    try:
        got = version_key(release)
    except ValueError:
        return False
    if not pin:
        return True
    try:
        ceiling = version_key(str(pin).strip())
    except ValueError:
        return False
    return got <= ceiling


def packages_sha256(inrelease, filename="main/binary-amd64/Packages.gz"):
    in_sha = False
    for raw in str(inrelease).splitlines():
        if raw.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha and raw and not raw.startswith(" ") and not raw.startswith("\t"):
            in_sha = False
        if not in_sha:
            continue
        parts = raw.split()
        if len(parts) >= 3 and parts[-1] == filename and len(parts[0]) == 64:
            return parts[0]
    raise ValueError("empreinte Packages.gz absente")


def _stanzas(text):
    current = {}
    for raw in str(text).splitlines():
        if not raw.strip():
            if current.get("Package"):
                yield current
            current = {}
            continue
        if raw.startswith(" ") or raw.startswith("\t"):
            continue
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        current[key] = value.strip()
    if current.get("Package"):
        yield current


def newest_generic(packages_text):
    """Version du linux-image-6.8 que linux-image-generic dépend. Refuse un autre train."""
    stanzas = list(_stanzas(packages_text))
    generics = [item for item in stanzas if item.get("Package") == "linux-image-generic"]
    if not generics:
        raise ValueError("linux-image-generic absent")
    chosen = max(generics, key=lambda item: item.get("Version") or "")
    depends = chosen.get("Depends") or ""
    image = None
    for part in depends.split(","):
        name = part.strip().split()[0]
        if _IMAGE.fullmatch(name):
            image = name
            break
        if name.startswith("linux-image-") and "generic" in name and not _IMAGE.fullmatch(name):
            raise ValueError("linux-image-generic ne pointe plus vers 6.8")
    if image is None:
        raise ValueError("dépendance 6.8 absente")
    for item in stanzas:
        if item.get("Package") == image and item.get("Version"):
            version = item["Version"]
            if not _PIN.fullmatch(version):
                raise ValueError("version d'image hors 6.8")
            return version
    raise ValueError("paquet {} absent".format(image))


def kernel_release_of(blob):
    from san_tui.release import ReleaseError, _member_ok

    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            try:
                member = archive.getmember("kernel.release")
            except KeyError as exc:
                raise ReleaseError("noyau hors 6.8") from exc
            if not _member_ok(member) or not member.isfile():
                raise ReleaseError("noyau hors 6.8")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ReleaseError("noyau hors 6.8")
            text = extracted.read(128).decode("utf-8", "replace").strip()
    except ReleaseError:
        raise
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError("noyau hors 6.8") from exc
    if not kernel_allowed(text, None):
        raise ReleaseError("noyau hors 6.8")
    return text
