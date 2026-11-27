#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
    printf 'capacity-vm-test: run as root\n' >&2
    exit 1
fi
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/capacity-vm-test.py"
