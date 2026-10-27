# OpenTV Raspberry Pi 4 — prototype (« Pi neuve → HDMI → QR → config → OpenTV → agent »)

Statut : **MVP vérifié hors matériel seulement** (tests unitaires/intégration, rootfs temporaire, Chromium headless
sur macOS). Aucune Raspberry Pi n'a encore exécuté ce code : **rien n'est déclaré plug-and-play** tant que la
checklist terrain ci-dessous n'est pas entièrement cochée sur une vraie Pi 4.

La Pi est une **source HDMI** qui affiche le tableau de bord OpenTV (« Pour toi », état, messages d'agents). Elle ne
remplace pas les applis Samsung (YouTube, Netflix…) qui restent lancées par OpenTV Tizen sur la TV, et elle ne
touche ni à `native/` (Tizen v23) ni au NanoPi (`scripts/nanopi/`). Aucun flux média n'est extrait, relayé ou
reconstruit : le MVP n'affiche que des métadonnées du catalogue accepté (`/var/lib/opentv/catalogue.json`, produit
ailleurs et copié sur la Pi) ; il n'y a pas de lecture vidéo dans ce lot.

## 1. Matériel et câbles

- Raspberry Pi 4 Model B (2 Go minimum, 4 Go conseillé). Pi 5 : prévu plus tard, **refusé** par le préflight.
- Alimentation officielle USB-C 5,1 V 3 A (une alimentation faible provoque des écrans noirs et du Wi-Fi instable).
- Carte microSD 16 Go+ (A1/A2) ou SSD USB 3.
- Câble **micro-HDMI → HDMI** branché sur le port **HDMI0** (le plus proche de l'USB-C) → entrée HDMI de la TV.
  Ce port porte aussi la CEC (`/dev/cec0`).
- Câble **Ethernet** vers la box/le routeur pour le premier démarrage (recommandé, voir §3).
- Boîtier ventilé ou dissipateur (Chromium à 1080p chauffe le SoC).
- Télécommande : celle de la TV par HDMI-CEC (priorité 1, **à valider sur le terrain**) ; repli : un téléphone sur le
  **même Wi-Fi** que la TV (QR affiché sur la TV) ; accessoire : télécommande USB 2,4 GHz ou clavier USB, sans pilote
  ni configuration (leur touche Power n'éteint pas la boîte).

## 2. Raspberry Pi Imager (préalable, sur le Mac)

1. Raspberry Pi Imager → *Raspberry Pi 4* → *Raspberry Pi OS (other)* → **Raspberry Pi OS (Legacy, 64-bit) Lite**
   (= Bookworm). Depuis la sortie de Trixie, l'entrée « Raspberry Pi OS Lite (64-bit) » par défaut n'est **plus**
   Bookworm : le préflight la refuse. Choisir explicitement **« Legacy »**.
2. Réglages (roue dentée) : nom d'hôte (ex. `opentv-salon`), utilisateur + mot de passe à toi, **SSH activé**
   (clé publique de préférence), **pays Wi-Fi = CA** (indispensable : sans pays le Wi-Fi et le hotspot restent bloqués),
   fuseau `America/Toronto`. Le Wi-Fi peut rester vide : OpenTV le configure par QR.
3. Écrire la carte, l'insérer dans la Pi. Rien d'autre n'est téléchargé ni flashé par les scripts OpenTV.

## 3. Premier démarrage : Ethernet recommandé

Brancher HDMI0 + Ethernet, puis l'alimentation. Avec Ethernet, la TV montre directement le QR de configuration.
Sans Ethernet ni Wi-Fi, après `OPENTV_HOTSPOT_GRACE_S` (60 s), OpenTV demande à NetworkManager un **Wi-Fi temporaire**
`OpenTV-XXXX` (WPA2, mot de passe aléatoire affiché uniquement sur la TV en QR « rejoindre le Wi-Fi »), puis le QR de
configuration `http://10.42.0.1:8081/setup?t=…`. Ce chemin hotspot est **implémenté mais non vérifié sur Pi**
(désactivable : `OPENTV_HOTSPOT_ENABLED=0`).

Le hotspot est **borné et fail-closed** :
- durée maximale `OPENTV_HOTSPOT_MAX_S` (défaut 1200 s = 20 min, 120–3600) comptée depuis son premier démarrage,
  conservée à travers les redémarrages du service (`/var/lib/opentv/hotspot.json`, lié au `boot_id`). À l'échéance :
  `ap-stop` (profil arrêté **et supprimé**), plus aucune rediffusion automatique ; la TV demande honnêtement de
  brancher Ethernet ou de redémarrer la boîte. Réarmement : redémarrage de la boîte, `sudo /opt/opentv/bin/opentv-pi
  setup reset`, ou « Réglages → Reconfigurer » à l'écran ;
- l'AP OpenTV est détecté même quand Ethernet/Wi-Fi est prioritaire (`nmcli … CONNECTION=opentv-setup-ap`) : toute
  configuration réussie l'arrête et le supprime, y compris « garder la connexion actuelle » avec Ethernet branché ; un AP
  orphelin (identifiants inconnus après redémarrage du service) est arrêté ;
- **garde root indépendante** `opentv-ap-guard.timer` (toutes les 30 s, `opentv-pi ap-guard`, oneshot sans shell) :
  même si `opentv.service` est arrêté, en échec, en boucle de redémarrage (configuration invalide) ou figé, l'AP est
  arrêté et son profil supprimé dès que le service n'est pas exactement `active`, que la configuration est invalide,
  que le `boot_id` est inconnu, ou que le budget (`hotspot.json`) est absent, d'un autre démarrage, expiré, à durée
  négative ou dépassé de plus de 30 s. Pire cas après la mort du service : ≈ 30 s + 5 s de précision + exécution ;
- `boot_id` illisible ou malformé ⇒ **aucun hotspot automatique** (budget non bornable de façon fiable), ni réarmement ;
  la TV l'explique (« démarrage de la boîte non identifiable… branche un câble Ethernet ») ;
- Wi-Fi choisi au QR : la TV n'attend jamais indéfiniment. Échéance persistée (`wifi-apply.json`, même `boot_id`) de
  210 s ; au-delà, échec honnête (« n'a pas répondu à temps »), job encore en file retiré, **nouveau QR**, résultat
  tardif de l'ancien job ignoré ; le hotspot ne repart que si son budget n'est pas épuisé (jamais réarmé par
  l'échéance). La configuration reste ouverte (même après un redémarrage du service) tant que le Wi-Fi n'est pas
  confirmé.

## 4. Installation (Pi déjà flashée, via SSH)

```sh
# depuis le Mac : copier le dépôt (aucun secret dedans)
rsync -a --exclude .git ./ <user>@opentv-salon.local:opentv-src/
ssh <user>@opentv-salon.local
cd ~/opentv-src/scripts/pi4
./prepare-pi.sh                       # plan seul : modèle, OS, paquets (cage, chromium, wlr-randr, v4l-utils…)
sudo ./prepare-pi.sh --yes            # préflight matériel strict, PUIS apt install, puis install.sh
# ou, si les paquets sont déjà là :
sudo ./install.sh --dry-run           # plan complet + préflight
sudo ./install.sh                     # préflight strict, sauvegardes, manifeste, services
sudo /opt/opentv/bin/opentv-pi health # manifeste + unités + HTTP loopback + réseau
```

`prepare-pi.sh` ne lance **jamais** apt avant un préflight matériel strict (`opentv-pi preflight --stage hardware` :
marqueur `/proc/device-tree/model` Pi 4 Model B, `/etc/rpi-issue`, Bookworm, arm64, KMS `vc4-kms-v3d`, DRM HDMI + vc4,
`cmdline.txt`). Les dépendances absentes (cage, chromium, network-manager…) y sont listées à part (« dépendances absentes
(installées par prepare) ») : ce ne sont pas des échecs à ce stade ; `install.sh` les exige ensuite.

Ce que fait `install.sh` : préflight strict (Pi 4 Model B, Raspberry Pi OS Bookworm **arm64**, `dtoverlay=vc4-kms-v3d`,
DRM HDMI, pilote vc4, NetworkManager actif, `nmcli`, `cage`, `chromium`), copie sous `/opt/opentv` (dont le moteur
« Pour toi » `scripts/nanopi/opentv_foryou.py` + `opentv_bridge.py` **inchangés** et `native/app/qrcode.js`), unités
systemd, comptes système (`opentv`, `opentv-kiosk`, groupe `opentv-agent`), PAM du kiosque, `/etc/opentv/opentv.env`
(sans secret, conservé s'il existe), sauvegarde de tout fichier remplacé, manifeste `/opt/opentv/MANIFEST.sha256`,
**1080p garanti** : `video=HDMI-A-1:1920x1080@60` ajouté à `/boot/firmware/cmdline.txt` (sauvegardé, retiré exactement
par `uninstall.sh` ; un `video=HDMI-A-1:` déjà présent est respecté ; effectif au prochain redémarrage de la boîte).
**Mise à jour** : les fichiers de l'ancien manifeste absents du nouveau sont retirés (sauvegardés ; jamais hors du
périmètre OpenTV), puis `systemctl daemon-reload`, `enable` et **`restart`** des quatre unités (dont `opentv-ap-guard.timer`) (un `enable --now` ne
relancerait pas un service déjà actif). Tout échec **avant l'écriture du manifeste** (copie, retrait, cmdline, env,
manifeste) ⇒ retour automatique à l'état précédent (fichiers remplacés, retirés et `cmdline.txt` restaurés ; nouveaux
fichiers effacés). **Après ce point de non-retour** (`systemd-sysusers`, `systemd-tmpfiles`, `chown`, `systemctl`),
aucun retour automatique fiable n'est possible : l'installation s'arrête avec la commande en échec, l'emplacement des
sauvegardes et la commande de retour arrière (`sudo /opt/opentv/uninstall.sh`) — elle ne prétend jamais avoir annulé.

Services :

| Unité | Rôle |
|---|---|
| `opentv.service` | service Python stdlib (utilisateur `opentv`, `Type=notify`, watchdog 30 s — tick borné : `nmcli` 3 s + `ip` 2 s, groupe de processus tué à l'échéance, découverte CEC en thread —, durci) : page TV loopback `127.0.0.1:8080` (réservée au compte `opentv-kiosk` par l'UID du pair TCP, `/proc/net/tcp`), formulaire LAN `:8081` **seulement pendant la configuration**, Agent Port `/run/opentv/agent.sock` ; **télécommande téléphone** `<adresse LAN actuelle>:8082/r` (jamais `0.0.0.0`) seulement configuré et en ligne, écoute relancée si l'adresse change |
| `opentv-kiosk.service` | Cage (Wayland) sur tty1 **sans `-s`** (aucun changement de console Ctrl+Alt+Fn : un clavier branché n'atteint aucune invite de connexion ; maintenance par SSH) + Chromium Raspberry Pi OS en kiosque, sous la politique gérée dédiée `opentv-kiosk.json` (DevTools, impression, téléchargements, boîtes de fichiers, extensions, navigation privée et toute navigation hors de `http://127.0.0.1`/page de secours bloqués), `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0` (jamais bloqué en « failed » après une série de plantages). Profil Chromium **persistant** dédié (`/var/lib/opentv-kiosk/chromium`, verrous `Singleton*` effacés au lancement ; aucun secret durable : la clé de page change à chaque démarrage du service). Le lanceur teste seulement que le **port TCP** 127.0.0.1:8080 accepte une connexion (20 s), sinon il ouvre la page de secours locale `file://…/fallback.html`, qui interroge `/healthz` et revient au tableau de bord |
| `opentv-net-helper.path` / `.service` | helper root déclenché par un job : écrit le profil NetworkManager (keyfile 0600) et lance `nmcli` en argv fixe, sans shell. Dossier des jobs ouvert une fois sans suivre de lien (`O_DIRECTORY|O_NOFOLLOW`), lectures/suppressions relatives à ce descripteur ; résultats écrits par descripteur (`fchown`/`fchmod`, jamais de `chown`/`chmod` par chemin). **Wi-Fi transactionnel** : profil candidat `opentv-wifi-candidate` (UUID neuf, `autoconnect=false`) activé d'abord ; l'ancien `opentv-wifi` n'est remplacé (rename atomique du keyfile, UUID du candidat) qu'après succès ; échec ou délai ⇒ candidat supprimé, ancien profil remonté (ou hotspot en configuration hors ligne), dans une réserve de budget de 51 s gardée avant l'essai. Tout résidu du dossier des jobs (fichier/dossier caché, nom invalide, lien, fifo) est supprimé sans action (sans suivre de lien) : `DirectoryNotEmpty=` ne peut pas boucler. **Budget réel** d'une activation : 120 s pour tous les jobs (`TimeoutStartSec=180`) ; `nmcli --wait` courts (AP down 10 s, Wi-Fi up 35 s, AP up 20 s) + délai d'exécution = attente + 5 s tronqué au budget restant, groupe de processus tué à l'échéance ; un job qui ne tient plus attend l'activation suivante ; un `wifi-apply` pris plus de 60 s après son dépôt est refusé (`expired`) |
| `opentv-ap-guard.timer` / `.service` | garde root du Wi-Fi temporaire (voir §3), indépendante du service Python ; oneshot, budget 40 s (`TimeoutStartSec=60`), seules commandes : `systemctl is-active opentv.service`, `nmcli`/`ip` en lecture, `nmcli connection down/delete id opentv-setup-ap` |
| `opentv-cec.timer` / `.service` | amorçage HDMI-CEC (20 s après le démarrage puis toutes les 60 s) : oneshot `opentv` (groupes `video`, `input`), sans capacité ni réseau, `DeviceAllow` CEC + entrée seulement, budget 40 s (`TimeoutStartSec=60`). Déclare Playback « OpenTV » et la touche OK seulement : **jamais** de réveil de la TV ni de changement d'entrée. Détails : §« Télécommande » |

Fichiers système dédiés (manifeste, sauvegarde d'un éventuel fichier existant, retirés/restaurés par `uninstall.sh`) :
`/etc/systemd/logind.conf.d/opentv-kiosk.conf` (touches Power, Reboot, Suspend, Hibernate **ignorées** par logind :
la touche Power d'une télécommande USB ou de la TV par CEC — `KEY_POWER` — éteindrait sinon la boîte ; effectif au
prochain redémarrage) et la politique Chromium `opentv-kiosk.json` installée dans `/etc/chromium/policies/managed/` et
`/etc/chromium-browser/policies/managed/` (le chemin lu par le Chromium de Raspberry Pi OS est à confirmer sur la Pi).
Les dossiers créés pour eux sont notés (`/var/lib/opentv-install/created-dirs.list`) et retirés s'ils sont vides ; un
dossier livré par la distribution n'est jamais supprimé. Aucune de ces règles ne touche aux 7 touches de navigation.

Télécommande — trois niveaux, dans cet ordre de priorité :

1. **Télécommande de la TV par HDMI-CEC (priorité 1, non validée sur la TV)** — `opentv-cec.timer` → `opentv-pi
   cec-bootstrap` (oneshot `opentv`, sans capacité ni réseau, budget 40 s). Énumère `/dev/cecN`, ne retient que le
   pilote `vc4` et le premier adaptateur dont l'adresse physique est valide (HDMI branché ; jamais d'adresse en dur).
   Déclare Playback « OpenTV » seulement si l'adaptateur est vierge (configuration étrangère jamais remplacée, la nôtre
   jamais réécrite). **Défaut sûr : rien d'autre n'est émis** — ni `<Image View On>` (réveil), ni `<Active Source>`
   (changement d'entrée), ni question d'alimentation, quel que soit l'état de la TV (allumée, en veille, inconnue,
   réveil, redémarrage de la boîte). OpenTV ne « vole » jamais l'entrée HDMI : l'utilisateur choisit l'entrée OpenTV
   avec la télécommande de la TV. La touche CEC Select arrive en `KEY_OK`, qui n'a ni symbole XKB ni code Chromium
   (perdue dans le kiosque) : si la table du noyau contient encore cette valeur par défaut, elle devient `KEY_ENTER`
   (volatile, réappliquée à chaque passage ; une valeur personnalisée n'est jamais touchée). Exit (souvent « Retour »)
   arrive en `XF86Close` → `Close`, géré par la page. Aucun démon ne lit ni n'injecte de touches. La touche Power de la
   TV (`KEY_POWER` côté boîte) est ignorée par logind (drop-in ci-dessus).
   - Vérifier : `sudo /opt/opentv/bin/opentv-pi cec` (lecture seule) → `configuration: "ours"`,
     `logical_address_claimed: true`, `select_key: "enter"` ; `journalctl -u opentv-cec` → `"result":"ready"`,
     `"announce":{"result":"not_requested"}`.
   - **Annonce manuelle, terrain seulement, avec l'accord de l'utilisateur** (la TV peut passer sur l'entrée
     OpenTV) : `sudo systemctl start opentv-cec-announce.service`, puis
     `journalctl -u opentv-cec-announce.service -n 20 --no-pager`. Cette unité dédiée fournit les groupes et le
     dossier d'exécution nécessaires sans lancer la commande en root. Refusée sans autre émission
     si la TV ne répond pas « allumée » (`tv_not_on`) : jamais de réveil, même forcé. Aucune option ne la rend
     automatique ; `OPENTV_CEC_WAKE_TV` n'existe plus (ignorée si présente). Désactiver la déclaration :
     `OPENTV_CEC_BOOTSTRAP=0`. Retour arrière (fait par `uninstall.sh`) : `sudo /opt/opentv/bin/opentv-pi
     cec-bootstrap --release` (efface seulement « OpenTV », rétablit `KEY_OK`).
   - Limite connue : beaucoup de TV n'envoient les touches qu'à la **source active**. Sans annonce automatique, il
     faudra peut-être choisir l'entrée OpenTV à la main ; répondre à `<Set Stream Path>`/`<Request Active Source>`
     (quand la TV le demande) est une évolution future explicite, non implémentée. Tant que les six touches ne sont pas
     prouvées sur la TV, le téléphone reste le repli.
2. **Téléphone (repli immédiat)** — sans terminal, sans adresse, jeton ni code à copier. À la fin de la configuration
   (Ethernet/Wi-Fi gardé), la page « C'est prêt » du téléphone propose « Utiliser ce téléphone comme télécommande » (un
   toucher, puis « Oui ») ; après un nouveau Wi-Fi choisi depuis le Wi-Fi temporaire « OpenTV-… », la page explique
   que le téléphone doit revenir sur le Wi-Fi habituel (le même que la TV) puis scanner le QR de la TV. L'accueil de
   la TV réserve **une colonne à droite** au QR « Télécommande » (jamais par-dessus les cartes, prouvé à 1920×1080 en
   Chromium réel) ; il reste affiché même après un appairage (« Ajouter un téléphone » : un téléphone perdu se
   remplace sans aucune touche), avec « Même Wi-Fi que la TV ». Aucune adresse n'est affichée en texte sur la TV.
   - **Appairage : QR seulement.** Secret aléatoire de 256 bits en mémoire (jamais écrit, jamais journalisé), qui
     tourne toutes les 5 min et aussitôt après chaque appairage réussi. Ouvrir le lien (GET) n'appaire jamais et ne
     compte aucun essai ; seul le bouton « Oui, utiliser ce téléphone » (POST) appaire. Aucun code court : rien à
     deviner. Erreurs limitées à 5 par IP / 10 min (bruit seulement).
   - **Adresse :** le serveur écoute sur l'adresse LAN actuelle (jamais `0.0.0.0`) et est relancé si elle change. Le QR
     vise `http://<nom d'hôte>.local:8082/r?p=…` quand ce nom se résout par mDNS (avahi/nss-mdns) **exactement** vers
     cette adresse (vérifié en tâche de fond toutes les 60 s ; conflit de nom ⇒ non) : le cookie du téléphone survit
     alors à un changement d'adresse DHCP. Sinon (avahi absent, conflit, `OPENTV_REMOTE_MDNS=0`) : repli sur l'adresse
     LAN liée, seulement dans le QR. `Host` accepté : cette adresse ou ce nom exact, avec le port ; loopback, adresse
     publique, autre IP privée, autre nom ⇒ 421. Pairs : IPv4 privées hors boucle locale.
   - **Session :** jeton 256 bits en cookie `HttpOnly` `SameSite=Lax`, **stable** jusqu'à 30 jours d'inactivité ou la
     révocation (aucune rotation : ni réponse perdue ni refus CSRF ne déconnectent le téléphone). Seule son empreinte
     SHA-256 est stockée (`/var/lib/opentv/remote.json`, 0600, écrit hors des verrous) ; 4 téléphones au plus (le moins
     récemment utilisé est oublié). L'inactivité n'est jugée qu'entre deux heures **synchronisées** (NTP, témoin
     `/run/systemd/timesync/synchronized`) : heure reculée ou pas encore synchronisée au démarrage ⇒ aucun téléphone
     n'est jamais oublié.
   - **Requêtes :** pages en `Referrer-Policy: same-origin` (en-tête et `<meta>`), pour que les vrais navigateurs
     envoient l'`Origin` des formulaires ; `Origin` doit valoir exactement `http://<Host>` ; `Origin: null` est
     **toujours** refusé (403) ; sans `Origin`, `Sec-Fetch-Site`/`Referer` doivent être cohérents ; jeton CSRF par
     téléphone. Pavé : Haut, Bas, Gauche, Droite, OK, Retour, Accueil — rien d'autre (ni volume ni marche/arrêt, ni
     URL, ni texte, ni shell). Fonctionne **sans JavaScript** (vrais formulaires POST, prouvé en Chromium réel JS
     désactivé : confirmation, touches, oubli) ; `remote.js` évite seulement le rechargement. Zoom laissé libre. Astuce
     « ajouter à l'écran d'accueil » sur le pavé. 10 touches/s par téléphone, 20/s par IP, 4 connexions simultanées par
     IP ; file vers la TV de 16 touches périmées après 3 s (jamais rejouées), lue par une longue requête loopback
     bornée (8 s, 2 attentes) ; la clé TV et l'API kiosque ne sont jamais exposées au téléphone.
   - Révoquer : TV → Réglages → « Oublier les téléphones », ou « Oublier ce téléphone » sur le pavé. Désactiver :
     `OPENTV_REMOTE_ENABLED=0`.
   **HTTP en clair sur le LAN : prototype.** Aucun certificat propre à l'appareil n'existe ; ne rien présenter comme
   HTTPS. Le cookie n'est donc pas `Secure` ; l'ajout d'un domaine/certificat par appareil n'exigera que l'origine
   publique et le drapeau `Secure` (l'API, le CSRF et le contrôle d'origine restent identiques).
