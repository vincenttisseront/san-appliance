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

La page `http://127.0.0.1:3000/appliance-update` télécharge une release GitHub de `vincenttisseront/san-appliance` (`san-appliance-app.tar.gz` et son fichier `.sha256`). L'empreinte est vérifiée avant d'écrire le slot inactif sous le support de conf. Si le nouveau slot ne répond pas, le pointeur revient une fois au slot précédent. Un échec de téléchargement ou d'empreinte ne modifie pas ce pointeur, ni les exports, ni le réseau. Le jeton, s'il existe, est lu dans `/etc/san/github.token` (mode 0600) au moment de l'appel et n'est pas mis dans l'ISO.

L'image embarque la WebUI loopback (`esos-webui`, pin `image/third_party/esos-webui.sha256`). `live/install-webui.sh` la reconstruit sur l'hôte Noble, sans Docker, avec Node 22.23.4, puis copie le bundle et ce binaire dans le rootfs. `san-webui.service` lance `scripts/serve-local.mjs`, qui force l'écoute sur `127.0.0.1:3000`. Le compte `sanui` n'a pas de shell de connexion. `NUXT_JWT_SECRET` et `NUXT_ENCRYPTION_KEY` sont créés au boot dans `/var/lib/san/webui/webui.env` et ne sont pas dans l'ISO.

L'image embarque `python3-rtslib-fb`, `targetcli-fb`, les modules `target_core_mod` et `iscsi_target_mod`, et la TUI `san-tui` (`live24/tui`). Le boot n'exporte rien tant que `/etc/san/chosen.json` est absent. Le mode diagnostic masque `san-restore.service`.

Le support de conf est un système de fichiers étiqueté `SANCONF`, choisi explicitement. Au boot, `san-conf-bind` le monte seulement si `chosen.json` est en version 2 et si l'identifiant by-id, le WWN, la série, la taille et la taille de bloc collent encore au disque. Sinon il ne monte pas, n'applique pas le réseau, n'exporte rien, et enregistre la cause. `san-restore` rétablit ensuite les cibles iSCSI dont le volume colle encore, avec une ACL et un CHAP. L'adresse de stockage reste l'identité du portail pour l'initiateur. Les autres adresses IPv4 hors de l'interface d'administration sont des portails supplémentaires ; si l'une disparaît, ce portail est retiré et les autres continuent. Le dernier portail n'est pas retiré. Un nom DNS déjà publié est seulement affiché. iSER n'est armé que si `ib_isert` est dans l'image. Le MTU 9000 n'est posé qu'après un paquet DF ; sinon l'interface reste à 1500. Deux interfaces de stockage sans adresse propre passent en active-backup quand le commutateur n'annonce pas LACP. RAID et LVM ne portent que les membres enregistrés. Un disque déjà classé libre et vide peut être ajouté comme spare ; un disque de boot ou exporté ne l'est pas. La reconstruction est plafonnée, et un scrub hebdomadaire aussi. SMART et une température au-dessus de 55 °C sont des alertes : elles ne retirent pas d'export. NVMe-oF TCP suit les mêmes gardes si `nvmet` et `nvmet-tcp` sont dans le noyau de l'image. Le guide du premier boot est `docs/first-boot.md`. Le conseiller Ollama est décrit par `SAN_OLLAMA_ENDPOINT` dans `/etc/san/ollama.env` ; sans ce fichier, l'unité ne démarre pas. Aucun modèle et aucune clé ne sont dans l'ISO.
