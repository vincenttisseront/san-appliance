#!/bin/bash
# Compare a signed InRelease sha256 with the pin. A mismatch needs a decision.
set -euo pipefail

apt_pin_check() {
    local pin_file=$1 decision_file=$2 name=$3 sha=$4
    local expected
    if [ ! -f "$pin_file" ]; then
        printf 'apt-pin: missing pin file %s\n' "$pin_file" >&2
        return 1
    fi
    expected=$(awk -v name="$name" '$2 == name { print $1; exit }' "$pin_file")
    if [ -z "$expected" ]; then
        printf 'apt-pin: no pin for %s\n' "$name" >&2
        return 1
    fi
    if [ "$sha" = "$expected" ]; then
        return 0
    fi
    if [ -f "$decision_file" ] && awk -v name="$name" -v sha="$sha" '
        $1 == name && $2 == sha && NF >= 3 { found = 1 }
        END { exit found ? 0 : 1 }
    ' "$decision_file"; then
        return 0
    fi
    printf 'apt-pin: APT signature changed for %s without an explicit decision (pinned %s, got %s)\n' \
        "$name" "$expected" "$sha" >&2
    return 2
}
