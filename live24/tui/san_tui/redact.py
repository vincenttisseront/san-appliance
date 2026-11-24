"""Retire les secrets des messages visibles et des journaux."""

SECRET_KEYS = {
    "password",
    "chap_password",
    "chap_secret",
    "secret",
    "mutual_password",
    "chap_mutual_password",
}


def redact(text, secrets=()):
    """Remplace les valeurs secrètes connues. Ne journalise jamais le secret."""
    if text is None:
        return ""
    out = str(text)
    for secret in secrets:
        if secret:
            out = out.replace(str(secret), "[redacted]")
    return out


def public_mapping(data):
    """Copie un dict en remplaçant toute clé secrète par une référence."""
    if isinstance(data, dict):
        cleaned = {}
        for key, value in data.items():
            if key in SECRET_KEYS:
                cleaned[key] = "[redacted]"
            else:
                cleaned[key] = public_mapping(value)
        return cleaned
    if isinstance(data, list):
        return [public_mapping(item) for item in data]
    return data
