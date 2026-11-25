#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
    printf 'perf-test: run as root\n' >&2
    exit 1
fi
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/perf-test.py"
