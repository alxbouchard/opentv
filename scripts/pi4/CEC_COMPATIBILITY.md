# Compatibilité HDMI-CEC OpenTV — protocole multimarque

Ce document sépare les preuves terrain des hypothèses. Une détection CEC ou une adresse logique ne prouve pas que les
six touches nécessaires atteignent l'interface. Chaque marque et chaque gamme doit refaire le protocole complet.

## Contrat OpenTV

Priorité de contrôle :

1. télécommande native de la TV par HDMI-CEC ;
2. télécommande Web locale sur téléphone ;
3. télécommande OpenTV USB/2,4 GHz.

OpenTV se déclare comme **Playback Device** sous le nom `OpenTV` et remappe OK. Après une nouvelle réclamation d'adresse
logique, il laisse d'abord cette adresse se stabiliser : le passage suivant de la minuterie, 30 s plus tard, fait
**une seule annonce de source active par démarrage de la boîte** (fenêtre de 240 s après le boot, TV allumée seulement,
témoin lié au `boot_id` écrit après succès seulement). Ce découpage est requis sur la Samsung terrain : une annonce
envoyée dans la même transaction que la réclamation était acquittée mais ne rétablissait pas le routage des touches.
OpenTV ne doit jamais réveiller la TV ni réannoncer périodiquement la source : après cette annonce, ni la minuterie ni
un redémarrage de service n'émet plus rien (Samsung Home et les applis natives ne sont pas interrompus).

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

### Reprise après une application native de la TV (0.7.3)

Seule autre annonce automatique, assurée par `opentv-cec-return.service` (module `cecreturn`) : la **même** séquence
sûre que ci-dessus, rejouée **une fois** quand la TV émet elle-même, dans l'ordre et en diffusion depuis l'adresse
logique 0 :

1. `ACTIVE_SOURCE` phys-addr 0.0.0.0 (la TV passe sur une application native), au plus 12 h avant l'étape 2 ;
2. `ROUTING_CHANGE` 0.0.0.0 → adresse physique **lue** de l'adaptateur OpenTV ;
3. `SET_STREAM_PATH` vers cette même adresse, au plus 5 s après l'étape 2.

Puis 1,5 s de calme, verrou partagé `/run/opentv-cec/lock`, relecture de l'adaptateur, et annonce seulement si la TV
répond « allumée ». Observation par `cec-ctl -s -r -m` (mode moniteur passif du noyau, `CAP_NET_ADMIN` seule dans un
réseau privé) : OpenTV ne devient ni suiveur ni initiateur sur ce descripteur, ne lit ni ne produit aucune touche.
Relecture finale de la reprise (0.7.4) : après le verrou et la question d'alimentation (TV vue allumée), juste
avant l'`ACTIVE_SOURCE` de la reprise, toutes les lignes déjà reçues par le moniteur sont relues sans attendre (mêmes
règles d'arrêt que le maintien ci-dessous) : une `ACTIVE_SOURCE` (application native rouverte, autre source), un
`STANDBY`, un routage ailleurs, une perte, une trame invalide ou un événement d'état arrivés pendant ces attentes
annulent la reprise (`"result":"cancelled"` + `reason`) — rien d'autre n'est émis, le maintien n'est pas armé, ni le
délai anti-doublon ni le quota ne sont consommés (un vrai retour ultérieur est repris normalement) ; trame encore
incomplète (`frame_pending`), fin du moniteur (`monitor_end`, fin rendue aussitôt) ou plus de 4096 lignes
(`drain_overflow`) ⇒ annulée aussi. Sans relecture installée par le moniteur (appel hors du service), reprise et
`POLL` refusent (`drain_unavailable`). `cec --activate` et l'annonce de démarrage, sans moniteur, sont inchangés.
Limite honnête, fenêtre résiduelle : une trame émise par la TV pendant le lancement même du cec-ctl de l'annonce
(quelques dizaines de ms), ou pas encore écrite par le moniteur, peut encore la précéder ; aucune lecture CEC ne la
ferme (gate terrain : YouTube rouvert aussitôt après un retour, sans vol de source).
Annulé ou jamais déclenché : autre source active, routage ailleurs, veille, ordre ou délai différent, doublon,
trame incohérente (octets bruts ≠ en-tête/adresses décodées), ligne inconnue ou trop longue, perte de messages,
redémarrage du moniteur/service (état vide), touches déjà reçues ou autre annonce pendant le calme, **Samsung Home
seul** (aucun message CEC). Allers-retours rapides : seule une annonce transmise consomme le délai anti-doublon (5 s)
et le quota (30/h) ; un retour exact pendant ce délai est reporté à sa fin (touches ou autre annonce ⇒ annulé), jamais
perdu ; `busy`, refus ou échec ne bloquent pas le retour suivant ; 60 tentatives/h au plus : `cancelled`, `busy`, refus
et échecs comptent dans le plafond de 60 tentatives/h, mais ni dans le délai anti-doublon de 5 s ni dans le quota de
30 annonces/h. Le témoin de l'annonce de démarrage n'est ni lu ni écrit. Opt-out : `OPENTV_CEC_RETURN_RECOVERY=0`.

