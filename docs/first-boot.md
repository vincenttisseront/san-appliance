# Premier démarrage

Au boot, l'appliance choisit elle-même. Il n'y a pas de formulaire à remplir. Un contrôle manuel, dans la TUI ou la WebUI, ne fait qu'annuler un choix déjà posé (`san-tui rollback` ou le retour d'un slot).

1. Le mot de passe affiché une fois sur la console série ouvre la session locale `sanadmin`. Il ne sert qu'une fois, et il n'ouvre pas SSH.
2. SSH n'accepte pas root et n'accepte pas de mot de passe. La clé de `sanadmin` se dépose sur le support de conf, dans `authorized_keys`, après que l'identifiant du support a été reconnu.
3. S'il n'existe pas encore de système de fichiers `SANCONF`, le plus grand disque vierge d'au moins 512 Mio, qui n'est pas le disque de boot, reçoit ce label et `chosen.json`. Un disque plus petit, monté, actif ou d'amorçage n'est pas touché.
4. La première interface hors boucle est l'administration. Chacune des suivantes est un réseau de stockage, avec un seul portail iSCSI. Le MTU reste 1500 tant que les deux extrémités n'annoncent pas 9000.
5. Un export n'est créé que pour un volume distinct du support de conf, et seulement après qu'un initiateur a été vu. L'ACL et le CHAP sont générés alors. Le secret est écrit une fois sur la série et dans `secrets.json` (mode 0600). Il n'est ni dans l'ISO, ni dans un journal.
6. Le mode diagnostic n'exporte pas et ne formate pas.
7. `toram` n'est pas sur la ligne de commande de cette ISO. Une mesure de RAM est écrite dans `/run/san/toram.json`. Elle ne suffit pas à l'activer ici.
8. RAID et LVM ne sont créés que sur des disques explicitement vierges, jamais sur le disque de boot. Un array déjà enregistré est assemblé. Un array étranger n'est pas repris.
9. Une release GitHub plus récente est appliquée au boot si aucune session iSCSI n'est en écriture. Un échec de téléchargement, du conseiller Ollama ou d'un journal distant laisse iSCSI en place. Le retour automatique reprend le slot précédent, puis l'image d'origine si ce slot ne répond pas.

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
NTP, le relevé d'erreurs SCSI, la latence par LUN, les canaux de mise à jour et l'image de secours démarrent sans formulaire. Un bouton de la WebUI ne fait qu'annuler un choix déjà fait. Leur échec ne coupe pas iSCSI.

Les disques exportés passent en ordonnanceur `none`, le read-ahead d'un LUN bloc brut est coupé, et `nr_requests` prend la profondeur de file mesurée sur le disque. Les IRQ du lien de stockage quittent les cœurs d'administration quand la liste d'IRQ existe. TSO et GRO restent comme la carte les annonce. UNMAP est actif, sans option. ImmediateData et FirstBurstLength ne changent que si le banc a bougé. Chaque réglage est posé seul et retiré si le p99 empire. Le chemin rapide n'est pas chiffré. Le tableau IOPS et p99 est écrit tout seul. `san-tui block-undo` retire ces choix. Un échec laisse iSCSI en service.
Le certificat HTTPS de la WebUI est régénéré à chaque démarrage dans `/var/lib/san/webui/tls`. La clé privée n'est pas dans l'ISO. La session WebUI expire après dix minutes sans activité. iSCSI n'en dépend pas. Le mode usine n'efface la configuration que si le fichier `/etc/san/factory` est présent, et il ne touche pas aux données des LUN. Un démarrage normal ne crée pas ce fichier.
