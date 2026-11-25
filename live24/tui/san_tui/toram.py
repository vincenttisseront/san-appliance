"""toram n'est jamais posé sans une mesure de RAM.

L'ISO live est figée : sa ligne de commande ne contient pas toram. La mesure
dit seulement si une racine installée aurait le droit de l'ajouter.
"""

HEADROOM = 512 * 1024 * 1024


def decide(mem_kib, squash_bytes):
    have = int(mem_kib) * 1024
    need = int(squash_bytes) * 2 + HEADROOM
    eligible = have >= need and int(squash_bytes) > 0
    if int(squash_bytes) <= 0:
        reason = "taille du squashfs inconnue, toram reste absent"
    elif not eligible:
        reason = "RAM {} o insuffisante pour {} o de squashfs, toram reste absent".format(have, squash_bytes)
    else:
        reason = "RAM suffisante, toram n'est pas ajouté à l'ISO figée"
    return {
        "enabled": False,
        "eligible": eligible,
        "mem_bytes": have,
        "squash_bytes": int(squash_bytes),
        "need_bytes": need,
        "reason": reason,
    }


def installed_cmdline(measurement):
    """Jeton pour un slot installé. L'ISO live ne l'utilise pas."""
    if measurement.get("eligible") and measurement.get("allow_installed"):
        return "toram"
    return ""
