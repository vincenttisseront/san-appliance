# san-appliance

Image d'appliance SAN sur Ubuntu 26.04.1 LTS (Resolute Raccoon, suite `resolute`, amd64).

Ce dépôt construit la racine et le disque. Il n'embarque pas SCST, ZFS, Docker, ni une ISO ESOS. La racine du squelette reste inscriptible : le passage en lecture seule vient avec le durcissement de livraison.

## Ce que le squelette démarre

- Node de la distribution (`/usr/bin/node`), paquet `nodejs` de resolute.
- WebUI `vincenttisseront/esos-webui` épinglée au commit `bc28ca20d76d5b551f005a6668b002e68c323c4e` (PR 20, écoute loopback). Le build se fait dans un chroot 26.04 : les modules natifs (`better-sqlite3`, `argon2`, `ssh2`) sont recompilés là, ils ne sont pas copiés depuis une image Docker.
- `san-webui.service` lance `scripts/serve-local.mjs`, qui force `127.0.0.1`. L'unité ne lance pas `.output/server/index.mjs`.
- nftables jette le port TCP 3000 quand l'interface n'est pas `lo`.
- SSH : `PermitRootLogin no`, mots de passe refusés, `AllowUsers sanadmin`. Le compte `sanadmin` n'a pas de clé dans l'image : on la dépose ensuite. `sanui` exécute la WebUI, shell `nologin`, sans sudo.
- Quatre partitions GPT : `san-esp` (512 Mio), `san-root-a` (8 Gio, reçoit la racine), `san-root-b` (8 Gio, formatée et vide), `san-data` (le reste, montée sur `/var/lib/san`).
- Secrets JWT et clé AES créés au premier boot dans `/var/lib/san/webui/webui.env` (`0640`, `sanui`). Absents du git.
- `AUTH_HEADER_TRUST_WITHOUT_TOKEN=false`.

Les écrans qui mutent un SAN passent encore par SSH dans la WebUI. Ce squelette ne les branche pas sur la machine locale, et il n'ouvre pas de SSH root vers `127.0.0.1`.

## Construire

Sur une machine qui a `mmdebstrap`, `git`, `systemd-repart` et un accès au miroir Ubuntu :

```bash
sudo image/build.sh
```

Résultat : `image/out/rootfs.tar` et, si `systemd-repart` est là, `image/out/san-appliance.raw` (18 Gio : ESP 512 Mio, deux racines de 8 Gio, `san-data` sur le reste).

L'ISO d'installation, amorçable en UEFI et en BIOS :

```bash
sudo image/iso.sh
```

Elle appelle `build.sh`, puis écrit `image/out/san-appliance.iso`. Le menu par défaut ne touche à aucun disque. L'entrée « Install » n'écrit que sur le premier disque vide, et seulement si la ligne de commande du noyau contient `san.install=1`. Le disque installé démarre avec systemd-boot, `root=LABEL=san-root-a`, la WebUI sur `127.0.0.1:3000` et SSH root refusé.

## Critère

Une VM démarrée sur cette image doit montrer `127.0.0.1:3000` seulement (`ss -H -ltn 'sport = :3000'`), répondre à `curl -fsS http://127.0.0.1:3000/api/health`, refuser `ssh root@vm`, et dropper un paquet TCP/3000 arrivé par une autre interface.
