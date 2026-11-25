"""Identité stable d'un volume : by-id, WWN, série, taille, bloc."""


def identity_problem(expected, current, label):
    """None si le volume vu est celui qui a été enregistré."""
    if not isinstance(expected, dict):
        return "{} illisible".format(label)
    if current is None:
        return "{} absent : {}".format(label, expected.get("id"))
    if current.get("id") != expected.get("id"):
        return "{} : identifiant by-id différent".format(label)
    if expected.get("wwn") and current.get("wwn") != expected.get("wwn"):
        return "{} : WWN différent".format(label)
    if expected.get("serial") and current.get("serial") != expected.get("serial"):
        return "{} : série différente".format(label)
    if expected.get("size") is not None and current.get("size") != expected.get("size"):
        return "{} : taille différente".format(label)
    if expected.get("block_size") and current.get("block_size") != expected.get("block_size"):
        return "{} : taille de bloc différente".format(label)
    return None
