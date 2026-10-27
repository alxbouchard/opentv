# Compatibilité HDMI-CEC OpenTV — protocole multimarque

Ce document sépare les preuves terrain des hypothèses. Une détection CEC ou une adresse logique ne prouve pas que les
six touches nécessaires atteignent l'interface. Chaque marque et chaque gamme doit refaire le protocole complet.

## Contrat OpenTV

Priorité de contrôle :

1. télécommande native de la TV par HDMI-CEC ;
2. télécommande Web locale sur téléphone ;
3. télécommande OpenTV USB/2,4 GHz.

OpenTV se déclare comme **Playback Device** sous le nom `OpenTV` et remappe OK. Il fait **une seule annonce de source
active par démarrage de la boîte** (minuterie, fenêtre de 240 s après le boot, TV allumée seulement, témoin lié au
`boot_id` écrit après succès seulement). Il ne doit jamais réveiller la TV ni réannoncer périodiquement la source : après
cette annonce, ni la minuterie ni un redémarrage de service n'émet plus rien (Samsung Home et les applis natives ne
sont pas interrompus).

Séquence émise, identique pour l'annonce de démarrage et la réactivation explicite :

1. `<Give Device Power Status>` vers la TV — seule question ; toute réponse autre que `on` (veille, transition,
   silence) arrête tout ;
2. `<Active Source>` avec l'adresse physique lue ;
3. `<Menu Status activated>`.

**Jamais `<Image View On>`, `<Text View On>`, `<Standby>`, ni touche ou commande d'alimentation simulée** : `cecboot`
refuse ces options avant de lancer `cec-ctl` (`NEVER_EMIT`, testé). Réactivation explicite — récupération
administrateur/terrain, uniquement si la TV répond qu'elle est déjà allumée :

```sh
sudo /opt/opentv/bin/opentv-pi cec --activate   # = sudo systemctl start opentv-cec-announce.service
```

### Portée actuelle de l'annonce automatique (politique produit)

- L'annonce automatique unique par démarrage est **activée par défaut pour toutes les marques**
  (`OPENTV_CEC_BOOT_ANNOUNCE=1`) mais **prouvée sur le terrain uniquement sur la Samsung d'Alex** (Anynet+).
- **Toutes les autres marques sont non testées** : une TV LG, Sony, Panasonic, TCL, Hisense, Roku TV… n'est déclarée
  compatible qu'après le protocole complet ci-dessous et une ligne remplie dans la matrice multimarque. D'ici là,
  « fonctionne probablement » n'est pas un résultat ; la télécommande téléphone reste le repli annoncé.
- Une marque qui réagirait mal à l'annonce (prise d'entrée indésirable, fermeture de menu, boucle) se désactive par
  `OPENTV_CEC_BOOT_ANNOUNCE=0` en attendant un profil par marque ; consigner le cas dans la matrice.

### Compromis délibéré de la fenêtre de 240 s

L'annonce n'est tentée que pendant les 240 premières secondes après le démarrage de la boîte, TV déjà allumée.
Conséquence assumée : **si la TV est allumée pendant cette fenêtre (par exemple boîte et TV mises sous tension
ensemble), elle peut basculer une fois sur l'entrée OpenTV**, même si l'utilisateur regardait une autre source. C'est
le prix pour que la télécommande de la TV fonctionne après un redémarrage sans intervention. En revanche, **jamais après
le témoin ni après la fenêtre** : une fois l'annonce réussie (témoin lié au `boot_id`) ou passé 240 s, ni la minuterie,
ni un redémarrage de service, ni une TV rallumée plus tard ne provoquent de changement d'entrée. Une TV en veille
pendant toute la fenêtre n'est jamais réveillée ; elle ne recevra pas d'annonce automatique pour ce démarrage
(récupération : téléphone appairé, ou `opentv-pi cec --activate`).

### Lacunes connues

- aucune action utilisateur « Afficher OpenTV » (téléphone appairé, future télécommande USB) ne déclenche encore la
  réactivation bornée sans terminal ;
- OpenTV ne répond pas encore à `<Request Active Source>` / `<Set Stream Path>` émis par la TV : choisir l'entrée HDMI
  OpenTV au menu de la TV ne rétablit donc pas forcément les touches.

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

### Régression après redémarrage du Pi (2026-10-27)

Après un redémarrage réel, l'affichage était correct mais la télécommande Samsung ne pilotait plus OpenTV ;
`opentv-pi cec` montrait `announced_this_boot=false` et `opentv-cec-announce.service` lancé à la main a tout
rétabli. Cause : la minuterie ne faisait plus aucune annonce. Correctif : une seule annonce automatique par démarrage
(voir « Contrat OpenTV »), couverte par `tests/test_cec_boot_announce.py`. **À revalider sur le Pi** : redémarrage,
six touches sans intervention, puis cycle Home Samsung (≥ 3 min ouvert) sans retour forcé sur OpenTV.

### Verdict Samsung actuel

- six touches : **PASS** ;
- navigation visible OpenTV : **PASS** ;
- aller-retour Home Samsung → OpenTV : **PASS** ;
- aucune prise d'entrée automatique sous l'ancien contrat sans annonce : **PASS** ; aucune réannonce après l'annonce
  unique de démarrage : **à revalider** ;
- persistance après un nouveau redémarrage du Pi avec l'annonce unique de démarrage, puis après extinction/rallumage
  complet de la TV : **à refaire** avant de classer cette gamme comme plug-and-play sans réserve.

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
   - lancer une seule fois `sudo /opt/opentv/bin/opentv-pi cec --activate` ;
   - moniteur du bus : aucun `IMAGE_VIEW_ON` émis par OpenTV, seulement `ACTIVE_SOURCE` puis `MENU_STATUS` ;
   - répéter les six touches. Aucun succès partiel n'est un PASS.
4. **Menu de la TV**
   - Home → naviguer dans le menu → Home ou Retour pour fermer ;
   - confirmer que les touches reviennent à OpenTV sans boucle ni vol d'entrée ;
   - noter les messages `MENU_STATUS`, `ROUTING_*`, `ACTIVE_SOURCE` éventuels.
5. **Redémarrage et alimentation**
   - redémarrer le Pi, puis refaire les six touches sans `--activate` (une seule annonce de démarrage attendue :
     `journalctl -b -u opentv-cec` → un `boot_done`, puis aucune émission) ;
   - redémarrer le Pi TV en veille, attendre plus de 240 s, puis allumer la TV : aucune bascule d'entrée, aucun
     réveil (moniteur : ni `IMAGE_VIEW_ON` ni `ACTIVE_SOURCE` d'OpenTV) ;
   - redémarrer le Pi puis allumer la TV **dans** les 240 s : au plus une bascule vers OpenTV, consignée ;
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

Une marque reste **non testée** (annonce automatique active par défaut, mais aucune promesse de compatibilité) tant que
sa ligne n'a pas de modèle/firmware consigné et un résultat pour chaque colonne, y compris les deux cas de la fenêtre
de 240 s (TV allumée dans la fenêtre / après) et l'absence de réveil.
