#!/bin/bash
# QEMU/KVM check of the live ISO: short CD boot, then LIO with a second VM.
# Disposable disks, isolated NICs, no passthrough. Secure Boot is not tested.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    printf 'iscsi-test: run as root (make test-iscsi)\n' >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$SCRIPT_DIR/iscsi-test.py"
