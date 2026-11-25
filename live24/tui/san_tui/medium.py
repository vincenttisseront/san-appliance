"""Support de conf versionné. Le réseau et les exports n'y entrent que si l'identifiant colle."""

from san_tui.identity import identity_problem

MEDIUM_VERSION = 2


def evaluate(stamp, device):
    """None si ce disque est le support choisi. Sinon la cause du refus."""
    if not isinstance(stamp, dict):
        return "support de conf illisible"
    if stamp.get("version") != MEDIUM_VERSION:
        return "version de support refusée"
    return identity_problem(stamp.get("conf") or {}, device, "support de conf")


def accept_ollama_env(text):
    """Une seule variable, sans clé ni secret."""
    seen = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            return False
        key, value = line.split("=", 1)
        if key.strip() != "SAN_OLLAMA_ENDPOINT" or not value.strip():
            return False
        if any(word in value.lower() for word in ("key", "token", "secret", "password")):
            return False
        seen = True
    return seen
