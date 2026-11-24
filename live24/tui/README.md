# TUI iSCSI LIO (Ubuntu 24.04)

Outils locaux pour l'appliance live Ubuntu 24.04. Ce répertoire ne construit pas l'ISO et ne modifie pas l'image 26.04 sous `image/`.

`san-tui` appelle les outils du système (sysfs, `ip`, python3-rtslib-fb). Il ne partage rien tout seul.

## Boot

`san-inventory.service` exécute `san-tui inventory`. Il écrit un inventaire. Il ne monte pas, ne formate pas, n'assemble pas de RAID et n'exporte aucun LUN.

`san-restore.service` ne démarre que si `/etc/san/chosen.json` existe. Pour chaque cible, il vérifie l'adresse de stockage, le volume by-id, le WWN, la série et la taille. Une cible dont le volume manque ou dont la conf est invalide n'est pas exportée. La cause est une alerte.

## Réseau

L'interface et l'adresse d'administration sont distinctes de celles du stockage. Le portail iSCSI est exactement l'adresse de stockage. `0.0.0.0` et `::` sont refusés. Il n'y a pas de mode démo ni d'ACL générée : un initiateur, une ACL, un CHAP.

Le secret CHAP est lu dans un fichier de mode `0600`. Il est stocké dans `secrets.json` (`0600`), jamais dans `exports.json` ni dans les alertes.

## Persistance

Trois rôles : le système live, un support de conf choisi explicitement, un support de data choisi explicitement. `san-tui persist-select` refuse un disque de boot, monté ou actif. La confirmation est l'identifiant `/dev/disk/by-id/...` complet. Chaque enregistrement copie la conf précédente dans `backups/<horodatage>/`.

## Commandes

Depuis `live24/tui` :

```bash
python3 -m unittest discover -s tests -v
./bin/san-tui inventory
```

Enregistrer un export, sans l'activer :

```bash
./bin/san-tui network-set --admin-iface enp1s0 --admin-address 192.168.1.10/24 \
  --storage-iface enp2s0 --storage-address 10.0.0.10/24
install -m 0600 /dev/null /root/chap
./bin/san-tui export-add --iqn iqn.2026-11.local.san:disk --portal 10.0.0.10 \
  --volume wwn-0x... --initiator iqn.2026-11.local.client:host \
  --chap-user client1 --chap-secret-file /root/chap --confirm /dev/disk/by-id/wwn-0x...
./bin/san-tui restore
```

La TUI interactive s'ouvre sur un terminal : disques, interfaces, réseau admin, réseau stockage, exports, alertes.

Paquet Ubuntu : `python3-rtslib-fb`. Le noyau doit avoir `CONFIG_TARGET_CORE` et `CONFIG_ISCSI_TARGET`. Sans ces options, `restore` ne crée pas de cible.
