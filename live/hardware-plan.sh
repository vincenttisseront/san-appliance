#!/bin/sh
# Plan matériel exécutable. Il n'ajoute aucune cible FC, FCoE, SRP ou HA.
# Une VM ne valide aucun de ces points. Le script affiche le plan et s'arrête.
set -eu
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
plan=$root/docs/hardware-validation.md
if [ ! -f "$plan" ]; then
    printf 'plan matériel absent\n' >&2
    exit 1
fi
cat "$plan"
printf '\nstatut: non exécuté sur ce banc. Aucun code cible FC, FCoE, SRP ou HA n'"'"'a été ajouté.\n'
exit 0