`"result":"announced"` prouve seulement la **transmission** acquittée de `<Active Source>`/`<Menu Status>`, pas que la
TV renvoie les touches : la reprise reste un gate terrain (six touches visibles). Le moniteur n'est prêt
(`cec_return_ready`) qu'après l'événement initial du noyau portant l'adresse physique et le masque attendus ; rien
n'est armé avant. Refus du mode moniteur (stderr de cec-ctl, même avec sortie 0), cec-ctl absent, adaptateur illisible
ou 3 échecs génériques consécutifs avant « prêt » ⇒ `cec_return_failed`, sortie 78 non relancée : unité `failed`,
`opentv-pi health` avertit `cec_return_failed`. Le texte de cec-ctl n'est jamais journalisé (codes stables seulement).
Changement d'état attendu de l'adaptateur (TV éteinte/rallumée, HDMI, réclamation en cours ; aussi un événement
initial discordant avant « prêt ») : jamais compté vers la sortie 78, découverte relue toutes les 2 s pendant 60 s
puis toutes les 5 s jusqu'au prochain `cec_return_ready` — jamais d'angle mort de 30 s ; adaptateur absent ⇒ attente
explicite (`cec_return_waiting`, `"rediscover":true`), jamais « prêt », rien d'armé. La même discordance 10 fois de
suite sur le même adaptateur lu ⇒ `adapter_state_inconsistent`, sortie 78. Aucune reprise avant un **nouvel**
événement initial conforme : une séquence vue avant lui, ou en cours de calme lors du changement d'état, n'émet rien.

### Maintien neutre après la reprise (0.7.4)