3. **Télécommande USB 2,4 GHz / clavier (dernier recours, accessoire)** — aucune configuration. `ui/keys.js`
   normalise flèches (`ArrowUp`/`Up`…), OK (`Enter`, Entrée du pavé, `Select`, `Accept`), Retour (`Escape`,
   `Backspace`, `BrowserBack`, `GoBack`, `Close`, `Exit`), Accueil (`BrowserHome`, `Home`, `GoHome` : retour au début),
   avec repli sur les anciens `keyCode`. Touches média : aucune action OpenTV aujourd'hui (pas de lecteur), non
   captées. Les touches de navigation du navigateur et le bouton « précédent » d'une souris-télécommande ne quittent
   jamais `/tv` (ni la page de secours).

Dans tous les cas, l'écran QR de configuration reste piloté par le téléphone de configuration : aucune touche n'y agit.

Récupération : redémarrage du service ⇒ téléphones conservés (empreintes sur disque), page TV resynchronisée sans
rejouer d'anciennes touches ; redémarrage de la boîte ⇒ déclaration Playback et table `KEY_ENTER` réappliquées (aucune
annonce, aucun réveil), téléphones conservés même si l'heure recule avant NTP ; nouvelle adresse DHCP ⇒ écoute relancée,
téléphone appairé par le nom `.local` conservé ; « Reconfigurer » ⇒ télécommande téléphone fermée jusqu'à la fin de la configuration ; `remote.json`
illisible ⇒ aucun téléphone autorisé (rescanner le QR) ; `uninstall.sh` sans `--purge` conserve les téléphones
appairés pour une réinstallation, `--purge` les efface.

