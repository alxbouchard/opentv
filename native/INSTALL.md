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
node scripts/install.mjs <IP_TV> OpenTV.wgt /home/owner/share/tmp/sdk_tools/OpenTV.wgt OpnTV00001
# = push sdb du .wgt puis « 0 vd_appinstall OpnTV00001 <chemin> » (méthode de TizenBrewInstaller)
```
Lancer à distance : commande sdb `0 was_execute OpnTV00001.OpenTV`.

Note : avec Host PC IP ≠ 127.0.0.1, TizenBrew ne peut plus lancer ses modules. OpenTV natif n'en a pas besoin.
