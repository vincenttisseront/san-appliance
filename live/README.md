# ISO live Ubuntu 24.04

Image live Noble, à côté de l'image installable 26.04 (`image/`). Elle ne partitionne rien et n'installe pas le système.

```bash
sudo apt-get install -y mmdebstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools
make doctor
make iso
```

`mtools` fournit `mformat`, exigé par `make doctor` : `grub-mkrescue` s'en sert pour l'image UEFI.

`make doctor` vérifie l'hôte Ubuntu 24.04 amd64, les outils, le menu GRUB, et les signatures APT (`InRelease` de noble, noble-updates, noble-security) avec le trousseau Ubuntu. Les empreintes sont épinglées dans `live/apt-pins.sha256`. Si une signature change, `make doctor` échoue tant qu'une ligne de `live/apt-decisions.txt` n'accepte pas explicitement la nouvelle empreinte. `make iso` produit dans `dist/` :

- `san-appliance-live-24.04-amd64.iso`
- `san-appliance-live-24.04-amd64.iso.sha256`
- `packages.manifest`
- `build-metadata.json`

La première entrée GRUB est « SAN appliance live » (`boot=casper`). Il n'y a pas d'entrée d'installation. La seconde est le mode diagnostic (`san.mode=diagnostic`) : elle n'exporte pas de LUN et ne formate rien. `nopersistent`, `raid=noautodetect` et `systemd.gpt_auto=0` empêchent casper, mdadm et systemd d'écrire un volume ou d'assembler un array tout seuls. `toram` n'est pas dans le menu : le boot mesure la RAM et laisse le paramètre absent. Le démarrage BIOS et UEFI passe par GRUB (El Torito). Secure Boot n'est pas pris en charge : l'ISO n'est pas signée. `md5sum.txt` est à la racine pour `casper-md5check`. La construction n'est pas reproductible bit à bit.

Le compte `sanadmin` n'est plus en autologin. Un mot de passe à usage unique est écrit sur la console série, puis verrouillé après la première session. Il n'est pas un secret de l'ISO. SSH est installé, refuse root et refuse les mots de passe, et n'écoute que l'adresse d'administration une fois le support de conf accepté. Aucune clé n'est dans l'image.

Au boot, si le réseau répond, la dernière release GitHub de `vincenttisseront/san-appliance` (`san-appliance-app.tar.gz` et son fichier `.sha256`) est appliquée toute seule. L'empreinte est vérifiée avant d'écrire le slot inactif. Une archive plus grosse que 256 Mio, ou plus ancienne que le slot actif, est refusée : le pointeur ne bouge pas. Le diff est écrit avant ce pointeur, pour être lu, sans confirmation. Si une session WebUI est en train d'écrire, l'application attend. Si le nouveau slot ne répond pas, le pointeur revient une fois au slot précédent. Un échec de téléchargement ne modifie pas ce pointeur, ni les exports, ni iSCSI. Chaque slot porte la date `installed`. La page `https://127.0.0.1:3000/appliance-update` montre le slot actif, le plus récent, et le diff. Le bouton « Revenir au slot précédent » est le seul geste manuel. Le jeton, s'il existe, est lu dans `/etc/san/github.token` (mode 0600) au moment de l'appel et n'est pas mis dans l'ISO.

La conf téléchargeable est `san-config.json`, version 2, déjà écrite à chaque sauvegarde. Le CHAP y est scellé, pas en clair. Déposer ce fichier dans `/var/lib/san/conf/incoming/` le fait importer au boot : chaque LUN est rattaché au volume qui a le même WWN. Un WWN absent est ignoré, et la cause est une alerte. Il n'y a pas d'écran de correspondance. Le mode usine efface cette conf et garde les données des LUN, mais seulement si `/etc/san/factory` est présent. Un démarrage normal ne crée pas ce fichier.

L'image embarque la WebUI loopback (`esos-webui`, pin `image/third_party/esos-webui.sha256`). `live/install-webui.sh` la reconstruit sur l'hôte Noble, sans Docker, avec Node 22.23.4, puis copie le bundle et ce binaire dans le rootfs. `san-webui.service` lance `front.mjs`, qui parle HTTPS sur `127.0.0.1:3000` et laisse Nitro sur `127.0.0.1:3010`. Le certificat est régénéré à chaque boot dans `/var/lib/san/webui/tls`. La clé privée n'est pas dans l'ISO. La session locale dure dix minutes d'inactivité. Elle ne porte pas iSCSI : un cookie expiré ou une WebUI arrêtée laisse les cibles en place. Le compte `sanui` n'a pas de shell de connexion. `NUXT_JWT_SECRET` et `NUXT_ENCRYPTION_KEY` sont créés au boot dans `/var/lib/san/webui/webui.env` et ne sont pas dans l'ISO.

L'image embarque `python3-rtslib-fb`, `targetcli-fb`, les modules `target_core_mod` et `iscsi_target_mod`, et la TUI `san-tui` (`live24/tui`). Le boot n'exporte rien tant que `/etc/san/chosen.json` est absent. Le mode diagnostic masque `san-restore.service`.

Le support de conf est un système de fichiers étiqueté `SANCONF`, choisi explicitement. Au boot, `san-conf-bind` le monte seulement si `chosen.json` est en version 2 et si l'identifiant by-id, le WWN, la série, la taille et la taille de bloc collent encore au disque. Sinon il ne monte pas, n'applique pas le réseau, n'exporte rien, et enregistre la cause. `san-restore` rétablit ensuite les cibles iSCSI dont le volume colle encore, avec une ACL et un CHAP, un seul portail par interface de stockage. RAID et LVM ne portent que les membres enregistrés. NVMe-oF TCP suit les mêmes gardes si `nvmet` et `nvmet-tcp` sont dans le noyau de l'image. Le guide du premier boot est `docs/first-boot.md`. Le conseiller Ollama est décrit par `SAN_OLLAMA_ENDPOINT` dans `/etc/san/ollama.env` ; sans ce fichier, l'unité ne démarre pas. Aucun modèle et aucune clé ne sont dans l'ISO.
