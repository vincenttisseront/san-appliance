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
