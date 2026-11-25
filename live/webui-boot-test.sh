#!/bin/bash
# VM-boot the live ISO and check the WebUI on 127.0.0.1 only.
set -euo pipefail
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$SCRIPT_DIR/webui-boot-test.py"
