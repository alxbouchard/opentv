# OpenTV natif — build, signature, installation

App Tizen autonome « OpenTV » (icône OpenTV). Ne dépend ni de TizenBrew ni de jsDelivr :
elle liste/lance les applis via l'API Tizen et démarre seule son service (télécommande téléphone, port 8096).

## Signature (même méthode que TizenBrew pour les TV 2017-2018)
- auteur : `opentv-author.p12` (gardé hors GitHub — le même pour chaque mise à jour, sinon la TV refuse)
- distributeur : certificat Tizen public du SDK (`sdk-public/tizen-distributor-signer.p12`, mot de passe `tizenpkcs12passfordsigner`)
- aucun compte Samsung, aucun DUID

Build (Tizen Studio CLI, Linux) :
```sh
tizen build-web -e '.*' -- native
tizen package -t wgt -s OpenTV-Old -o ./release -- native/.buildResult   # demande le mot de passe auteur
```

## Installation sur la TV (sans Tizen Studio)
Pré-requis : TV en mode développeur, « Host PC IP » = IP de la machine qui installe (ici le Mac, 192.168.2.13).

```sh
scripts/deploy.sh <IP_TV> OpenTV.wgt
# 1. push sdb du .wgt (scripts/install.mjs)
# 2. « 0 vd_appinstall OpnTV00001 <chemin> » (méthode de TizenBrewInstaller)
# 3. « 0 debug OpnTV00001.OpenTV » : relance à neuf (sinon l'instance préchargée garde l'ancienne version)
# 4. lit GET http://<IP_TV>:8096/status pour vérifier
```

## Diagnostic à distance
`GET http://<IP_TV>:8096/status` renvoie la version du service et l'état rapporté par l'écran :
`{"version":15,"client":{"build":"OpenTV v16 native","screen":"home","apps":103,"focus":"app-…"}}`.
`GET /cmd?cmd=up|down|left|right|ok|back|home` pilote l'écran (même chemin que la télécommande iPhone).

## Versions
- `BUILD` (app.js) : affiché en bas à droite. Changer aussi `?b=` dans index.html à chaque version (cache).
- `SVC_VERSION` (app.js) = `VERSION` (service/index.js) : si le service change, monter les deux ;
  l'appli lance alors le nouveau service, qui arrête l'ancien.

Note : avec Host PC IP ≠ 127.0.0.1, TizenBrew ne peut plus lancer ses modules. OpenTV natif n'en a pas besoin.
