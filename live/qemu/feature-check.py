#!/usr/bin/python3
"""Vérifie ce que le boot a appliqué, et qu'Ollama en échec ne l'arrête pas."""

import hashlib
import json
import os
import pathlib
import subprocess
import time

HOST_NQN = "nqn.2026-11.local.client:nvme"
SUB_NQN = "nqn.2026-11.local.san:nvme"


def run(args, check=False):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def main():
    mdstat = pathlib.Path("/proc/mdstat").read_text(encoding="utf-8", errors="replace")
    array_names = []
    for md in pathlib.Path("/sys/block").glob("md*"):
        name_path = md / "md" / "array_name"
        if name_path.is_file():
            array_names.append(name_path.read_text(encoding="utf-8").strip())
    dev_md = []
    md_dir = pathlib.Path("/dev/md")
    if md_dir.is_dir():
        dev_md = sorted(entry.name for entry in md_dir.iterdir())
    detail = run(["mdadm", "--detail", "--scan"]).stdout
    lvs = run(["lvs", "--noheadings", "-o", "vg_name,lv_name,lv_attr"]).stdout
    medium = {}
    state = pathlib.Path("/run/san/medium-state.json")
    if state.is_file():
        medium = json.loads(state.read_text(encoding="utf-8"))
    alerts = json.loads(run(["san-tui", "alerts"]).stdout or "[]")
    ab = {}
    ab_path = pathlib.Path("/var/lib/san/conf/ab.json")
    if ab_path.is_file():
        ab = json.loads(ab_path.read_text(encoding="utf-8"))
    data_id = (ab.get("data") or {}).get("id")
    slot_b = ((ab.get("slots") or {}).get("b") or {}).get("id")
    data_sha = ""
    slot_sha = ""
    if data_id and pathlib.Path(data_id).exists():
        with open(data_id, "rb") as handle:
            data_sha = hashlib.sha256(handle.read(1024)).hexdigest()
    slot_prefix = ""
    if slot_b and pathlib.Path(slot_b).exists():
        with open(slot_b, "rb") as handle:
            slot_prefix = handle.read(16).decode("utf-8", "replace")
            slot_sha = hashlib.sha256(slot_prefix.encode()).hexdigest()
    nvme_export = pathlib.Path("/sys/kernel/config/nvmet/subsystems") / SUB_NQN
    listen = run(["ss", "-H", "-ltn"]).stdout
    ssh_listen = [line for line in listen.splitlines() if ":22" in line]
    ssh_pass = run([
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "PreferredAuthentications=password", "-o", "PubkeyAuthentication=no",
        "-o", "ConnectTimeout=3", "sanadmin@192.168.10.10", "true",
    ])
    ssh_root = run([
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=3", "root@192.168.10.10", "true",
    ])
    connected = False
    nvme_err = ""
    if nvme_export.is_dir():
        hostnqn = pathlib.Path("/etc/nvme/hostnqn")
        hostnqn.parent.mkdir(parents=True, exist_ok=True)
        hostnqn.write_text(HOST_NQN + "\n", encoding="utf-8")
        run(["modprobe", "nvme-tcp"])
        proc = run(["nvme", "connect", "-t", "tcp", "-a", "10.1.0.10", "-s", "4420", "-n", SUB_NQN])
        connected = proc.returncode == 0
        nvme_err = (proc.stderr or proc.stdout or "")[-300:]
        if connected:
            time.sleep(1)
            run(["nvme", "disconnect", "-n", SUB_NQN])

    env = pathlib.Path("/etc/san/ollama.env")
    env.write_text("SAN_OLLAMA_ENDPOINT=http://127.0.0.1:9/advise\n", encoding="utf-8")
    os.chmod(env, 0o600)
    advised = run(["san-tui", "ollama-advise"])
    alerts_after = json.loads(run(["san-tui", "alerts"]).stdout or "[]")
    md_after = pathlib.Path("/proc/mdstat").read_text(encoding="utf-8", errors="replace")

    print("SAN-RESULT " + json.dumps({
        "medium": medium,
        "mdstat": mdstat,
        "array_names": array_names,
        "dev_md": dev_md,
        "md_detail": detail,
        "lvs": lvs,
        "nvme_config": nvme_export.is_dir(),
        "nvme_connect": connected,
        "nvme_err": nvme_err,
        "data_sha256": data_sha,
        "slot_prefix": slot_prefix,
        "slot_sha256": slot_sha,
        "ab_active": ab.get("active"),
        "ssh": ssh_listen,
        "ssh_password_rc": ssh_pass.returncode,
        "ssh_root_rc": ssh_root.returncode,
        "ollama_rc": advised.returncode,
        "ollama_out": (advised.stdout or "")[-400:],
        "alerts": alerts_after[-6:],
        "md_unchanged": mdstat == md_after,
        "earlier_alerts": [item.get("message") for item in alerts][-8:],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
