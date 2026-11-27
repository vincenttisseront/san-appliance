"""Un LUN sans ACL d'initiateur reste invisible. L'ACL reprise est celle du premier."""

import copy


def valid_acl(acl):
    if not isinstance(acl, dict):
        return False
    initiator = acl.get("initiator") or ""
    if not initiator.startswith("iqn.") or initiator in {"iqn.", "iqn.*"}:
        return False
    if any(ch.isspace() for ch in initiator) or "*" in initiator:
        return False
    return True


def first_acl(targets):
    for target in targets or []:
        for acl in target.get("acls") or []:
            if valid_acl(acl):
                return copy.deepcopy(acl)
    return None


def mask_targets(targets):
    """Remplit une ACL manquante avec celle du premier initiateur. N'ouvre rien."""
    shared = first_acl(targets)
    visible = []
    hidden = []
    for target in targets or []:
        if shared is None:
            hidden.append(target)
            continue
        cloned = copy.deepcopy(target)
        cloned["acls"] = [copy.deepcopy(shared)]
        visible.append(cloned)
    return visible, hidden
