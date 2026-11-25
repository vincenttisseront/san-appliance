"""Conseiller optionnel. Il ne choisit qu'un profil déjà mesuré."""

import json
import urllib.request

ALLOWED_KEYS = {
    "scheduler",
    "mtu",
    "write_back",
    "digest",
    "pin",
    "hugepages",
    "governor",
    "burst",
    "backstore",
    "preallocate",
}


def fetch_once(url, timeout=2):
    """Un seul essai. Un échec retourne None, sans nouvel appel."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def decide(payload, catalog, current):
    """Le modèle ne peut envoyer qu'un nom de profil. Toute autre clé est refusée."""
    if payload is None:
        return {"action": "alert", "reason": "ollama injoignable"}
    if not isinstance(payload, dict) or set(payload.keys()) != {"profile"}:
        return {"action": "alert", "reason": "réponse hors catalogue"}
    name = payload.get("profile")
    profiles = (catalog or {}).get("profiles") or {}
    entry = profiles.get(name)
    if not isinstance(entry, dict):
        return {"action": "alert", "reason": "profil non mesuré"}
    change = entry.get("change")
    if not isinstance(change, dict) or len(change) != 1:
        return {"action": "alert", "reason": "un seul réglage est autorisé"}
    key = next(iter(change))
    if key not in ALLOWED_KEYS:
        return {"action": "alert", "reason": "réglage inconnu"}
    current_p99 = _p99(profiles.get(current))
    suggested = entry.get("p99_us")
    if suggested is None or (current_p99 is not None and suggested > current_p99):
        return {
            "action": "rollback",
            "reason": "p99 pire que le réglage courant",
            "profile": name,
        }
    if name == current:
        return {"action": "none", "reason": "déjà appliqué"}
    return {"action": "apply", "profile": name, "change": dict(change)}


def rollback_needed(before_p99, after_p99):
    """Sans mesure après coup, le réglage ne reste pas."""
    if before_p99 is None or after_p99 is None:
        return True
    return after_p99 > before_p99


def _p99(entry):
    if not isinstance(entry, dict):
        return None
    value = entry.get("p99_us")
    if isinstance(value, (int, float)):
        return value
    return None
