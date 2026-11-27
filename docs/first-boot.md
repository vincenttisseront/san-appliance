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

NTP, le relevé d'erreurs SCSI, la latence par LUN, les canaux de mise à jour et l'image de secours démarrent sans formulaire. Un bouton de la WebUI ne fait qu'annuler un choix déjà fait. Leur échec ne coupe pas iSCSI.
