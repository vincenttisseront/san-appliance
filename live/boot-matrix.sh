#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
    printf 'boot-matrix: run as root\n' >&2
    exit 1
fi
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/boot-matrix.py"
