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

Cette image ne contient pas la TUI ni la cible iSCSI. Elle ne charge pas `target_core_mod`.
