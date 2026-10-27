# Compatibilité HDMI-CEC OpenTV — protocole multimarque

Ce document sépare les preuves terrain des hypothèses. Une détection CEC ou une adresse logique ne prouve pas que les
six touches nécessaires atteignent l'interface. Chaque marque et chaque gamme doit refaire le protocole complet.

## Contrat OpenTV

Priorité de contrôle :

1. télécommande native de la TV par HDMI-CEC ;
2. télécommande Web locale sur téléphone ;
3. télécommande OpenTV USB/2,4 GHz.

OpenTV se déclare comme **Playback Device** sous le nom `OpenTV`. Le démarrage automatique peut enregistrer le lecteur
et remapper OK, mais ne doit jamais réveiller la TV, prendre l'entrée HDMI ou réannoncer périodiquement la source.
L'annonce active reste un geste terrain manuel et consenti, uniquement si la TV répond qu'elle est allumée :

```sh
sudo systemctl start opentv-cec-announce.service
```

Ne jamais modifier un compte TV, un menu de service ou un réglage usine pour faire passer la gate.

## Samsung testée — preuve du 2026-10-27

Téléviseur Samsung d'Alex (modèle non consigné), fonction CEC Samsung **Anynet+**. Raspberry Pi 4 sous Raspberry Pi OS
Legacy Bookworm 64 bits, pilote noyau `vc4_hdmi`.

### Enregistrement observé

- adaptateur : `/dev/cec0`, `vc4-hdmi-0` ;
- adresse physique : `1.0.0.0` ;
- adresse logique : `4`, **Playback Device 1** ;
- nom OSD : `OpenTV` ;
- RC passthrough : actif ;
- deuxième port `/dev/cec1` : non connecté, adresse `f.f.f.f`.

### Touches réellement reçues

Après une annonce active manuelle et consentie, le bus a montré des messages TV → Playback Device 1
`USER_CONTROL_PRESSED` / `USER_CONTROL_RELEASED`, puis le noyau a produit :

| Geste Samsung | Commande CEC | Code Linux observé | Action OpenTV |
|---|---|---:|---|
| Haut | `up` | `KEY_UP` 103 | monter / Réglages selon le contexte |
| Bas | `down` | `KEY_DOWN` 108 | rangée suivante |
| Gauche | `left` | `KEY_LEFT` 105 | carte/bouton précédent |
| Droite | `right` | `KEY_RIGHT` 106 | carte/bouton suivant |
| OK | `select` (`0x00`) | remappé vers `KEY_ENTER` 28 | ouvrir/valider |
| Retour | `back` (`0x0d`) | `KEY_EXIT` 174 | fermer/revenir |

Le remappage de Select est nécessaire : `KEY_OK` 352 n'a pas de symbole XKB exploitable par Chromium dans cette pile,
donc le bootstrap CEC remappe seulement le scancode CEC `0x00` vers `KEY_ENTER`, sans remplacer une table étrangère.

### Interaction avec le menu Samsung Home

Comportement visuel confirmé par Alex :

1. sur l'entrée HDMI OpenTV, la télécommande Samsung contrôle OpenTV ;
2. **Home** ouvre le menu Samsung et les touches contrôlent alors le menu Samsung ;
3. **Home** ou **Retour** ferme cette surcouche ;
4. le contrôle revient automatiquement à OpenTV.

Le HDMI OpenTV reste affiché sous la surcouche Samsung. Pendant l'ouverture et la fermeture de Home, `/dev/cec0` n'a
reçu ni `MENU_STATUS`, ni `ROUTING_CHANGE`, ni autre événement permettant au Pi de détecter la surcouche. Une annonce
manuelle `ACTIVE_SOURCE` a rétabli immédiatement le routage des touches lors du diagnostic, puis le cycle Home/Retour a
fonctionné normalement. **Ne pas ajouter de keepalive ou de réannonce périodique** : cela fermerait le menu Samsung et
pourrait interrompre les applications natives.

### Verdict Samsung actuel

- six touches : **PASS** ;
- navigation visible OpenTV : **PASS** ;
- aller-retour Home Samsung → OpenTV : **PASS** ;
- aucune prise d'entrée automatique : **PASS** ;
- persistance après extinction/rallumage complet de la TV et après un nouveau redémarrage du Pi sans annonce terrain :
  **à refaire** avant de classer cette gamme comme plug-and-play sans réserve.

Classe provisoire : **B — compatible après activation CEC terrain bornée**.

## Protocole obligatoire pour une nouvelle marque

Filmer ou observer l'écran et enregistrer simultanément le bus et l'entrée noyau. Ne jamais conclure sur le seul nom
du périphérique.

1. **Préflight**
   - sélectionner manuellement l'entrée HDMI OpenTV ;
   - `opentv-pi cec` : `/dev/cec0` lisible, adresse physique différente de `f.f.f.f`, lecteur logique réclamé ;
   - noter marque, modèle, année, version firmware, nom commercial du CEC et port HDMI.
2. **Sans annonce active**
   - appuyer une fois : Haut, Bas, Gauche, Droite, OK, Retour ;
   - exiger pression et relâchement au bus, code noyau et réaction visible pour les six.
3. **Activation terrain si nécessaire**
   - TV allumée, consentement explicite ;
   - lancer une seule fois `opentv-cec-announce.service` ;
   - répéter les six touches. Aucun succès partiel n'est un PASS.
4. **Menu de la TV**
   - Home → naviguer dans le menu → Home ou Retour pour fermer ;
   - confirmer que les touches reviennent à OpenTV sans boucle ni vol d'entrée ;
   - noter les messages `MENU_STATUS`, `ROUTING_*`, `ACTIVE_SOURCE` éventuels.
5. **Redémarrage et alimentation**
   - redémarrer le Pi, puis refaire les six touches sans annonce forcée ;
   - mettre la TV en veille pendant que le Pi reste allumé : OpenTV ne doit pas la réveiller ;
   - rallumer la TV et refaire le cycle Home ;
   - répéter après extinction froide si le produit cible ce scénario.
6. **Replis**
   - si une touche manque : télécommande téléphone immédiatement disponible ;
   - tester ensuite un HID USB standard ; ne pas masquer un échec CEC derrière le repli.

Commandes de preuve, sans URL ni secret :

```sh
sudo cec-ctl -d /dev/cec0 -m --monitor-time 120 --wall-clock
cat /proc/bus/input/devices
sudo /opt/opentv/bin/opentv-pi cec
journalctl -b -u opentv-cec.service -u opentv-cec-announce.service --no-pager
```

## Matrice multimarque

| Marque / nom CEC | Modèle / firmware | Sans annonce | Après annonce consentie | Six touches | Home aller-retour | Reboot | Classe |
|---|---|---|---|---|---|---|---|
| Samsung / Anynet+ | modèle à consigner | non confirmé | oui | PASS | PASS | partiel | B provisoire |
| LG / SIMPLINK | à tester | — | — | — | — | — | — |
| Sony / BRAVIA Sync | à tester | — | — | — | — | — | — |
| Panasonic / VIERA Link | à tester | — | — | — | — | — | — |
| TCL / HDMI-CEC | à tester | — | — | — | — | — | — |
| Hisense / HDMI-CEC | à tester | — | — | — | — | — | — |
| Roku TV / 1-touch play | à tester | — | — | — | — | — | — |

Classes : **A** = six touches et reprise après reboot sans intervention ; **B** = six touches après une activation terrain
bornée ; **C** = partiel avec repli requis ; **D** = CEC inutilisable pour OpenTV.