Terrain Samsung du 2026-10-31 : la séquence exacte est détectée, `"result":"announced"` (seulement une question
d'alimentation, `ACTIVE_SOURCE`, `MENU_STATUS`) rend les touches… quelques secondes à minutes. L'activation manuelle est
tout aussi temporaire. Un répondeur ponctuel ou permanent à `GIVE_DEVICE_POWER_STATUS`, puis une libération/réclamation
+ annonce : **réfutés**. Un `cec-follower` standard gardait le contrôle vivant ; la capture a montré que son seul
apport utile était un `POLL` neutre vers la TV toutes les ~15 s. Canari `cec-ctl -d /dev/cec0 -s -t 0 -P` toutes les
15 s, sans suiveur ni autre réponse : six touches > 5 min, **confirmé par Alex**. `POLL` (un octet, aucun opcode)
n'annonce rien, ne réveille rien, ne change ni l'alimentation ni la source.

Distinction : **`announced` rétablit le transfert des touches ; le POLL neutre le maintient** sur cette Samsung. Contrat
(module `cecreturn`, même service, aucun mode suiveur) :

- armé **seulement** après une reprise `announced` de ce moniteur (prêt conforme + séquence exacte), en mémoire : un
  redémarrage du service ou du moniteur ne l'arme jamais ; `busy`, refus, échec, `adapter_changed` ou quota non plus.
  cec-ctl 1.22.1 sort avec 0 même sans transmission : `announced` (comme l'annonce de démarrage et `cec --activate`)
  exige la sortie exacte fusionnée stdout+stderr d'un `ACTIVE_SOURCE` transmis depuis notre adresse logique ; un
  statut `Tx, Error`/`Not Acknowledged` ou un texte parasite ⇒ `failed` (code `tx` stable), ni `MENU_STATUS`, ni
  témoin, ni maintien ;
- `POLL` vers l'adresse logique 0, au moins 15 s après la fin de l'annonce puis au moins 15 s après la fin de chaque
  `POLL` réellement émis (**jamais deux POLL à moins de 15 s sur le bus**, même quand l'attente du verrou
  `/run/opentv-cec/lock` — ≤ 3 s — retarde une sonde) ; avant chaque `POLL` dû, les lignes déjà reçues du moniteur
  sont lues sans attendre (une `ACTIVE_SOURCE` déjà arrivée désarme avant toute émission) ; relecture de l'adaptateur
  surveillé (le nôtre : vc4, Playback « OpenTV », même adresse physique, même masque logique) ; puis, juste avant
  l'émission — après l'attente du verrou et la relecture de l'adaptateur, verrou tenu —, nouvelle lecture non
  bloquante des lignes déjà reçues, mêmes règles d'arrêt : une trame d'arrêt arrivée pendant ces attentes annule ce
  `POLL` et désarme ; trame encore incomplète ⇒ `frame_pending`, plus de 4096 lignes ⇒ `drain_overflow` (annulé,
  désarmé) ; course résiduelle limitée au lancement de cec-ctl lui-même ; argv figé `cec-ctl -d <adaptateur> -s -t 0 -P` (adaptateur découvert, jamais
  `NEVER_EMIT`, annonce ni question d'alimentation) ; succès = sortie exacte d'un POLL acquitté par la TV depuis une de
  nos adresses logiques ;
- arrêt immédiat (`"event":"cec_return_keepalive","state":"disarmed"` + raison) : toute `ACTIVE_SOURCE` reçue
  (TV/application native 0.0.0.0 → `native_app`, autre source → `other_source`), routage ou `SET_STREAM_PATH` vers une
  autre adresse, `STANDBY`, `INACTIVE_SOURCE`, perte de messages, trame incohérente/ligne inconnue, événement d'état de
  l'adaptateur, fin du moniteur, POLL non acquitté (`poll_nack`) ou en échec, verrou occupé, adaptateur/adresse
  logique changés, reprise suivante non annoncée ;
- continue tant que notre adresse reste choisie : Samsung Home (aucune trame), touches et trafic ordinaire ne
  l'arrêtent pas et ne déclenchent **jamais** d'annonce (pendant Home, le `POLL` neutre est la seule émission) ; jamais d'`ACTIVE_SOURCE`, de `MENU_STATUS` ni de question
  d'alimentation périodiques ; une autre entrée HDMI n'est jamais reprise ; une TV éteinte n'est jamais réveillée ;
- journal : `armed`, `active` (première sonde, puis une ligne par heure au plus), `disarmed` — jamais une ligne par
  POLL ni de donnée brute du bus. Opt-out du maintien seul : `OPENTV_CEC_RETURN_KEEPALIVE=0`, lu au démarrage du
  service seulement — l'appliquer par `sudo systemctl restart opentv-cec-return.service`.

Un POLL acquitté est une preuve de **transport** seulement, jamais de reprise des touches : les six touches visibles,
immédiatement puis après une fenêtre de stabilité, restent la seule preuve (gate « Maintien après retour »).

Preuve terrain (2026-10-31 ~16:08, production 0.7.3 + canari POLL manuel) : POLL acquittés toutes les 15 s (séquences
711–718), adaptateur sain (le nôtre, PA 1.0.0.0, masque 0x10), mais la télécommande ne pilotait plus OpenTV et aucun
retour exact n'était survenu depuis 14:37:48 ; `sudo /opt/opentv/bin/opentv-pi cec --activate` (annonce sûre) a
rétabli immédiatement. Un POLL acquitté maintient un renvoi des touches existant mais ne restaure pas un renvoi déjà
perdu : des POLL vus sur le bus avant une reprise `announced` n'arment jamais le maintien et ne valent jamais reprise
; la seule restauration automatique reste l'annonce unique déclenchée par la séquence exacte de la TV (sinon `cec
--activate`), jamais d'annonce périodique.

### Suiveur exclusif minimal (0.7.6)

Terrain Samsung : la TV cesse parfois d'émettre `USER_CONTROL` après 30 s à 5 min malgré des POLL acquittés et un
service sain ; une réannonce `ACTIVE_SOURCE` rétablit. Sans suiveur, le noyau répond `FEATURE_ABORT` à
`GIVE_DEVICE_POWER_STATUS` ; après extinction/rallumage de la TV, le moniteur voit le changement d'état puis « prêt »
mais rien ne rétablit l'entrée avant `cec --activate`. Un `cec-follower -m -s` complet maintenait les touches ; un
canari tardif en espace utilisateur était invalide (l'abandon automatique du noyau partait d'abord). Contrat
(`opentv-cec-follower.service`, module `cecfollower`, service dédié : le moniteur textuel de reprise reste inchangé) :

- adaptateur vc4 découvert (jamais `/dev/cec0` en dur) dont la configuration est la nôtre (Playback « OpenTV »,
  posée par `opentv-cec.service`) ; mode `INITIATOR|EXCL_FOLLOWER` (0x21) relu, **jamais passthrough** (0x31 refusé
  par le transport) ; aucune capacité (pas de `CAP_NET_ADMIN`) ; adresse physique, adresses logiques et masque relus à
  chaque événement et juste avant chaque émission ; aucune réclamation (`S_LOG_ADDRS`/`S_PHYS_ADDR` absents) ;
  `ALLOW_RC_PASSTHRU` conservé (avertissement `rc_passthru_off` s'il manque) ;
- `GIVE_DEVICE_POWER_STATUS` dirigé vers notre adresse logique ⇒ exactement `REPORT_POWER_STATUS` « on » en < 1 s,
  plus aucun `FEATURE_ABORT` automatique ; un opcode que le noyau traite lui-même (`GET_CEC_VERSION`, `ABORT`,
  `GIVE_OSD_NAME`, `GIVE_DEVICE_VENDOR_ID`, `GIVE_FEATURES`, `GIVE_PHYSICAL_ADDR`) reçu quand même ⇒ anomalie,
  échec fermé (sortie 78, mode rendu) ;
- `SET_STREAM_PATH` diffusé par la TV, longueur exacte, vers notre adresse physique ⇒ exactement une `ACTIVE_SOURCE`
  (la TV demande explicitement notre chemin ; aucun `MENU_STATUS`) ; autre adresse, trame malformée, autre émetteur,
  adaptateur qui change (événement en file ou relecture différente juste avant l'émission) ⇒ rien. C'est la réponse
  sûre qui doit rétablir les touches après un rallumage **si** la Samsung envoie cette demande ;
- tout autre message dirigé ⇒ `FEATURE_ABORT` « unrecognized » (parité avec le noyau seul) ; jamais pour
  `FEATURE_ABORT`, `USER_CONTROL_PRESSED`/`RELEASED` (ni abandonnés ni injectés : le noyau les transforme en touches),
  `REPORT_PHYSICAL_ADDR` ni les messages de diffusion seule ; aucune réponse aux diffusions hors `SET_STREAM_PATH`
  exact (`REQUEST_ACTIVE_SOURCE` compris) ;
- **jamais** `IMAGE_VIEW_ON`, `TEXT_VIEW_ON`, `STANDBY`, question d'alimentation, POLL, annonce périodique ni
  réclamation : liste blanche de trois trames (octets exacts) vérifiée juste avant chaque `CEC_TRANSMIT` ;
- extinction/rallumage : même descripteur (le noyau conserve le mode suiveur), relecture immédiate à chaque événement
  d'état, « prêt » dès la réclamation automatique du noyau (fenêtre de retour jamais manquée, aucune scrutation) ;
  **aucune annonce sur simple HPD** : sans demande de la TV, OpenTV reste passif et la récupération reste manuelle ;
  configuration étrangère ⇒ mode rendu, redécouverte bornée ; adaptateur retiré ⇒ réouverture 0,2 s puis 1/2/5 s ;
- un seul propriétaire `EXCL_FOLLOWER` (jamais volé, `exclusive_follower_busy`), verrou partagé
  `/run/opentv-cec/lock` exigé pour `ACTIVE_SOURCE`/`FEATURE_ABORT` (attente ≤ 0,4 s), jamais attendu pour
  `REPORT_POWER_STATUS` (réponse sans effet de source, < 1 s même si l'amorçage tient le verrou), demande de plus de
  0,8 s ignorée, ≤ 10 émissions/s et 120/min,
  `ACTIVE_SOURCE` espacées de 2 s et ≤ 30/h, jamais de réémission après NACK ;
- avec le suiveur actif, **aucun POLL** : `OPENTV_CEC_RETURN_KEEPALIVE` est ignoré ; la reprise sur séquence exacte
  reste un repli (notre `ACTIVE_SOURCE` l'annule) ; l'annonce unique de démarrage et `cec --activate` sont inchangés ;
- journal sobre (`cec_follower_ready`, `cec_follower_waiting`, `cec_follower_reply`, statistiques horaires + relevé
  final à l'arrêt) ; échec véridique ⇒ sortie 78, `cec_follower_failed`, unité `failed`, `opentv-pi health` avertit
  `cec_follower_failed` ; accès refusé (course udev/`DeviceAllow` au démarrage) ⇒ sortie 1, relance toutes les 10 s
  sans limite, jamais 78.

Gate terrain : `OPENTV_CEC_FOLLOWER=canary` (même comportement + une ligne par demande, servie ou non — raison
`rx_not_ready`, `stale`, `rate_limited`, `duplicate_request`, `lock_busy`, `adapter_churn`, `tx_*` —, bornée, rien de
périodique).
Repli et rollback : `OPENTV_CEC_FOLLOWER=0` puis
`sudo systemctl restart opentv-cec-follower.service opentv-cec-return.service` (comportement 0.7.5). Tant que les
gates ne sont pas passées (bus sans `FEATURE_ABORT` 0x8f, réponse < 1 s, trois cycles YouTube de 15 min, TV
éteinte/rallumée ×3, autre entrée HDMI sans reprise, TV éteinte sans réveil), dire « restauré pour l'instant ».

### Lacunes connues

- aucune action utilisateur « Afficher OpenTV » (téléphone appairé, future télécommande USB) ne déclenche encore la
  réactivation bornée sans terminal ;
- OpenTV ne répond pas à `<Request Active Source>` ; depuis 0.7.6, le suiveur répond à un `<Set Stream Path>` exact
  de la TV vers notre adresse (une `ACTIVE_SOURCE`), mais choisir l'entrée HDMI OpenTV au menu de la TV ne rétablit
  les touches que si la TV émet cette demande (à prouver sur la Samsung) ;
- la reprise après application native n'est prouvée que sur la séquence Samsung capturée ; une autre marque qui ne
  l'émet pas exactement ne déclenche rien (repli : téléphone, `cec --activate`).

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
fonctionné normalement. **Aucune annonce, réannonce ou reprise périodique** (ni `ACTIVE_SOURCE`, ni `MENU_STATUS`, ni
réveil, ni libération/réclamation) : cela fermerait le menu Samsung et pourrait interrompre les applications natives.
Seule émission répétée admise, distincte : le POLL neutre 0.7.4 (un octet sans opcode, au moins 15 s d'écart),
autorisé seulement après une reprise `announced` de la séquence exacte de retour d'application native (jamais au
démarrage, après un redémarrage du service, sur Home seul ni par minuterie), arrêté dès qu'une application native,
une autre source, un routage ailleurs ou la veille apparaît. Qu'il laisse Home ouvert reste une gate Pi : Home
Samsung ouvert ≥ 3 min pendant le maintien, sans fermeture ni retour forcé sur OpenTV.

### Régression après redémarrage du Pi (2026-10-27)

Après un redémarrage réel, l'affichage était correct mais la télécommande Samsung ne pilotait plus OpenTV ;
`opentv-pi cec` montrait `announced_this_boot=false` et `opentv-cec-announce.service` lancé à la main a tout
rétabli. Cause : la minuterie ne faisait plus aucune annonce. Correctif : une seule annonce automatique par démarrage
(voir « Contrat OpenTV »), couverte par `tests/test_cec_boot_announce.py`. **À revalider sur le Pi** : redémarrage,
six touches sans intervention, puis cycle Home Samsung (≥ 3 min ouvert) sans retour forcé sur OpenTV.

### Retour depuis YouTube natif (2026-10-30)

Capture `cec-ctl -m` sur le Pi réel (LA 4 Playback « OpenTV », PA 1.0.0.0, Select → Entrée) :

1. avant YouTube : TV → Playback Device 1 `USER_CONTROL_PRESSED/RELEASED` (down/up), devenus événements Linux ;
2. entrée dans YouTube natif : la TV diffuse `ACTIVE_SOURCE` phys-addr 0.0.0.0 ;
3. sortie (21:49:35) : la TV diffuse `ROUTING_CHANGE` 0.0.0.0 → 1.0.0.0, puis `SET_STREAM_PATH` 1.0.0.0, puis
   `GIVE_DEVICE_POWER_STATUS` toutes les 5 s ; plus aucun `USER_CONTROL` ni événement Linux : OpenTV visible, touches
   inertes.

`sudo opentv-pi cec --activate` (question d'alimentation, `ACTIVE_SOURCE`, `MENU_STATUS` ; sans libération ni nouvelle
réclamation d'adresse logique) a rétabli la télécommande, **confirmé physiquement par Alex**. Correctif 0.7.3 :
reprise automatique décrite au « Contrat OpenTV », couverte par `tests/test_cec_return.py` (séquence capturée rejouée).
**À valider sur le Pi** : moniteur accepté sous `CAP_NET_ADMIN` dans le bac à sable, YouTube → Retour → six touches
sans intervention (3 fois), YouTube → autre entrée sans émission, Home Samsung sans annonce ni reprise (seul le `POLL` neutre 0.7.4 continue s'il
est armé), aucun réveil.

### Verdict Samsung actuel

- six touches : **PASS** ;
- navigation visible OpenTV : **PASS** ;
- aller-retour Home Samsung → OpenTV : **PASS** ;
- aucune prise d'entrée automatique sous l'ancien contrat sans annonce : **PASS** ; aucune réannonce après l'annonce
  unique de démarrage : **à revalider** ;
- persistance après un nouveau redémarrage du Pi avec l'annonce unique de démarrage, puis après extinction/rallumage
  complet de la TV : **à refaire** avant de classer cette gamme comme plug-and-play sans réserve ;
- retour YouTube natif → OpenTV : touches inertes **reproduites**, `cec --activate` **PASS** (Alex) ; reprise
  automatique 0.7.3 (`opentv-cec-return.service`) : **à valider sur le Pi** ;
- 2026-10-31 : reprise `announced` détectée et transmise, mais touches perdues après quelques secondes/minutes
  (**reproduit**) ; canari POLL neutre 15 s seul : six touches > 5 min (**PASS**, Alex) ; maintien intégré 0.7.4
  (`cec_return_keepalive`) : **à valider sur le Pi** (six touches immédiatement puis après 2, 5 et 10 min, trois
  cycles YouTube ↔ OpenTV, Home, autre source, TV éteinte) ;
- 2026-11 : sans suiveur, `FEATURE_ABORT` du noyau à `GIVE_DEVICE_POWER_STATUS` ; `cec-follower -m -s` complet garde
  les touches ; suiveur exclusif minimal 0.7.6 (`opentv-cec-follower.service`) : **à valider sur le Pi** (gate
  « Suiveur CEC »).

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
   - noter les messages `MENU_STATUS`, `ROUTING_*`, `ACTIVE_SOURCE` éventuels ;
   - ouvrir une application native (YouTube…) puis revenir sur OpenTV : noter la séquence exacte de la TV ; avec
     `opentv-cec-return.service`, exiger d'abord `cec_return_ready` au journal (aucune boucle avant « prêt »), puis soit
     la reprise (`"result":"announced"` **et** six touches visibles), soit l'absence
     d'émission si la marque n'émet pas la séquence (consigner, repli téléphone).
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
journalctl -b -u opentv-cec.service -u opentv-cec-announce.service -u opentv-cec-return.service --no-pager
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
