# Validation matérielle

Ce plan ne conclut rien à partir d'une VM. Fibre Channel, FCoE, SRP et la haute disponibilité ne sont pas couverts par les essais QEMU de cette branche. Un essai sur machine virtuelle ne remplace aucun des points ci-dessous.

Chaque essai se fait sur un banc isolé, avec des disques dont la perte est acceptable. On relève l'identifiant stable avant et après. On ne reformate pas un volume qui contient encore des données à conserver. On n'écrit pas depuis deux contrôleurs en même temps.

## Fibre Channel

Matériel : un HBA cible réellement présent, un commutateur et un initiateur dont le WWPN est connu.

1. Le port passe lien up et le commutateur voit le WWPN de la cible.
2. Un LUN n'est visible que pour le WWPN listé. Un autre WWPN ne le voit pas.
3. Après un retrait du câble puis un retour, le même LUN et le même WWID reviennent.
4. Le mode diagnostic de l'appliance n'exporte pas ce LUN.

## FCoE

Matériel : une CNA ou une NIC FCoE, et un commutateur qui annonce le VLAN FCoE sur cette interface seulement.

1. La session monte sur l'interface choisie, pas sur les autres.
2. Un initiateur non listé ne voit pas le LUN.
3. Couper le VLAN retire la session sans toucher aux disques non sélectionnés.

## SRP

Matériel : un HCA InfiniBand et un fabric. Le subnet manager n'est pas lancé par l'appliance, sauf s'il est déjà le rôle explicite d'une autre machine du banc.

1. Le lien IB est up.
2. La cible SRP répond à l'initiateur autorisé.
3. Un second initiateur non listé n'obtient pas le LUN.

## Haute disponibilité

Matériel : deux nœuds, un réseau de cluster distinct du réseau de stockage, et un mécanisme de fence identifié. Un nœud seul ne se déclare pas cluster.

1. Au départ, un seul nœud exporte le volume. L'autre ne l'ouvre pas en écriture.
2. Après une bascule décidée, le premier nœud a relâché le volume avant que le second l'exporte.
3. Un retour arrière reprend le même ordre : le second relâche, puis le premier exporte.
4. Une coupure du réseau de cluster ne doit pas laisser les deux nœuds écrire.

Tant que ces essais n'ont pas été faits sur ce matériel, le statut reste non couvert.
