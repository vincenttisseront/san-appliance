"""Mise à jour A/B. Le slot actif et la partition data ne sont pas écrits."""

import hashlib

from san_tui.safety import SafetyError, assert_usable


class UpdateError(Exception):
    pass


def plan_update(state, pending, volumes_by_id, mode):
    if mode == "diagnostic":
        raise UpdateError("diagnostic : pas de mise à jour")
    if not pending:
        return None
    if not isinstance(state, dict) or state.get("version") != 1:
        raise UpdateError("état A/B refusé")
    if not isinstance(pending, dict) or pending.get("version") != 1:
        raise UpdateError("mise à jour : version refusée")
    slot = pending.get("slot")
    if slot not in {"a", "b"}:
        raise UpdateError("slot inconnu")
    if state.get("active") == slot:
        raise UpdateError("le slot demandé est le slot actif")
    slots = state.get("slots") or {}
    slot_id = (slots.get(slot) or {}).get("id")
    data_id = (state.get("data") or {}).get("id")
    if not slot_id or slot_id == data_id:
        raise UpdateError("la partition data n'est pas un slot")
    volume = volumes_by_id.get(slot_id)
    if volume is None:
        raise UpdateError("slot absent : {}".format(slot_id))
    try:
        assert_usable(volume)
    except SafetyError as exc:
        raise UpdateError(str(exc)) from exc
    digest = pending.get("sha256") or ""
    image = pending.get("image") or ""
    if len(digest) != 64 or not image:
        raise UpdateError("image ou empreinte absente")
    return {
        "slot": slot,
        "id": slot_id,
        "image": image,
        "sha256": digest,
        "data": data_id,
    }


def apply_update(plan, read_image, write_device, read_device, write_state, state):
    payload = read_image(plan["image"])
    digest = hashlib.sha256(payload).hexdigest()
    if digest != plan["sha256"]:
        raise UpdateError("empreinte de l'image différente")
    write_device(plan["id"], payload)
    written = read_device(plan["id"], len(payload))
    if hashlib.sha256(written).hexdigest() != digest:
        raise UpdateError("relecture du slot différente, slot actif inchangé")
    updated = {
        "version": 1,
        "active": plan["slot"],
        "disk": state.get("disk"),
        "slots": {},
        "data": state.get("data"),
    }
    for name, item in (state.get("slots") or {}).items():
        updated["slots"][name] = dict(item)
    updated["slots"][plan["slot"]]["sha256"] = digest
    write_state(updated)
    return updated


def grub_cmdline(measurement):
    """Le slot installé reçoit toram seulement après une mesure positive."""
    parts = ["boot=casper", "nopersistent"]
    if measurement.get("eligible") and measurement.get("allow_installed"):
        parts.append("toram")
    return " ".join(parts)
