#!/usr/bin/python3
"""Confirm a boot with a missing export disk did not open the portal."""

import json
import subprocess

subprocess.run(["systemctl", "start", "san-net-apply.service"], check=False)
subprocess.run(["systemctl", "start", "san-restore.service"], check=False)
listen = subprocess.run(
    ["ss", "-H", "-ltn", "sport", "=", ":3260"],
    text=True,
    capture_output=True,
)
alerts = subprocess.run(["san-tui", "alerts"], text=True, capture_output=True)
state = subprocess.run(
    ["systemctl", "show", "san-restore.service", "-p", "ActiveState", "-p", "Result", "-p", "ExecMainStatus"],
    text=True,
    capture_output=True,
)
try:
    parsed = json.loads(alerts.stdout or "[]")
except json.JSONDecodeError:
    parsed = []
print("SAN-RESULT " + json.dumps({
    "listen": listen.stdout.strip(),
    "alerts": parsed,
    "unit": state.stdout.strip(),
}, ensure_ascii=False))
