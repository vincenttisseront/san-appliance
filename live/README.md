# ISO live Ubuntu 24.04

Image live Noble, à côté de l'image installable 26.04 (`image/`). Elle ne partitionne rien et n'installe pas le système.

```bash
sudo apt-get install -y mmdebstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools
make doctor
make iso
```

`mtools` fournit `mformat`, exigé par `make doctor` : `grub-mkrescue` s'en sert pour l'image UEFI.

`make doctor` vérifie l'hôte Ubuntu 24.04 amd64, les outils, le menu GRUB, et les signatures APT (`InRelease` de noble, noble-updates, noble-security) avec le trousseau Ubuntu. `make iso` produit dans `dist/` :

- `san-appliance-live-24.04-amd64.iso`
- `san-appliance-live-24.04-amd64.iso.sha256`
- `packages.manifest`
- `build-metadata.json`

La première entrée GRUB est « SAN appliance live » (`boot=casper`). Il n'y a pas d'entrée d'installation. La seconde est le mode diagnostic (`san.mode=diagnostic`) : elle n'exporte pas de LUN et ne formate rien. `nopersistent`, `raid=noautodetect` et `systemd.gpt_auto=0` empêchent casper, mdadm et systemd d'écrire un volume ou d'assembler un array tout seuls. `toram` n'est pas dans le menu : le boot mesure la RAM et laisse le paramètre absent. Le démarrage BIOS et UEFI passe par GRUB (El Torito). Secure Boot n'est pas pris en charge : l'ISO n'est pas signée. `md5sum.txt` est à la racine pour `casper-md5check`. La construction n'est pas reproductible bit à bit.

Le compte `sanadmin` n'est plus en autologin. Un mot de passe à usage unique est écrit sur la console série, puis verrouillé après la première session. Il n'est pas un secret de l'ISO. SSH est installé, refuse root et refuse les mots de passe, et n'écoute que l'adresse d'administration une fois le support de conf accepté. Aucune clé n'est dans l'image.

La page `http://127.0.0.1:3000/appliance-update` affiche le journal de mise à jour et peut revenir d'un slot. Elle n'est pas un formulaire obligatoire. Le boot télécharge une release plus récente de `vincenttisseront/san-appliance` (`san-appliance-app.tar.gz` et `san-appliance-app.tar.gz.sha256`) quand aucune session iSCSI n'écrit. L'empreinte est vérifiée avant d'écrire le slot inactif. Deux slots restent sur le support de conf, et l'image d'origine reste dans `/usr/lib/san-app/image` : le retour peut aller plus loin qu'un seul pas. Si le portail iSCSI configuré n'écoute pas après la bascule, le pointeur de slot revient, sans démontage de la cible. Une signature détachée n'est contrôlée que si `/etc/san/release-pubkey.asc` est présent. Cette clé n'est pas dans l'ISO, et ce dépôt n'en crée pas : sans elle, l'empreinte est vérifiée et l'écart est noté dans le journal. Le jeton, s'il existe, est lu dans `/etc/san/github.token` (mode 0600) au moment de l'appel et n'est pas mis dans l'ISO. Un échec de téléchargement ne modifie pas les exports.

L'image embarque la WebUI loopback (`esos-webui`, pin `image/third_party/esos-webui.sha256`). `live/install-webui.sh` la reconstruit sur l'hôte Noble, sans Docker, avec Node 22.23.4, puis copie le bundle et ce binaire dans le rootfs. `san-webui.service` lance `scripts/serve-local.mjs`, qui force l'écoute sur `127.0.0.1:3000`. Le compte `sanui` n'a pas de shell de connexion. `NUXT_JWT_SECRET` et `NUXT_ENCRYPTION_KEY` sont créés au boot dans `/var/lib/san/webui/webui.env` et ne sont pas dans l'ISO.

L'image embarque `python3-rtslib-fb`, `targetcli-fb`, les modules `target_core_mod` et `iscsi_target_mod`, et la TUI `san-tui` (`live24/tui`). Le boot choisit un disque de données vierge d'au moins 512 Mio qui n'est pas le disque d'amorçage, sépare les interfaces, et n'exporte qu'un volume dont le réseau, l'identifiant et l'ACL collent. Le mode diagnostic masque le formatage, le RAID, le LVM et les exports. La TUI et la WebUI inspectent et annulent. Elles ne sont pas un formulaire de mise en service.

Le support de conf est un système de fichiers étiqueté `SANCONF`. Au boot, `san-conf-bind` le monte seulement si `chosen.json` est en version 2 et si l'identifiant by-id, le WWN, la série, la taille et la taille de bloc collent encore au disque. Sinon il ne monte pas, n'applique pas le réseau, n'exporte rien, et enregistre la cause. `san-restore` rétablit ensuite les cibles iSCSI dont le réseau, le volume et le WWN collent encore, avec une ACL et un CHAP, un seul portail par interface de stockage. RAID et LVM automatiques ne portent que des disques vierges, jamais le disque de boot. Un array déjà enregistré est assemblé ; un array étranger ne l'est pas. NVMe-oF TCP suit les mêmes gardes si `nvmet` et `nvmet-tcp` sont dans le noyau de l'image. `io_uring` n'est retenu que si rtslib et le noyau de l'image l'exposent. Le gouverneur `performance` n'est posé que si `cpufreq` existe. Le guide du premier boot est `docs/first-boot.md`. Le conseiller Ollama est décrit par `SAN_OLLAMA_ENDPOINT` dans `/etc/san/ollama.env` ; sans ce fichier, l'unité ne démarre pas. Aucun modèle et aucune clé ne sont dans l'ISO.