Résolution : paramètre noyau `video=HDMI-A-1:1920x1080@60` (installé par `install.sh`) + `wlr-randr` dans Cage.
Le produit reste volontairement en **1080p60** : la TV fait la mise à l'échelle. Le Pi 4 peut sortir un écran 4K60 sur
HDMI0 avec `hdmi_enable_4kp60=1`, mais au prix d'une fréquence GPU, d'une consommation et d'une température plus
élevées. Ce réglage n'améliore pas le décodeur de Chromium.

Vidéo validée sur le Pi réel avec Chromium Raspberry Pi OS 155 lancé par `/usr/bin/chromium` (le lanceur fournit
`--use-angle=gles`) : une mire locale H.264 High@L4.2, 1920×1080, 60 i/s et 8 Mbit/s a ouvert `/dev/video10`, porté
l'horloge H.264 à 500 MHz et perdu 7 images sur 2 254 (0,311 %), avec `hvs_underrun=0`, 38,9 °C et
`get_throttled=0x0`. C'est donc un décodage V4L2 matériel réellement exercé, pas une inférence. Limite produit : H.264
matériel jusqu'à 1080p ; le bloc HEVC 4K60 du BCM2711 n'est pas exposé au Chromium non-ChromeOS, et VP9/AV1 restent
logiciels. Ne pas ajouter de drapeaux VA-API/V4L2 historiques, `--no-sandbox`, `--disable-gpu-sandbox` ou
`--disable-gpu`. Sources : [BCM2711](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/processors/bcm2711.adoc),
[configuration vidéo](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/configuration/audio-display.adoc),
[intégration Chromium RPi](https://github.com/RPi-Distro/chromium/issues/63).

Reprise de l'écran : la clé de la page TV change à chaque démarrage du service. Après un redémarrage, l'API répond
`403 {"error":"tv_key"}` ; la page sonde `/tv` puis se recharge (attente 2 s, 4 s, 8 s), au plus **3 fois par
10 min** (journal en `sessionStorage`), puis affiche « Redémarre la boîte OpenTV si cela dure ». Si la sonde échoue
(service pas encore prêt), l'attente double à chaque échec jusqu'à 60 s (≤ 6 sondes la première minute, prouvé en
Chromium headless). Tout autre refus
(compte non autorisé, service arrêté) n'entraîne aucun rechargement : « Reconnexion… ». L'écran rappelle discrètement
que les applis Samsung (YouTube, Netflix…) restent sur la TV.

## 5. QR de configuration

1. La TV affiche « Bienvenue ! Configurons ta TV » et un QR (valable 10 min, **une seule fois**, 5 essais de
   **soumission** ; ouvrir/rafraîchir le formulaire (GET), même avec un mauvais code, n'use aucun essai).
2. Le téléphone ouvre un **assistant en 3 étapes** (rendu par le serveur, utilisable sans JavaScript ; `setup.js`
   de même origine ajoute seulement « Afficher » le mot de passe et l'anti double envoi) :
   1. « Quel est ton Wi-Fi ? » : grandes cartes des réseaux proches (`nmcli -t -f SSID,SIGNAL,SECURITY device wifi
      list --rescan no`, argv fixe sans shell, seulement avec un jeton valide ; WPA personnel uniquement, SSID
      validés, doublons fusionnés, 8 au plus, cache 20 s, au plus une exécution par 5 s, une à la fois), « Mon Wi-Fi
      n'est pas dans la liste » (nom + réseau masqué) ou « Garder le câble / le Wi-Fi actuel » ;
   2. mot de passe (un seul champ) + « Connecter la TV ». Case facultative, **décochée** : « Laisser Muse apprendre
      de mes goûts » (= avis). « Plus d'options » : nom de la TV (« Salon »), Hermit et Hermes (**aucun accès** par
      défaut ; voir seulement / voir + avis / tout). Une reconfiguration reprend la politique enregistrée ;
   3. « C'est prêt » / « Regarde la TV ».
   Les étapes 1→2 sont des GET (jeton jamais consommé, au plus 4 champs, jamais de mot de passe) ; seul le POST
   final porte `name/net/ssid/psk/hidden/agent_*`. Après une erreur, l'étape fautive est réaffichée sans le mot de
   passe.
3. Le mot de passe Wi-Fi part dans un job 0600 consommé une fois par le helper root, puis seulement dans le profil
   NetworkManager candidat (`opentv-wifi-candidate.nmconnection`, root 0600, `autoconnect=false`), promu en
   `opentv-wifi.nmconnection` seulement après connexion réussie. Jamais en argv, en journal, sur la TV, ni dans un
   résultat. Refusé ou délai dépassé ⇒ candidat supprimé, **ancien `opentv-wifi` intact et remonté** (ou hotspot
   rétabli en configuration hors ligne), nouveau QR.
   **Limite** : le formulaire est servi en **HTTP non chiffré** sur le réseau local (pas de certificat possible pour
   une adresse locale sans avertissement). Par Ethernet, le mot de passe Wi-Fi saisi traverse le LAN en clair : ne
   configurer que depuis un réseau local de confiance (ou le Wi-Fi temporaire OpenTV, WPA2 à clé aléatoire affichée
   sur la TV). Un jeton faux ne consomme aucun essai (seul le bon jeton avec un formulaire invalide compte) ; les
   jetons faux sont limités à 30/min par adresse IP (429).
4. Ensuite le port 8081 se ferme et la TV affiche « Pour toi ». Reconfigurer : tuile « Réglages → Reconfigurer » à
   l'écran, ou `sudo /opt/opentv/bin/opentv-pi setup reset`.

## 6. Agent MCP (Muse, Hermit, Hermes sur la boîte)

Interface bornée et typée, **sans port réseau** : socket Unix `/run/opentv/agent.sock` (0660, groupe `opentv-agent`),
jeton par agent (seul son SHA-256 est stocké), scopes `read` / `feedback` / `display` plafonnés par le choix fait au QR,
débit (10 appels/s) et verrouillage (20 échecs/min ⇒ 60 s) **par UID du client** (`SO_PEERCRED`) : un compte local
hostile ne bloque pas un autre compte ; audit JSONL expurgé (`/var/lib/opentv/audit.jsonl` : outil, statut,
durée, tailles — jamais de jeton, de requête ni de texte).

Politique : le QR enregistre **explicitement** Muse, Hermit et Hermes, y compris « Aucun accès » (scopes vides).
Dès qu'une politique existe, `agent issue` refuse un agent absent ou inconnu (`agent_not_in_policy`) et tout scope
au-delà du plafond (`scope_exceeds_policy`, y compris pour un agent à « Aucun accès ») ; seul `--force`, tapé sur la
boîte par l'administrateur, dépasse ce plafond. Avant la première configuration (aucune politique), l'administrateur
local (root) peut émettre n'importe quel scope valide.

```sh
sudo /opt/opentv/bin/opentv-pi agent issue --id muse --scopes read,feedback,display --owner alex
#  -> /etc/opentv/agents/agent-muse-<uid d'alex>.json (root:alex 0640) ; le jeton n'est jamais affiché
sudo gpasswd -a alex opentv-agent      # puis se reconnecter
sudo /opt/opentv/bin/opentv-pi agent list
sudo /opt/opentv/bin/opentv-pi agent revoke --id muse
```

Sous root, `agent issue` n'écrit **jamais** dans un home (ni `mkdir`, ni `chown`, ni lien suivi) : le fichier client
va dans `/etc/opentv/agents/` (root:root 0755, créé par tmpfiles ; chaque composant du chemin vérifié root et non
inscriptible), nommé `agent-<id>-<uid>.json`, propriétaire root, groupe = groupe **privé** du compte (refus
`owner_group_not_private` si ce groupe est partagé), mode 0640 : lisible par l'agent, non modifiable. Écriture
`O_EXCL|O_NOFOLLOW` relative au dossier, `fchown`/`fchmod` sur le descripteur, rename atomique. `--out` est refusé
sous root (`out_forbidden_as_root`) ; sans privilège, `--out`/`~/.config/opentv/agent-<id>.json` (0600) restent
possibles pour le compte courant seulement. Si le fichier ne peut pas être écrit, le jeton émis est annulé (l'entrée
précédente du registre est restaurée). L'agent lance lui-même `opentv-mcp --config <ce fichier>`.

Configuration MCP stdio de l'agent (même machine) :

```json
{"mcpServers": {"opentv": {"command": "/opt/opentv/bin/opentv-mcp",
                           "args": ["--config", "/etc/opentv/agents/agent-muse-1000.json"]}}}
```

Protocole : JSON-RPC 2.0 ligne par ligne, `initialize` (2025-06-18, 2025-03-26, 2024-11-05), `ping`, `tools/list`
(filtré par scopes, une page), `tools/call`. Outils : `status`, `recommend` (limit ≤ 20, offset ≤ 200), `search`
(titre, requête de type URL refusée), `feedback` (actions explicites : J'aime, Pas pour moi, Déjà vu, À voir plus
tard, Pas ce soir et leurs retraits), `history`, `display_message` (≤ 140 caractères, sans adresse, 1 / 5 s).
Toutes les sorties passent par le nettoyage du moteur « Pour toi » : **aucune URL**. Pas de shell, pas d'URL arbitraire,
pas d'accès brut à la base. Relais sortant (agent distant) : **non implémenté** ; `status` répond
`relay: not_implemented`. Il devra être sortant, authentifié et réutiliser ces mêmes outils.

## 7. Retour arrière

```sh
sudo /opt/opentv/uninstall.sh            # services + garde arrêtés, Wi-Fi temporaire OpenTV arrêté ET supprimé,
                                         # puis fichiers du manifeste retirés, originaux restaurés, tty1 rendu
sudo /opt/opentv/uninstall.sh --purge    # + état OpenTV, /etc/opentv (le Wi-Fi configuré reste)
sudo /opt/opentv/uninstall.sh --purge --forget-wifi   # + profil Wi-Fi opentv-wifi
sudo systemctl disable --now opentv-kiosk.service     # kiosque seul coupé : console tty1 de retour
```
`--dry-run` affiche le plan. `uninstall.sh` retire aussi exactement `video=HDMI-A-1:1920x1080@60` de `cmdline.txt`
(s'il a été ajouté par OpenTV) ; copie d'origine dans `/var/lib/opentv-install/backup-*/boot/firmware/cmdline.txt`.

## 8. Limites non vérifiées (honnêtes)

- Aucun test sur une vraie Pi 4 : Cage + Chromium Raspberry Pi OS sur tty1, PAM/logind, `wlr-randr`, permissions DRM
  du compte `opentv-kiosk`, watchdog systemd et durcissement (`SystemCallFilter`, `DeviceAllow=char-cec`) sont
  **configurés, pas prouvés**.
- Hotspot NetworkManager (`ipv4.method=shared`, dnsmasq) et bascule AP → Wi-Fi sur la même radio : non vérifiés.
  WPA3-SAE seul, réseaux ouverts et portails captifs : non pris en charge.
- `systemd-analyze verify` : absent sur macOS (lint maison seulement ici) ; validé sous Linux par l'agent parent lors
  de la revue. Ce lot modifie `opentv-kiosk.service` (`StartLimitIntervalSec=0`, `RestartSec=5`) : relancer
  `systemd-analyze verify` sur Linux/la Pi avant livraison. `opentv-ap-guard` (timer, accès
  root de `systemctl is-active` et `nmcli` sous `ProtectSystem=strict`, lecture de `/var/lib/opentv/hotspot.json` avec
  `CAP_DAC_READ_SEARCH`) est **configuré, pas prouvé** sur Pi ; idem les délais `nmcli --wait` réels.
- Page de secours `file://…/fallback.html` : elle interroge `http://127.0.0.1:8080/healthz` par `fetch`. Chromium récent
  applique Local Network Access ; une origine `file:` est normalement classée « loopback » (même espace, pas de
  blocage), mais ce n'est **pas vérifié** sur le Chromium de Raspberry Pi OS. Gate terrain : arrêter `opentv.service`,
  relancer le kiosque, vérifier que la page de secours s'affiche puis revient seule sur `/tv` après
  `systemctl start opentv`. En cas d'échec : `systemctl restart opentv-kiosk` (le lanceur réessaie d'abord le port TCP
  pendant 20 s). Cas rare : le kiosque démarre après `opentv.service` (`Type=notify`, port ouvert avant READY).
- Reprise de l'écran, hotspot borné, verrouillage par UID : prouvés en tests (Chromium headless réel, horloge simulée,
  `LOCAL_PEERCRED` sur macOS). Sur le Pi réel, `boot_id`, NetworkManager, hotspot sans Ethernet, restauration Wi-Fi,
  redémarrage et disparition du profil AP ont aussi été exercés ; `SO_PEERCRED` reste couvert par tests, pas sondé
  manuellement.
- CEC : `opentv-cec` déclare seulement Playback « OpenTV » (aucune annonce automatique, aucune commande de
  volume/standby). Le noyau seul produit les touches. Prouvé sur le Pi avant ce lot (avec une annonce manuelle) :
  Haut/Bas/Gauche au niveau noyau. **Non prouvé** : que la TV Samsung route les touches à OpenTV **sans** annonce
  (entrée choisie à la main), Droite, OK (Select → Entrée par la table du noyau), Retour (Samsung « Return » = CEC Exit
  → `Close` ?), la persistance après redémarrage, et que la touche Power de la TV arrive bien en `KEY_POWER` ignoré.
  Si une touche manque : repli téléphone, puis USB.
- Télécommande téléphone : HTTP en clair sur le LAN (prototype, pas de certificat d'appareil). Sur le Wi-Fi réel,
  l'appairage QR par nom `.local`, cinq commandes et la révocation ont été exercés par un client HTTP, sans session
  résiduelle. Non validée avec un vrai iPhone/Android (isolation client d'un routeur = téléphone ne joint pas la boîte).
  Résolution `.local` : native sur iOS et macOS ; sur Android elle dépend de la version (non vérifié). La boîte ne
  peut pas savoir si **le téléphone** résout `.local` : s'il n'y arrive pas, le QR ne s'ouvre pas — contournement
  `OPENTV_REMOTE_MDNS=0` (QR par adresse ; l'appairage est alors à refaire si l'adresse DHCP change). À valider sur le
  terrain. Résolution par `getaddrinfo` dans le service (`nss-mdns` de Raspberry Pi OS) vérifiée sur le Pi sous le
  durcissement systemd.
- Témoin d'heure synchronisée : `systemd-timesyncd` (défaut de Raspberry Pi OS), confirmé sur le Pi après redémarrage ; avec un autre démon NTP, aucune
  expiration par inactivité n'a lieu (fail-safe : les téléphones restent jusqu'à révocation).
- Politique Chromium : chemin réellement lu par le Chromium de Raspberry Pi OS (`/etc/chromium` ou
  `/etc/chromium-browser`), blocage effectif de DevTools (Ctrl+Maj+I, F12), de l'impression (Ctrl+P), des
  téléchargements et de la navigation externe, et affiches `https` toujours visibles (les listes d'URL ne visent que
  la navigation) : **non vérifiés sur la Pi**. Touche Power : drop-in logind effectif après redémarrage, non vérifié.
- Décodage vidéo : H.264 1080p60 matériel validé par `/dev/video10`, horloge H.264, compteurs d'images, thermique et
  absence d'underrun. 4K dans Chromium, HEVC, VP9 et AV1 restent hors cible ; ne jamais les présenter comme accélérés.
- Formulaire de configuration en **HTTP non chiffré** sur le LAN (voir §5) : par Ethernet, le mot de passe Wi-Fi
  traverse le réseau local en clair ; configurer seulement depuis un LAN de confiance ou par le Wi-Fi temporaire.
- Kiosque **sans durcissement systemd** (pas de `NoNewPrivileges`/`ProtectSystem` : le sandbox setuid de Chromium en
  dépend) ; le compte `opentv-kiosk` possède tty1 et le groupe `input` : un clavier branché pilote Chromium en
  kiosque. Atténué par la politique gérée (DevTools, impression, téléchargements, navigation externe bloqués), Cage
  sans changement de console et logind qui ignore Power ; pas de shell (`nologin`). Accès physique = confiance.
- Promotion du candidat Wi-Fi (`nmcli connection load <final> <candidat>` : l'UUID actif passe sur `opentv-wifi`,
  l'ancien UUID disparaît avec son fichier remplacé) : chemin de succès vérifié avec NetworkManager réel sur le Pi.
  Le mauvais mot de passe et sa restauration transactionnelle restent une gate terrain distincte.
- Serveurs HTTP et Agent Port : 16 connexions servies au plus (503 / `busy` au-delà), échéance globale par connexion
  (15 s HTTP, 10 s Agent Port) ; l'écriture d'une réponse vers un client qui ne lit pas reste bornée par le délai du
  socket, pas par l'échéance globale.
- « Pour toi » TV : la sélection est recalculée (catalogue relu + `Bridge.session`) toutes les 120 s ; coût CPU/mémoire de ce rechargement sur Pi 4 **à mesurer sur le terrain** (gros catalogue).
- Journal HTTP : erreurs et non-2xx toujours ; succès des polls (`/api/tv/*`, `/static/*`, `/healthz`) non
  journalisés, un échantillon agrégé `http_ok_sample` toutes les 600 requêtes.
- Catalogue : à copier dans `/var/lib/opentv/catalogue.json` (propriétaire `opentv`, 0600) depuis la chaîne acceptée ;
  sans lui « Pour toi » affiche une sélection vide.

## 9. Checklist terrain (critères distincts)

| Critère | Preuve exigée |
|---|---|
| **Configuré** | `sudo /opt/opentv/bin/opentv-pi preflight --text` OK ; `install.sh` sans erreur ; `opentv-pi health` → `ok:true`, manifeste OK, `opentv.service` et `opentv-net-helper.path` actifs, `NRestarts=0` (`systemctl show -p NRestarts opentv`) |
| **QR / réseau** | QR visible ; formulaire ouvert sur le téléphone ; second envoi du même QR refusé ; Ethernet puis Wi-Fi (`nmcli -t device status`) ; mot de passe absent de `journalctl -u opentv -u opentv-net-helper` et de `/var/lib/opentv` ; port 8081 fermé après configuration (`ss -ltn`) ; hotspot sans Ethernet : réseau `OpenTV-XXXX` visible puis disparu après succès ; hotspot laissé sans configuration : disparu après 20 min et absent de `nmcli connection show` ; hotspot + Ethernet + « garder » : AP supprimé ; **hotspot actif puis `systemctl stop opentv`** : AP disparu en ≤ 60 s (`journalctl -u opentv-ap-guard` : `ap_guard_stop`) ; mauvais mot de passe Wi-Fi : nouveau QR en ≤ 210 s ; `systemctl show -p ExecMainStartTimestamp,ActiveEnterTimestamp opentv-net-helper` < 120 s par activation ; `uninstall.sh` avec hotspot actif : AP absent ensuite |
| **UI visible** | photo/vidéo de la TV : tableau de bord 1920×1080 net (`wlr-randr` ou `/sys/class/drm/card*-HDMI-A-1/modes`), « Pour toi » navigable au clavier, message d'agent affiché ; `kill` de Chromium ⇒ retour automatique du kiosque ; `systemctl restart opentv` ⇒ « Reconnexion… » puis rechargement automatique et données de retour ; mode 1920×1080 effectif après redémarrage de la boîte |
| **CEC** | `sudo /opt/opentv/bin/opentv-pi cec` liste `/dev/cec0` avec une adresse physique ≠ `f.f.f.f` TV allumée ; `journalctl -u opentv-cec` : `"result":"ready"`, `"announce":{"result":"not_requested"}` ; `cec-ctl -d /dev/cec0` : OSD `'OpenTV'`, masque ≠ `0x0000` ; après **redémarrage** : idem sans intervention ; TV en veille au démarrage de la boîte, puis allumée : **la TV ne se rallume pas et ne change pas d'entrée** à cause d'OpenTV (`cec-ctl -d /dev/cec0 -m` : aucune `IMAGE_VIEW_ON`/`ACTIVE_SOURCE` émise par OpenTV) |
| **Télécommande CEC** | entrée OpenTV choisie avec la télécommande Samsung (sans `--force-announce`) : Haut, Bas, Gauche, Droite, OK (ouvre le panneau), Retour (le ferme) ; puis **après `sudo reboot`**, sans intervention, de nouveau les six touches ; touche Power de la TV : la boîte reste allumée ; un ensemble partiel = critère CEC échoué (repli téléphone ; `--force-announce` seulement comme essai terrain consenti) |
| **Télécommande téléphone** | iPhone et Android sur le même Wi-Fi : QR de la colonne de droite → « Oui, utiliser ce téléphone » → les 7 touches agissent en < 0,5 s ; l'URL ouverte est `<nom>.local:8082` (sinon noter le repli) ; ancien QR refusé ; « Oublier les téléphones » (Réglages) ⇒ le pavé demande de rescanner ; `systemctl restart opentv` puis `sudo reboot` (heure avant NTP) ⇒ téléphone toujours appairé ; nouvelle adresse DHCP ⇒ téléphone toujours appairé ; `ss -ltn` : `:8082` lié à l'adresse LAN (jamais `0.0.0.0`), absent pendant « Reconfigurer » ; ajout à l'écran d'accueil iOS fonctionnel ; aucun jeton dans `journalctl -u opentv` |
| **Télécommande USB** | télécommande 2,4 GHz branchée : flèches, OK, Retour, Accueil sur l'écran OpenTV ; Accueil/BrowserBack ne quittent jamais `/tv` ; écran QR : aucune touche n'agit ; **touche Power : la boîte reste allumée** (après redémarrage) ; clavier : F12/Ctrl+Maj+I, Ctrl+P, Ctrl+S, Ctrl+Alt+F2 sans effet |
| **Décodage** | H.264 local 1080p60 : `/dev/video10` ouvert, horloge H.264 ≈ 500 MHz, 7/2 254 images perdues (0,311 %), `hvs_underrun=0`, `get_throttled=0x0`. Refaire 30 min avec une vidéo autorisée avant promotion produit ; 4K/HEVC/VP9/AV1 Chromium hors cible. |
| **Rollback** | `uninstall.sh` ⇒ console tty1 de retour, `/opt/opentv` absent, fichiers d'origine restaurés ; réinstallation propre ; `--purge` sans perte du Wi-Fi |
| **Agent** | `opentv-mcp` depuis le compte de l'agent : `tools/list` filtré par scopes, `recommend` sans URL, scope refusé, audit présent sans secret |

## 10. Développement

```sh
python3 -m unittest discover -s scripts/pi4/tests -t scripts/pi4      # depuis la racine du dépôt (286 tests)
python3.11 -m unittest discover -s scripts/pi4/tests -t scripts/pi4   # version de Bookworm
# bouts en bout réels (Chromium headless, temps réel) et analyse ES5 :
#   OPENTV_TEST_PLAYWRIGHT=<module playwright> OPENTV_TEST_ACORN=<module acorn> OPENTV_TEST_SHOTS=<dossier de captures>
#   (formulaires du téléphone sans JavaScript, Origin: null refusé, écran TV 1920×1080 sans chevauchement du QR)
./scripts/pi4/install.sh --dry-run ; ./scripts/pi4/install.sh --root /tmp/rfs --skip-preflight
```
