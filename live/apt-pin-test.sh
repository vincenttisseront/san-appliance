#!/bin/bash
# The pin accepts the recorded signature and rejects a silent change.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
. ./apt-pin.sh

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pin=$tmp/pins
decision=$tmp/decisions
printf '%s\n' 'aaaa  noble' > "$pin"
printf '%s\n' '# suite sha reason' > "$decision"

apt_pin_check "$pin" "$decision" noble aaaa
echo "apt-pin-test: matching pin accepted"

set +e
apt_pin_check "$pin" "$decision" noble bbbb
status=$?
set -e
[ "$status" -eq 2 ]
echo "apt-pin-test: changed signature refused"

printf '%s\n' 'noble bbbb archive-moved-on-purpose' >> "$decision"
apt_pin_check "$pin" "$decision" noble bbbb
echo "apt-pin-test: explicit decision accepted"

printf '%s\n' 'noble cccc' >> "$decision"
set +e
apt_pin_check "$pin" "$decision" noble cccc
status=$?
set -e
[ "$status" -eq 2 ]
echo "apt-pin-test: decision without a reason refused"
