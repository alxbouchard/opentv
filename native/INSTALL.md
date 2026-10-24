# OpenTV — application native Tizen (icône OpenTV)

Ce dossier est l'app OpenTV complète, autonome (aucune dépendance jsDelivr/GitHub au
lancement). Une fois installée, la TV affiche l'icône **OpenTV** et l'ouvre direct.

## Pourquoi une étape de signature
Samsung n'accepte que les `.wgt` signés avec **ton** certificat Samsung, lié au DUID de
ta TV. C'est exactement l'étape que tu as déjà faite pour installer TizenBrew. Elle se
fait sur ton Mac (compte Samsung), pas depuis le cloud.

DUID de ta TV : `uuid:abc7fc91-3509-47c5-99b6-c69d9c3113b8`

## Installation (3 commandes)
Pré-requis : Tizen Studio + un profil de certificat Samsung (Tools > Certificate Manager),
TV en mode développeur avec l'IP de ce Mac en « Host PC IP ».

```sh
cd /chemin/vers/OpenTV-native
TZ=~/tizen-studio/tools/ide/bin           # adapte si installé ailleurs

# 1) construire
$TZ/tizen build-web -- .

# 2) empaqueter + signer avec TON profil (remplace MONPROFIL ; liste-les avec
#    $TZ/tizen security-profiles list )
$TZ/tizen package -t wgt -s MONPROFIL -o ./dist -- ./.buildResult

# 3) connecter la TV et installer
~/tizen-studio/tools/sdb connect <IP_DE_LA_TV>
~/tizen-studio/tools/sdb devices
$TZ/tizen install -n OpenTV.wgt -- ./dist
```

Puis sur la TV : l'icône **OpenTV** apparaît dans la liste des applis. Lance-la.

## Mise à jour plus tard
Change les fichiers dans `app/`, refais les 3 commandes. (Pas de reboot, pas de cache
jsDelivr : tout est embarqué dans le `.wgt`.)
