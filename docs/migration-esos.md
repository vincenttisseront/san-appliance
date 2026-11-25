# Migration depuis ESOS

Ce guide sert à relever une configuration ESOS avant d’en recréer les exports à la main. Il ne fournit pas d’import. Ni `scst.conf`, ni un export SCST, ni un binaire ESOS n’est chargé par une commande de ce dépôt.

Référence lue : [quantum/esos](https://github.com/quantum/esos) `142e23aaa0f64020be998b53f9ee3079089c7986`. Les faits ci-dessous viennent de `etc/rc.d/rc.scst`, `tui/menu_targets.c`, `tui/menu_devices.c` et `etc/fstab`. Aucun script ESOS n’a été exécuté.

L’image Ubuntu 26.04 déjà construite dans `image/` applique un `scst.conf` vide. Ce n’est pas un chargeur de configuration ESOS. L’équivalent Ubuntu 24.04 visé par `docs/esos-parity.md` est LIO (`targetcli`), qui ne lit pas `scst.conf`.

## Ce qu’il faut relever

Faire ce relevé sur l’ESOS encore seul maître du volume, en lecture. Noter les valeurs dans un document hors du git et hors de l’ISO. Ne pas y coller un secret CHAP : le transférer par un canal séparé, et ne pas le laisser dans un journal.

| Élément | Où le lire sur ESOS | Pourquoi le conserver |
| --- | --- | --- |
| IQN de la cible | Bloc `TARGET_DRIVER iscsi` / `TARGET` de `/etc/scst.conf`, ou le nom sous `/sys/kernel/scst_tgt/targets/iscsi/` | Les initiateurs mémorisent cet IQN. En changer les force à reconfigurer. |
| IQN des initiateurs et groupes | `GROUP` / `INITIATOR` dans le même fichier, ou `ini_groups` dans sysfs | C’est l’ACL. Un initiateur absent ne doit pas voir le LUN. |
| Numéro de LUN | `LUN` dans la cible et dans chaque groupe | Un LUN renuméroté casse les chemins déjà montés. |
| WWID / série / NAA | Attributs du device SCST (`t10_dev_id`, `usn`) et, pour le disque backend, `/dev/disk/by-id` | La nouvelle appliance doit désigner ce disque par un identifiant stable, pas par `/dev/sdX`. |
| Taille de bloc | `block_size` choisi dans la TUI pour un vdisk, ou `queue/logical_block_size` du disque backend | Une taille différente change l’identité vue par l’initiateur. |
| Handler et chemin | `vdisk_blockio`, `vdisk_fileio` ou `vdisk_nullio`, et le fichier ou le disque associé | `vdisk_nullio` n’a pas de données à migrer. Un fichier et un disque bloc ne se remplacent pas sans copie. |
| CHAP | Utilisateurs entrants s’ils sont présents dans la conf SCST | À ressaisir sur la nouvelle cible. Jamais dans l’ISO, le git ou les logs. |
| Réseau de la cible | Adresse du portail iSCSI, distincte de l’administration si c’est le cas | La nouvelle cible n’écoute pas sur toutes les interfaces. |

Commandes de lecture, à lancer sur ESOS, sans réécrire la conf :

```sh
scstadmin -check_config /etc/scst.conf
ls -l /dev/disk/by-id
```

`scstadmin -write_config` et `scstadmin -config` modifient l’état : ne pas s’en servir pour un simple relevé.

## Recréation

1. Arrêter les exports ESOS et vérifier qu’aucun initiateur n’a encore le volume ouvert en écriture.
2. Sur la nouvelle appliance, créer la cible, le portail, le LUN, l’ACL et le CHAP à partir du relevé.
3. Comparer IQN, numéro de LUN, WWID et taille de bloc avec le relevé avant d’autoriser un initiateur.
4. Un écart sur l’un de ces quatre points : ne pas activer la cible. Corriger ou revenir à ESOS.
5. Il n’y a pas d’outil d’import SCST à lancer, ni vers LIO, ni vers l’image 26.04.

## Retour arrière

Le média ESOS reste intact pendant l’essai. `install.sh` peut avoir conservé un slot SECONDARY ; GRUB démarre PRIMARY par défaut (`misc/grub.cfg`). Ne pas reformater `esos_conf` ni `esos_data`.

1. Désactiver les exports de la nouvelle appliance.
2. Vérifier que le volume n’y est plus ouvert.
3. Démarrer l’ESOS d’origine (entrée PRIMARY, ou SECONDARY si c’est elle qui portait la conf connue).
4. Seulement ensuite, relancer les exports ESOS.
5. Si la nouvelle appliance a déjà écrit sur le volume, le retour ne restaure pas les blocs. Il évite seulement une double écriture de plus. La copie de secours, si elle existe, est le seul retour des données.

## Ce que cette branche ne fait pas

Aucun outil de ce dépôt n'importe `scst.conf`, un handler SCST, ni un binaire ESOS. LIO ne lit pas cette conf. Il n'y a pas de volume ouvert en écriture des deux côtés, même le temps d'une bascule. Le retour à la conf précédente de l'appliance 24.04 est `san-tui rollback` : il reprend la sauvegarde versionnée, il ne crée pas une cible.

## Un seul écrivain

Le même volume ne doit jamais être ouvert en écriture par ESOS et par la nouvelle appliance en même temps. Les deux serveurs ne se partagent pas le disque « le temps de la bascule ».

Ordre sûr : ESOS arrêtée et volume fermé, puis la nouvelle cible. Pour revenir : nouvelle cible arrêtée et volume fermé, puis ESOS. Une lecture de contrôle sur le serveur qui n’exporte pas ne dispense pas de cette règle dès qu’une écriture est possible.
