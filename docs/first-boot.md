# Premier démarrage

Cette image ne formate rien et n'exporte rien tant qu'un support de conf n'a pas été choisi explicitement.

1. Le mot de passe affiché une fois sur la console série ouvre la session locale `sanadmin`. Il ne sert qu'une fois, et il n'ouvre pas SSH.
2. SSH n'accepte pas root et n'accepte pas de mot de passe. La clé de `sanadmin` se dépose sur le support de conf, dans `authorized_keys`, après que l'identifiant du support a été reconnu.
3. Choisir un disque qui n'est ni le média d'amorçage, ni un disque monté, ni un disque déjà actif. L'identifiant est un lien `/dev/disk/by-id`, pas un nom `sdX`.
4. Séparer l'interface d'administration et l'interface de stockage. Le portail iSCSI écoute seulement l'adresse de stockage.
5. Aucun LUN n'est exporté sans ACL d'initiateur et sans CHAP. Le secret ne va ni dans l'ISO, ni dans un journal.
6. Le mode diagnostic du menu n'inventorie que la machine. Il n'exporte pas et ne formate pas.
7. `toram` n'est pas sur la ligne de commande de cette ISO. Une mesure de RAM est écrite dans `/run/san/toram.json`. Elle ne suffit pas à l'activer ici.
8. Le retour à la conf précédente est `san-tui rollback`. Les écrans de la TUI et la WebUI, si elle est présente, ne créent pas d'export.

Ollama est un conseiller à côté. Sans `SAN_OLLAMA_ENDPOINT` dans `/etc/san/ollama.env`, il ne démarre pas. Il ne choisit qu'un profil déjà mesuré, un seul réglage, et il retire ce réglage si le p99 empire. Un échec de ce service ne coupe ni le réseau, ni iSCSI, ni le support de conf.

Rien de ce qui suit ne se demande au premier boot. Si le matériel ou le module manque, la fonction est ignorée et iSCSI continue.

- Un disque déjà classé libre, vide, et assez grand peut devenir le spare d'un array enregistré. Un disque de boot, exporté, ou qui contient déjà des données n'est pas pris. `san-tui raid-spare-undo` le retire.
- La reconstruction est plafonnée à 50 000 Kio/s, le scrub hebdomadaire à 20 000 Kio/s. `san-tui raid-pace-undo` retire le plafond, `san-tui raid-scrub-stop` arrête le check en cours.
- SMART en échec et une température de disque au-dessus de 55 °C produisent une alerte. Les exports ne changent pas.
- Chaque adresse IPv4 hors de l'interface d'administration est un portail. L'initiateur continue d'utiliser l'adresse IP. Si une adresse disparaît, ce portail est retiré et les autres restent ; le dernier n'est pas retiré. `san-tui portal-undo` retire les portails ajoutés en plus de l'adresse de stockage.
- iSER n'est activé que si `ib_isert` est dans l'image. Sinon une alerte le dit et le portail TCP reste. `san-tui iser-undo` le désactive.
- Le MTU 9000 n'est posé qu'après un ping dont le bit DF est positionné. S'il échoue, ou s'il n'y a pas de passerelle, l'interface reste à 1500. `san-tui mtu-undo` fixe 1500.
- Deux interfaces de stockage sans adresse propre sont agrégées. Sans annonce LACP du commutateur, le mode est active-backup. `san-tui bond-undo` retire l'agrégat.
- Un nom DNS déjà publié est affiché à côté du portail. Il ne remplace pas l'adresse. S'il n'y a pas de nom, rien n'est demandé.
