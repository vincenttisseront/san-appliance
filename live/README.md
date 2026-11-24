# ISO live Ubuntu 24.04

Image live Noble, à côté de l'image installable 26.04 (`image/`). Elle ne partitionne rien et n'installe pas le système.

```bash
make doctor
make iso
```

`make doctor` vérifie l'hôte Ubuntu 24.04 amd64, les outils, le menu GRUB, et les signatures APT (`InRelease` de noble, noble-updates, noble-security) avec le trousseau Ubuntu. `make iso` produit dans `dist/` :

- `san-appliance-live-24.04-amd64.iso`
- `san-appliance-live-24.04-amd64.iso.sha256`
- `packages.manifest`
- `build-metadata.json`

La première entrée GRUB est « SAN appliance live » (`boot=casper`). Il n'y a pas d'entrée d'installation. La seconde est le mode diagnostic (`san.mode=diagnostic`) : elle n'exporte pas de LUN. `nopersistent` et `systemd.gpt_auto=0` empêchent casper et systemd d'écrire un volume de persistance ou de monter seuls un disque. Le démarrage BIOS et UEFI passe par GRUB (El Torito). Secure Boot n'est pas testé et n'est pas annoncé. La construction n'est pas reproductible bit à bit.

Le compte console `sanadmin` est ouvert en local par autologin. Le mot de passe vide posé par casper est verrouillé au boot. OpenSSH n'est pas installé. Aucun secret n'est dans l'ISO.

L'image embarque `python3-rtslib-fb`, `targetcli-fb`, les modules `target_core_mod` et `iscsi_target_mod`, et la TUI `san-tui` (`live24/tui`). Le boot n'exporte rien tant que `/etc/san/chosen.json` est absent. Le mode diagnostic masque `san-restore.service`.

Le support de conf choisi par l'opérateur est un système de fichiers étiqueté `SANCONF`. Au boot, `san-conf-bind` ne monte que cette étiquette, et seulement si elle contient déjà `chosen.json`. Il ne formate pas et ne touche pas aux autres disques. `san-net-apply` repose ensuite les adresses enregistrées dans `exports.json`, puis `san-restore` rétablit les cibles dont le volume, le WWN, la série et la taille collent encore. `open-iscsi` est présent pour une VM d'essai ; il n'a aucun nœud configuré dans l'image.
