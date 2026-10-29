# OpenTV Raspberry Pi 4 — prototype (« Pi neuve → HDMI → QR → config → OpenTV → agent »)

Statut : **MVP vérifié hors matériel seulement** (tests unitaires/intégration, rootfs temporaire, Chromium headless
sur macOS). Aucune Raspberry Pi n'a encore exécuté ce code : **rien n'est déclaré plug-and-play** tant que la
checklist terrain ci-dessous n'est pas entièrement cochée sur une vraie Pi 4.

La Pi est une **source HDMI** qui affiche le tableau de bord OpenTV (« Pour toi », état, messages d'agents). Elle ne
remplace pas les applis Samsung (YouTube, Netflix…) qui restent lancées par OpenTV Tizen sur la TV, et elle ne
touche ni à `native/` (Tizen v23) ni au NanoPi (`scripts/nanopi/`). Aucun flux média n'est extrait, relayé ou
reconstruit : l'accueil n'affiche que des métadonnées du catalogue accepté (`/var/lib/opentv/catalogue.json`, copié
une première fois depuis la chaîne acceptée puis, avec une source approuvée, rafraîchi chaque jour sur la Pi, §6 ter). « Regarder » (§6 bis) réutilise le lecteur du site autorisé, sans jamais lire, extraire ni
relayer l'adresse du média ; il est **livré désactivé** et n'apparaît qu'après approbation de la source ET promotion
terrain explicite (porte de production, §6 bis).

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
systemd, comptes système (`opentv`, `opentv-kiosk`, `opentv-keys`, groupe `opentv-agent` ; l'ancienne appartenance
`opentv-kiosk` ∈ `input` est retirée, Cage reçoit `input` par son unité seulement), PAM du kiosque, `/etc/opentv/opentv.env`
(sans secret, conservé s'il existe), sauvegarde de tout fichier remplacé, manifeste `/opt/opentv/MANIFEST.sha256`,
**1080p garanti** : `video=HDMI-A-1:1920x1080@60` ajouté à `/boot/firmware/cmdline.txt` (sauvegardé, retiré exactement
par `uninstall.sh` ; un `video=HDMI-A-1:` déjà présent est respecté ; effectif au prochain redémarrage de la boîte).
**Démarrage produit silencieux** : OpenTV ajoute seulement les options d'affichage sûres absentes (`quiet`,
`loglevel=3`, `logo.nologo`, `vt.global_cursor_default=0`, `systemd.show_status=false`) par écriture atomique et
synchronisée de `cmdline.txt`; une valeur locale `debug`/`loglevel=` est respectée. Un splash tty1 affiche
« OpenTV / Démarrage... » avant Cage, sans supprimer les journaux ni SSH. `opentv-boot-check` vérifie l'état après
redémarrage.
**Image OpenTV de bout en bout (Plymouth)** : l'arc-en-ciel du firmware est retiré par **un seul ajout en fin de
`/boot/firmware/config.txt`** (`disable_splash=1`, précédé de `[all]` si la dernière section est conditionnelle ;
octets exacts notés dans `/var/lib/opentv-install/config-splash.added` avant une écriture atomique synchronisée ; fin
de ligne CRLF/absente et lignes existantes conservées ; une valeur locale `disable_splash=0` est respectée et signalée
par `opentv-boot-check`). `prepare-pi.sh` installe le paquet `plymouth` (jamais `install.sh`). Si Plymouth est
utilisable (plymouthd, module `script`, crochet initramfs, `update-initramfs`, `lsinitramfs`, `auto_initramfs=1`,
`/boot/firmware/initramfs8`, un noyau v8 complet — `/boot/initrd.img-<version>-v8` et ses modules —, ni `nosplash`
ni `plymouth.enable=0|false|no|off` / `rd.plymouth=…`), `install.sh` livre le thème
`/usr/share/plymouth/themes/opentv` (image fixe 1920×1080 : fond sombre OpenTV v23, marque « OpenTV » centrée, petit
« Démarrage… » ; bande sans libellé à l'arrêt ; aucun message système, aucune animation, aucun réseau), sélectionne
`Theme=opentv` dans `/etc/plymouth/plymouthd.conf` (thème et contenu précédents notés durablement :
`plymouth-theme.added`, `plymouthd.conf.orig`), ajoute `splash plymouth.ignore-serial-consoles` aux jetons notés de
`cmdline.txt` (`console=serial0` et `console=tty1` conservés), puis — seulement après toutes ces vérifications et une
fois le thème complet en place — régénère l'initramfs en **transaction** (`opentv-initramfs.sh`, partagé avec
`uninstall.sh`). Sur Raspberry Pi OS, `update-initramfs` remplace `/boot/initrd.img-<v>` par renommage, mais le crochet
`z50-raspi-firmware` **copie en place** vers `/boot/firmware/initramfs8` (troncature puis écriture : une coupure ou un
arrêt pendant la copie le laisse tronqué). Donc : (1) noyau **exact** — le noyau v8 dont `initrd.img-<v>` est
identique octet pour octet à `initramfs8`, sinon le v8 le plus récent — et `update-initramfs -u -k <v>`, jamais
`-k all` ; (2) **avant** : copie durable (temp → fsync → renommage → fsync du dossier, relue) de `initramfs8`,
`/boot/initrd.img-<v>` et `/var/lib/initramfs-tools/<v>` dans `backup-…/initramfs` avec SHA-256, taille et mode
(`meta`), puis journal `/var/lib/opentv-install/initramfs.txn` ; (3) régénération bornée par **SIGTERM** (900 s,
jamais SIGKILL), puis attente bornée de la fin de tout le groupe de processus (la copie du crochet) — un écrivain qui
survit n'est jamais concurrencé : journal conservé ; (4) vérification : `lsinitramfs` borné (300 s) avec les entrées
exactes du thème, `plymouthd`, module `script` et rendu DRM, **et** `initramfs8` identique à `initrd.img-<v>` ;
(5) tout échec ⇒ thème, `plymouthd.conf`, `config.txt`, `cmdline.txt` et marqueurs restaurés, et les **octets
d'origine** des trois fichiers restaurés (temp du même dossier → fsync → renommage → fsync du dossier) puis relus
(SHA-256) **avant** tout changement de marqueur. Le journal n'est effacé qu'après le manifeste. Un journal resté
(coupure, arrêt brutal) est **rejoué en premier** par le prochain `install.sh` ou `uninstall.sh` (octets d'avant
restaurés et vérifiés ; une sauvegarde altérée ou un journal hors de `/var/lib/opentv-install` est refusé, rien
d'autre n'est modifié), **puis seulement** le noyau exact est choisi sur l'`initramfs8` rétabli (un `initramfs8`
tronqué ne correspond à aucun noyau ; le noyau noté au journal n'est retenu que s'il est validé et identique) et
l'image OpenTV est régénérée pour ce même noyau. Si `opentv-boot-check` signale le thème absent de l'initramfs :
relancer `sudo ./install.sh` (ou `sudo update-initramfs -u -k "$(uname -r)"`, jamais `-k all`). Toute régénération due à OpenTV (thème livré,
`Theme=opentv`, jetons `splash`), **même si `Theme=opentv` était déjà choisi avant l'installation**, est notée
durablement **avant** de lancer `update-initramfs` (`/var/lib/opentv-install/initramfs-plymouth.rebuilt`, fichier
régulier validé ; un lien ou un non-fichier fait refuser installation et désinstallation avant toute modification).
Après un échec, ce marqueur n'est rendu à son état précédent que si la restauration à l'octet a réussi ; sinon il reste
(fail-closed). `uninstall.sh` régénère (même transaction) dès que ce marqueur existe, **après** avoir rétabli le thème
tel qu'avant l'installation (fichiers OpenTV retirés, fichiers d'origine du dossier `opentv` restaurés, dossier créé
par OpenTV retiré), vérifie qu'aucune entrée `plymouth/themes/opentv/*` sans fichier sur le disque ne subsiste et que
`initramfs8` est identique à `initrd.img-<v>`, et n'efface le marqueur qu'ensuite. En cas d'échec, les octets d'avant
la désinstallation (image OpenTV amorçable) sont restaurés et relus, puis la désinstallation **s'arrête** (code 1)
avant l'arrêt des services et tout retrait de fichier : `/opt/opentv/uninstall.sh`, sa bibliothèque et le marqueur
restent pour relancer (même avec `--purge`). Une réinstallation identique ne régénère rien. Sans Plymouth
utilisable, rien de tout cela n'est activé : l'écran texte `opentv-splash` reste le repli (et `splash` n'est pas
ajouté). Avec Plymouth actif, `opentv-splash` n'écrit **rien** sur tty1 (le texte réapparaîtrait au passage) et le
note au journal. Passage au kiosque : `opentv-kiosk.service` remplace `plymouth-quit.service` (`Conflicts=`, comme un
gestionnaire de session graphique ; n'attend jamais `plymouth-quit-wait.service`) et ne porte **aucune** commande
annexe : `TTYReset=no`, `TTYVHangup=no`, `TTYVTDisallocate=no`, ni `ExecStartPre` ni `ExecStartPost` (systemd 252
applique les réglages TTY d'un service à **chaque** commande : un `ExecStartPost` pouvait raccrocher tty1 et tuer Cage
par SIGHUP, en boucle). Le passage vit dans des unités root dédiées, oneshot bornées, **sans aucune option TTY**,
tirées par le kiosque (`Wants=`) : `opentv-plymouth-deactivate.service` (avant Cage, ≤ 12 s : `plymouth deactivate`,
l'image reste affichée, le maître DRM est rendu) et `opentv-plymouth-quit.service` (après le démarrage, ≤ 45 s : lit
`systemctl show --property=MainPID --value opentv-kiosk.service`, attend que ce PID tienne le nœud DRM **d'affichage
vc4** — `/dev/dri/cardN` dont le pilote est `vc4-drm`, jamais un `card*` quelconque comme v3d — puis `plymouth quit
--retain-splash` ; kiosque inactif/en échec ou 25 s dépassées : `plymouth quit` ordinaire, console rendue). Filet :
`opentv-plymouth-failsafe.service`, déclenché par `OnFailure=` (kiosque et unités de passage — jamais
`OnFailure=plymouth-quit.service`, en conflit avec le kiosque) et par `opentv-plymouth-failsafe.timer` 120 s après le
noyau : Plymouth encore actif ⇒ `retain-splash` si le kiosque tient vc4, sinon quit ordinaire. Plymouth ne reste donc
jamais indéfiniment (kiosque masqué, condition non remplie, échec), et `plymouth-quit-wait.service` se termine
toujours. Kiosque désactivé : `plymouth-quit.service` s'exécute normalement et la console revient. Le mode de secours
(`console=tty1`) quitte Plymouth lui-même et reste visible. Ces scripts root utilisent des chemins fixes (variables
`OPENTV_*` de test ignorées en root). L'installation et la désinstallation conservent des marqueurs durables et
refusent de continuer si la partition de démarrage ou `cmdline.txt` ne peut pas être restauré exactement.
**Mise à jour** : les fichiers de l'ancien manifeste absents du nouveau sont retirés (sauvegardés ; jamais hors du
périmètre OpenTV), puis `systemctl daemon-reload`, `enable` et **`restart`** des unités OpenTV (dont
`opentv-ap-guard.timer` et `opentv-splash.service`) (un `enable --now` ne
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
| `opentv-splash.service` | oneshot tty1 borné à 5 s, sans réseau et durci : fond sombre « OpenTV / Démarrage... » avant le kiosque — **repli** seulement : ordonné après `plymouth-start.service`, il n'écrit rien quand Plymouth affiche l'image OpenTV (`splash` + `plymouthd` vivant). Le drop-in `getty@tty1` retire l'invite de connexion seulement tant que le kiosque est activé; rien n'est masqué et SSH reste le chemin de récupération. |
| `opentv-net-helper.path` / `.service` | helper root déclenché par un job : écrit le profil NetworkManager (keyfile 0600) et lance `nmcli` en argv fixe, sans shell. Dossier des jobs ouvert une fois sans suivre de lien (`O_DIRECTORY|O_NOFOLLOW`), lectures/suppressions relatives à ce descripteur ; résultats écrits par descripteur (`fchown`/`fchmod`, jamais de `chown`/`chmod` par chemin). **Wi-Fi transactionnel** : profil candidat `opentv-wifi-candidate` (UUID neuf, `autoconnect=false`) activé d'abord ; l'ancien `opentv-wifi` n'est remplacé (rename atomique du keyfile, UUID du candidat) qu'après succès ; échec ou délai ⇒ candidat supprimé, ancien profil remonté (ou hotspot en configuration hors ligne), dans une réserve de budget de 51 s gardée avant l'essai. Tout résidu du dossier des jobs (fichier/dossier caché, nom invalide, lien, fifo) est supprimé sans action (sans suivre de lien) : `DirectoryNotEmpty=` ne peut pas boucler. **Budget réel** d'une activation : 120 s pour tous les jobs (`TimeoutStartSec=180`) ; `nmcli --wait` courts (AP down 10 s, Wi-Fi up 35 s, AP up 20 s) + délai d'exécution = attente + 5 s tronqué au budget restant, groupe de processus tué à l'échéance ; un job qui ne tient plus attend l'activation suivante ; un `wifi-apply` pris plus de 60 s après son dépôt est refusé (`expired`) |
| `opentv-ap-guard.timer` / `.service` | garde root du Wi-Fi temporaire (voir §3), indépendante du service Python ; oneshot, budget 40 s (`TimeoutStartSec=60`), seules commandes : `systemctl is-active opentv.service`, `nmcli`/`ip` en lecture, `nmcli connection down/delete id opentv-setup-ap` |
| `opentv-player.service` | contrôleur du lecteur « Regarder » (`playerd`, compte `opentv-kiosk`, groupes effectifs `opentv-kiosk video render` — **jamais `input`**) ; Chromium séparé, CDP par tube seulement ; activé par l'installation mais **sans capacité de lecture** tant que la source n'est pas approuvée et la lecture promue sur le terrain (§6 bis) |
| `opentv-player-keys.service` | aide des touches de fermeture (compte `opentv-keys`, seul détenteur de `input` côté lecture, `DeviceAllow=char-input r`, sans réseau `PrivateNetwork=yes`, sans capacité, `ProtectSystem=strict`) : lit evdev seulement pendant une séance abonnée, n'envoie que `back/exit/home/devtools` au contrôleur par `/run/opentv-player-keys/keys.sock` (SO_PEERCRED vérifié dans les deux sens) |
| `opentv-cec.timer` / `.service` | amorçage HDMI-CEC (20 s après le démarrage puis toutes les 60 s) : oneshot `opentv` (groupes `video`, `input`), sans capacité ni réseau, `DeviceAllow` CEC + entrée seulement, budget 40 s (`TimeoutStartSec=60`). Déclare Playback « OpenTV », remappe la touche OK et fait **une seule annonce `<Active Source>` par démarrage** de la boîte (fenêtre de 240 s après le boot, TV allumée seulement) : jamais de réveil de la TV, jamais de réannonce. Détails : §« Télécommande » |

Fichiers système dédiés (manifeste, sauvegarde d'un éventuel fichier existant, retirés/restaurés par `uninstall.sh`) :
`/etc/systemd/logind.conf.d/opentv-kiosk.conf` (touches Power, Reboot, Suspend, Hibernate **ignorées** par logind :
la touche Power d'une télécommande USB ou de la TV par CEC — `KEY_POWER` — éteindrait sinon la boîte ; effectif au
prochain redémarrage) et la politique Chromium `opentv-kiosk.json` installée dans `/etc/chromium/policies/managed/` et
`/etc/chromium-browser/policies/managed/` (le chemin lu par le Chromium de Raspberry Pi OS est à confirmer sur la Pi).
Les dossiers créés pour eux sont notés (`/var/lib/opentv-install/created-dirs.list`) et retirés s'ils sont vides ; un
dossier livré par la distribution n'est jamais supprimé. Aucune de ces règles ne touche aux 7 touches de navigation.

Télécommande — trois niveaux, dans cet ordre de priorité :

1. **Télécommande de la TV par HDMI-CEC (priorité 1, validée sur la Samsung terrain)** — `opentv-cec.timer` → `opentv-pi
   cec-bootstrap` (oneshot `opentv`, sans capacité ni réseau, budget 40 s). Énumère `/dev/cecN`, ne retient que le
   pilote `vc4` et le premier adaptateur dont l'adresse physique est valide (HDMI branché ; jamais d'adresse en dur).
   Déclare Playback « OpenTV » seulement si l'adaptateur est vierge (configuration étrangère jamais remplacée, la nôtre
   jamais réécrite). **Une seule activation par démarrage de la boîte** (`OPENTV_CEC_BOOT_ANNOUNCE=1`, défaut) : sans
   `<Active Source>`, la Samsung terrain ne transmet plus les touches au Pi après un redémarrage. Dans les 240 s qui
   suivent le boot (`/proc/uptime`), HDMI branché et adresse logique obtenue, la minuterie demande l'état
   d'alimentation ; TV déjà allumée ⇒ `<Active Source>` puis `<Menu Status>`, une fois, quelle que soit la source
   active annoncée par la TV — **jamais `<Image View On>` ni aucune commande de réveil/alimentation** (refusées par le
   code avant tout lancement de `cec-ctl`). TV en veille/inconnue ⇒ rien d'autre (jamais de réveil), nouvel essai au passage
   suivant tant que la fenêtre est ouverte. Le témoin `/run/opentv-cec/announced.json` (lié au `boot_id`, tmpfs) n'est
   écrit qu'après succès ; ensuite aucun passage de minuterie ni redémarrage de service n'émet plus rien, donc Samsung
   Home et les applications natives ne sont jamais interrompus. Hors fenêtre, `boot_id`/uptime illisible ou dossier
   d'état non inscriptible ⇒ aucune annonce automatique. La touche CEC Select arrive en `KEY_OK`, qui n'a ni symbole XKB ni code Chromium
   (perdue dans le kiosque) : si la table du noyau contient encore cette valeur par défaut, elle devient `KEY_ENTER`
   (volatile, réappliquée à chaque passage ; une valeur personnalisée n'est jamais touchée). Exit (souvent « Retour »)
   arrive en `XF86Close` → `Close`, géré par la page. Aucun démon ne lit ni n'injecte de touches. La touche Power de la
   TV (`KEY_POWER` côté boîte) est ignorée par logind (drop-in ci-dessus).
   - Vérifier : `sudo /opt/opentv/bin/opentv-pi cec` (lecture seule) → `configuration: "ours"`,
     `logical_address_claimed: true`, `select_key: "enter"`, `announced_this_boot: true` ; `journalctl -b -u
     opentv-cec` → un passage `"announce":{"result":"boot_done",…}`, puis `"already_this_boot"` /
     `"boot_window_passed"` sans aucune émission.
   - **Réactivation explicite — récupération administrateur/terrain** (après un débranchement HDMI, une TV allumée
     après la fenêtre, ou un essai terrain ; la TV, déjà allumée, passe sur l'entrée OpenTV ; même séquence que
     l'annonce de démarrage : `<Active Source>` + `<Menu Status>`, jamais `<Image View On>`) :
     `sudo /opt/opentv/bin/opentv-pi cec --activate` (démarre
     `opentv-cec-announce.service` sous le compte `opentv`, puis relit l'état ; `activation.ok: true` seulement si le
     témoin de ce démarrage (même `boot_id`) a été réécrit par une annonce transmise, sinon `ok: false` et
     `"result":"no_announcement"` — unité sautée sans `/dev/cec*`, amorçage désactivé, HDMI débranché… ; équivalent :
     `sudo systemctl start opentv-cec-announce.service`), journal : `journalctl -u opentv-cec-announce.service -n 20
     --no-pager`. Si un passage de la minuterie tient le verrou plus de 15 s, la réactivation échoue avec
     `"result":"busy"` et la note « réessayer dans une minute » (aucune émission, aucune sortie système). Ignore le témoin de démarrage, refusée sans autre émission si la TV ne répond pas « allumée »
     (`tv_not_on`) : jamais de réveil, même forcé. `OPENTV_CEC_WAKE_TV` n'existe plus (ignorée si présente). Aucune
     annonce automatique : `OPENTV_CEC_BOOT_ANNOUNCE=0`. Désactiver la déclaration : `OPENTV_CEC_BOOTSTRAP=0`. Retour arrière (fait par `uninstall.sh`) : `sudo /opt/opentv/bin/opentv-pi
     cec-bootstrap --release` (efface seulement « OpenTV », rétablit `KEY_OK`).
   - Les six gestes Samsung et l'aller-retour Home Samsung → OpenTV sont prouvés. Le protocole multimarque, les codes
     observés et les gates de reboot/alimentation sont dans [CEC_COMPATIBILITY.md](CEC_COMPATIBILITY.md).
   - **Récupération si la télécommande de la TV est inerte** (TV allumée après la fenêtre de démarrage, HDMI
     rebranché, retour depuis une autre source…) : beaucoup de TV, dont la Samsung terrain, n'envoient les touches
     qu'à la **source active** déclarée par OpenTV. **Choisir l'entrée HDMI OpenTV ne suffit donc pas forcément** :
     OpenTV ne répond pas encore aux demandes de la TV. Dans l'ordre :
     1. **utilisateur** : la télécommande téléphone appairée (QR de la colonne de droite) pilote OpenTV sans CEC ;
     2. **administrateur / terrain** : `sudo /opt/opentv/bin/opentv-pi cec --activate` (TV déjà allumée).
   - **Lacunes connues (évolutions futures, non implémentées)** : (a) aucune action utilisateur « Afficher OpenTV »
     (téléphone appairé, future télécommande USB) ne déclenche encore cette réactivation bornée sans terminal ;
     (b) OpenTV ne répond pas à `<Request Active Source>` / `<Set Stream Path>` quand la TV les émet. Tant que les six
     touches ne sont pas prouvées sur la TV, le téléphone reste le repli.
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
rejouer d'anciennes touches ; redémarrage de la boîte ⇒ déclaration Playback et table `KEY_ENTER` réappliquées, une
seule annonce de source active si la TV est allumée (aucun réveil), téléphones conservés même si l'heure recule avant NTP ; nouvelle adresse DHCP ⇒ écoute relancée,
téléphone appairé par le nom `.local` conservé ; « Reconfigurer » ⇒ télécommande téléphone fermée jusqu'à la fin de la configuration ; `remote.json`
illisible ⇒ aucun téléphone autorisé (rescanner le QR) ; `uninstall.sh` sans `--purge` conserve les téléphones
appairés pour une réinstallation, `--purge` les efface.

Résolution : paramètre noyau `video=HDMI-A-1:1920x1080@60` (installé par `install.sh`) + `wlr-randr` dans Cage.
Le produit reste volontairement en **1080p60** : la TV fait la mise à l'échelle. Le Pi 4 peut sortir un écran 4K60 sur
HDMI0 avec `hdmi_enable_4kp60=1`, mais au prix d'une fréquence GPU, d'une consommation et d'une température plus
élevées. Ce réglage n'améliore pas le décodeur de Chromium.

Vidéo validée pendant 30 min sur le Pi réel avec Chromium Raspberry Pi OS 155 lancé par `/usr/bin/chromium` (le
lanceur fournit `--use-angle=gles`) : une mire locale H.264 High@L4.2, 1920×1080, 60 i/s et 8 Mbit/s a gardé
`/dev/video10` ouvert pendant les 30 sondes, maintenu l'horloge H.264 entre 499 987 808 et 500 000 992 Hz et perdu au
plus 0,372 % des images (rapport final : 299 sur 113 504), avec `hvs_underrun=0`, température maximale 41,3 °C et
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

Navigation de l'accueil (retour terrain) : ↑ remonte d'une rangée en gardant la colonne mémorisée de cette rangée, ↓
descend ; seul ↑ depuis la **première** rangée mène aux Réglages ; dans une fiche ouverte, ↑ et ↓ sont consommés sans
effet (jamais de saut vers Réglages depuis un film ; Retour ferme la fiche sur la même carte) ; Retour remonte aussi
d'une rangée. L'en-tête affiche « Connectés : … » = identités réellement installées (jamais une simple autorisation). OK sur une carte ouvre sa fiche (J'aime, Pas pour moi, Déjà vu, À voir plus tard, Pas ce soir). « Regarder » n'y
figure (en premier) que si la lecture est réellement possible : source approuvée, lecture promue sur le terrain,
contrôleur et aide des touches joignables (§6 bis) ; sinon aucun bouton factice n'est affiché.

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

### 5 bis. Réglages → « Gérer les assistants » (sans refaire la configuration)

Action dédiée, distincte de « Reconfigurer » : **ni Wi-Fi, ni nom, ni hotspot, ni job réseau, ni étape de première
mise en route**. La ligne affiche deux nombres distincts : « Autorisés : N · Connectés : M ».

1. TV : Réglages → « Gérer les assistants » → confirmation (« Non » au focus par défaut ; Retour = un niveau) →
   « Oui, afficher le QR ». La feuille Réglages montre un QR dédié (la colonne « Télécommande » est masquée : un seul
   QR à l'écran) ; OK sur « Annuler », Retour ou Accueil annulent sans rien changer.
2. Téléphone (même Wi-Fi), 3 étapes courtes rendues par le serveur, **sans aucun script** :
   1. « Assistants de la TV » : pour Muse, Hermit et Hermes, deux lignes séparées — **Autorisé** (plafond choisi) et
      **Connecté** (identité réellement installée sur la boîte par l'administrateur, avec « actif il y a … » seulement
      après un vrai appel authentifié à l'Agent Port depuis ce démarrage) ; un choix par assistant, pré-rempli. Un
      assistant installé avec **plus** de droits que son autorisation (émis avec `--force`) est signalé et son choix
      par défaut est « Ne rien changer pour lui » : il n'est jamais réduit ni révoqué sans un choix explicite ;
   2. « Confirmer » : avant → après, avec la conséquence réelle (« Pas installé sur la boîte : cette autorisation ne
      le connecte pas », « Il perd tout de suite les droits retirés », « Il sera déconnecté ») ; « Oui, enregistrer »
      est le seul POST qui écrit ; « Terminer sans rien changer » si rien ne change ;
   3. « C'est enregistré » (chaque assistant marqué « Modifié » ou « Inchangé », état réel relu) ; la TV revient au
      début de l'accueil et affiche « C'est enregistré ! Assistants autorisés : N · connectés : M ». Les pages
      finales (enregistré, annulé, erreurs) portent leurs styles **en ligne** : l'écoute se ferme juste après la
      réponse, avant que le navigateur ne puisse demander `/static/setup.css`. Ces styles sont un sous-ensemble mot
      pour mot de `setup.css` (test), autorisés par leur seule empreinte `'sha256-…'` dans la CSP (aucun
      `'unsafe-inline'`, aucun script, aucune écoute prolongée).
3. Sécurité : écoute ouverte **seulement** pendant ce mode, configuré et en ligne, sur l'adresse LAN actuelle (jamais
   `0.0.0.0`), même port que la configuration mais serveur distinct qui ne sert que `/assistants` et
   `/static/setup.css` (aucun `/setup`, aucun balayage Wi-Fi) ; pairs IPv4 privés, Host = adresse liée ; jeton QR
   distinct du jeton Wi-Fi, usage unique, 10 min, 5 essais de soumission (un GET n'en use aucun ; jeton faux limité à
   30/min par adresse) ; POST : Origin exacte (`null` refusé), jeton CSRF lié au QR (HMAC d'une clé par démarrage),
   `Referrer-Policy: same-origin`, CSP sans script. Rejeu, QR expiré, perte du réseau ou « Reconfigurer » ferment le
   mode ; la TV annonce « expiré » ou « annulé : rien n'a changé ».
4. Effet : **seules les lignes réellement modifiées** sont écrites (même règle pour la page et pour le serveur :
   niveau différent, ou identité installée au-delà du niveau choisi explicitement). La confirmation envoie « keep »
   pour toute ligne « Inchangé » et une empreinte de l'état affiché ; si l'état a changé entre-temps (ex.
   `agent issue` par l'administrateur), l'envoi est refusé (409, rien d'écrit, aucun essai usé) et l'état réel est
   réaffiché. L'écriture reste atomique et complète : agent modifié ⇒ nouveau plafond **et**, dans la même écriture,
   son identité réduite (scopes retirés tout de suite ; « Aucun accès » ⇒ révoquée) ; agent connu non modifié ⇒ sa
   lecture validée actuelle (absente/invalide ⇒ `[]`, déjà affichée « Aucun accès ») et son identité **jamais
   touchée** ; entrées étrangères conservées. Aucun choix modifiant ⇒ aucune écriture (« Rien n'a changé »).
   **Aucun jeton ni fichier client n'est jamais émis** : autoriser un assistant absent ne l'installe pas
   (l'administrateur garde `agent issue`, §6) ; une élévation ne change pas les droits d'une identité existante avant
   sa réémission. Registre illisible ou écriture impossible ⇒ « Rien n'a changé », fichier intact, nouvel essai
   possible. L'Agent Port reste un socket Unix local : ce mode ne l'ouvre ni ne l'expose.
   **QR indisponible** : si l'écoute `/assistants` ne peut pas s'ouvrir (port pris, adresse perdue), le service ferme
   aussitôt le mode (résultat `unavailable`) ; côté TV, une feuille sans adresse de QR affiche « QR indisponible,
   réessaie. » à la place du QR après 2,5 s, puis se ferme et annule le mode à 8 s (« Rien n'a changé »). Une seule
   tentative : aucune relance automatique, l'utilisateur rouvre depuis Réglages.
   **Fichier client après révocation** : le jeton est invalidé immédiatement (son hash quitte le registre), mais le
   fichier `/etc/opentv/agents/agent-<id>-<uid>.json` (root, §6) reste sur la boîte avec un **jeton mort**. Le service
   web tourne sans privilège et n'obtient aucun droit sur ce dossier root ; aucune suppression n'est faite depuis ce
   parcours (et `agent revoke` ne la fait pas non plus aujourd'hui). Nettoyage par l'administrateur :
   `sudo rm /etc/opentv/agents/agent-<id>-<uid>.json`.

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

**Écritures concurrentes et registre illisible.** Toute écriture du registre (`agent issue`/`revoke`, retour arrière,
QR de configuration, « Gérer les assistants ») relit puis réécrit `agents.json` sous un verrou partagé entre le service
et la CLI root : `flock` sur `/var/lib/opentv/agents.json.lock` (fichier vide `opentv:opentv` 0600 ; ouvert
relativement au dossier, sans suivre de lien ni bloquer sur une FIFO, lien unique exigé ; créé par root ⇒ rendu au
compte du service par `fchown` sur le descripteur). Création sans course : `O_EXCL|O_CREAT` d'abord, sinon ouverture
de l'existant sans `O_CREAT` (au plus 10 essais si le fichier disparaît entre les deux ⇒ `registry_busy`, jamais
« chemin dangereux ») ; seul le processus qui vient de le créer le rend au service par `fchown` — un verrou root
préexistant inattendu est refusé, à supprimer par l'administrateur. Attente bornée à 5 s ⇒ `registry_busy`, rien
d'écrit. Cette attente a lieu dans la requête qui écrit (QR de configuration ou « Gérer les assistants »), sous le
verrou du service : pendant une écriture admin concurrente (rare : `agent issue`/`revoke` au même instant), le
rafraîchissement de l'état TV (`/api/tv/state`, toutes les 2 s) peut donc rester suspendu jusqu'à 5 s, puis reprend
seul ; rien n'est perdu, l'écriture refusée se réessaie. Une
sauvegarde du téléphone et un `agent revoke`/`issue` simultanés ne se perdent donc jamais : le second relit l'état du
premier (une révocation ne ressuscite jamais ; un état changé depuis l'affichage du téléphone ⇒ 409). Registre
présent mais illisible, invalide ou non régulier ⇒ **toute** écriture est refusée (`registry_unreadable`, CLI : code
2), fichier intact octet pour octet ; le QR de configuration applique quand même le Wi-Fi et le nom (les assistants
restent refusés, la TV affiche « État illisible »), sans jamais remplacer le fichier. Ce succès partiel (registre
illisible, verrou occupé ou écriture impossible) n'est jamais présenté comme complet : la page finale du téléphone
(« C'est prêt » ou « C'est presque fini »), l'écran TV pendant la connexion et l'annonce TV finale disent « Les
assistants n'ont pas été enregistrés : rien n'a changé pour eux. Réessaie depuis Réglages → Gérer les assistants. »
(état TV : `setup.agents_saved=false`).

Configuration MCP stdio de l'agent (même machine) :

```json
{"mcpServers": {"opentv": {"command": "/opt/opentv/bin/opentv-mcp",
                           "args": ["--config", "/etc/opentv/agents/agent-muse-1000.json"]}}}
```

Protocole : JSON-RPC 2.0 ligne par ligne, `initialize` (2025-06-18, 2025-03-26, 2024-11-05), `ping`, `tools/list`
(filtré par scopes, une page), `tools/call`. Outils : `status`, `recommend` (limit ≤ 20, offset ≤ 200),
`catalogue_list` (tout le catalogue accepté, tri alphabétique, pages ≤ 50, offset ≤ 2000, aucune URL ni champ brut), `search`
(titre, requête de type URL refusée), `feedback` (actions explicites : J'aime, Pas pour moi, Déjà vu, À voir plus
tard, Pas ce soir et leurs retraits), `history`, `display_message` (≤ 140 caractères, sans adresse, 1 / 5 s).
Toutes les sorties passent par le nettoyage du moteur « Pour toi » : **aucune URL**. Pas de shell, pas d'URL arbitraire,
pas d'accès brut à la base. Relais sortant générique (agent distant) : **non implémenté** ; `status` répond
`relay: not_implemented`. Muse dispose de son propre pont sortant (section suivante).

**Pilotage (scope `control`)** — jamais un défaut, jamais accordé par une installation ou une mise à jour : seulement
par un choix explicite du propriétaire (« Gérer les assistants » → « Piloter OpenTV : proposer et lancer des films »,
ou `sudo opentv-pi muse authorize-control --yes` pour Muse). Niveaux emboîtés : Aucun ⊂ Voir ⊂ Avis ⊂ Messages ⊂
Pilotage. Outils sémantiques typés, sans URL, chemin, touche brute de la TV, durée libre ni boucle :

| Outil | Paramètres | Effet autoritaire (jamais « pixels visibles ») | Débit par agent |
|---|---|---|---|
| `present_title` | `id` stable du catalogue accepté, `text` facultatif ≤ 140 sans adresse | action TV `present` mise en file (`queued`) | 1 / 3 s, 20 / 10 min |
| `play_title` | `id` stable | `Playback.start` — mêmes portes que « Regarder » (source approuvée, promotion terrain, contrôleur prêt) ⇒ `loading` + action TV `play` | 1 / 10 s, 6 / 10 min |
| `close_player` | — | lecteur en cours ⇒ `closing` (raison `agent`), sinon `closed:false` | 1 / 2 s |
| `remote_control` | `key` ∈ up, down, left, right, ok, back, home | action TV `key` ; pendant une lecture seules back/home (ferment le lecteur) | 3 / s, 60 / min |

Les actions TV (`opentv_pi/tvcontrol.py`) portent un `seq` croissant ; la page `/tv` les **réclame** une à une
(`POST /api/tv/action {seq, phase:"claim"}`, atomique : jamais appliquée deux fois, même page rechargée) puis rend
compte (`phase:"done"`) : `queued` → `claimed` → `applied` | `failed` (raison bornée), ou `expired` (non réclamée en
20 s), `failed:lost` (réclamée sans résultat en 15 s), `failed:superseded` (nouvelle fiche avant réclamation).
`status` expose, **seulement à un agent qui a le scope `control`**, `control.playback`
(idle/loading/playing/closing/closed/unavailable, `id`, `seq`) et `control.actions` (dernières actions et leur état) :
queued, appliqué, en lecture et fermé restent quatre faits distincts. Sans `control`, `status` répond
`control: {enabled: false}` — ni film en cours, ni état de lecture, ni actions TV (moindre privilège).
`play_title` ne part que d'un lecteur `idle` : tant qu'une fin (`closed`/`unavailable`) n'est pas acquittée par la
page `/tv` (retour sur la fiche précédente en cours), il est refusé `player_returning` (vérifié sous le verrou de
`Playback.start` ; « Regarder » d'Alex garde son contrat) ; pendant ce même retour, `present_title` et
`remote_control` (toutes touches) sont aussi refusés `player_returning`, `close_player` restant permis. Priorité humaine : configuration ou « Gérer les
assistants » ouverts ⇒ `tv_setup_active`/`tv_busy` ; pendant une lecture, aucune fiche ni touche (`player_active`)
sauf fermeture ; une touche **ou une fiche proposée** (`present_title`) mise en file moins de 2 s après une touche de
la télécommande ou du téléphone (ou suivie d'une touche avant d'être appliquée) est refusée (`human_active`) : la
fiche ou la carte qu'Alex vient d'ouvrir reste. `close_player` est toujours permis ; `play_title` (demande
explicite d'Alex, lecture déjà acceptée par les portes) affiche toujours l'écran de chargement honnête. Jamais les
Réglages (`settings_forbidden`) ni « Regarder » par OK (`use_play_title`). La fiche proposée affiche « Proposé par
Muse » et la phrase de l'assistant ; après une lecture, Retour revient sur cette même fiche.

**Identité liée** : le pont Muse s'authentifie comme `muse-device`, mais tout ce que voit Alex porte le nom de
l'agent propriétaire (`agents.canonical_agent` : « Proposé par Muse », « Message de Muse », jamais l'identifiant
brut) et ses budgets de débit (`TOOL_RATES`, appels/s, intervalle des messages et des propositions) sont **partagés**
avec `muse` : deux identités ne doublent jamais le débit. L'audit et le journal gardent l'identité réellement
authentifiée. Dans « Gérer les assistants », l'en-tête TV et les compteurs, `muse-device` est **replié sur Muse**
(`app.assistant_row`) : droits effectifs = scopes enregistrés plafonnés par l'autorisation de Muse (ce que l'Agent
Port accorde), pont **prêt** seulement avec exactement `agents.LINKED_REQUIRED` (`musebridge.MUSE_SCOPES` en
dérive) — Muse est alors « Connecté (piloter OpenTV) » et « Connectés : Muse » ; un appel authentifié réussi le
marque « actif » au nom de Muse. Réduit en dessous (pilotage → lecture/avis/messages), le pont reste installé mais
**en pause** (il refuse de s'enregistrer, exit 78, et reste arrêté) : non compté connecté, affiché « Non : pont Muse
installé mais en pause », et la confirmation l'annonce (« Muse perd tout de suite le pilotage… le pont reste
installé… il faudra réautoriser le pilotage puis réinstaller le pont ») ; « Aucun accès » révoque son identité
(« Le pont Muse sera révoqué tout de suite »). Le pont entre dans l'empreinte de confirmation (installé/retiré
entre-temps ⇒ 409).

**Texte libre** (`display_message`, phrase de `present_title`) : 1–140 caractères, inséré par `textContent`
seulement ; refusés : contrôles et formats Unicode (surcharges/isolats/marques bidi, largeur nulle, BOM, tiret
conditionnel, étiquettes), U+2028/U+2029, usage privé/non attribués, espaces exotiques (seuls espace, insécable,
fine insécable et fine sont admis), plus de 3 diacritiques empilés, écritures de droite à gauche, et toute adresse —
y compris déguisée : pleine chasse, point idéographique/pleine chasse/petite forme, lettres non latines
(squelette NFKC + accents retirés), ou nom d'hôte suivi d'un mot (« evil.com ce soir »), points de substitution
(point médian, katakana, puce, opérateur point, point arménien…), point ASCII entouré d'espaces (« evil . com »),
point épelé ou entre crochets vers une extension courante (« evil dot com », « evil [.] fr », « evil (point) net »),
IPv4 courte/hexadécimale (« 127.1 », « 10.0.1 », « 0x7f.1 », « 127.1:80 »), IPv6 (« [::1] », « fe80::1 »), schémas
faibles (« http:/x », « mailto : », « tel: », « //evil », `\\serveur`, « localhost »), caractères vides de
catégorie admise (U+3164 et autres remplissages hangûl, U+2800, joncteur de graphèmes) et sélecteurs de variation
hors émoji (U+FE0E/U+FE0F seulement après un symbole ou un chiffre de touche). Les titres français (accents, « », ’,
E.T., O.S.S. 117, WALL·E, « Drame · Comédie », 7.8/10, 20:30, émojis ❤️ 1️⃣) restent intacts ; limite connue :
l'écriture inclusive au point médian (« spectateur·rice·s ») est refusée, comme sa forme au point ASCII.

### 6 · Muse. Pont Muse (SDK Linux officiel, transport seulement)

Muse joint OpenTV par le **SDK Linux officiel** (`facebookincubator/muse-gadget-sdk`), utilisé **uniquement** pour
l'appariement Bluetooth et le transport chiffré (Noise). Les commandes par défaut du SDK — `system.run`, `file.read`,
`file.write`, `device.health` — ne sont **jamais** exposées : leurs modules (`executor.py`, `fileops.py`, `service.py`,
`cli.py`) et l'unité `musegadget.service` d'origine ne sont pas livrés, un garde d'import refuse de les charger, et le
pont enregistre sa propre description d'appareil, limitée à cette liste fermée :

| Commande Muse | Outil MCP OpenTV | Bornes |
|---|---|---|
| `opentv.status` | `status` | aucun paramètre (inclut l'état de lecture et des actions TV) |
| `opentv.search` | `search` | `query` 1–80 caractères, jamais d'adresse ; `limit` ≤ 20 ; `offset` ≤ 200 |
| `opentv.recommend` | `recommend` | `limit` ≤ 20 ; `offset` ≤ 200 |
| `opentv.catalogue_list` | `catalogue_list` | pages `limit` ≤ 50, `offset` ≤ 2000 : tout le catalogue accepté, `has_more` |
| `opentv.history` | `history` | `list` (énumération), `limit` ≤ 50, `offset` ≤ 500 |
| `opentv.feedback` | `feedback` | `id` stable, `action` (énumération « Pour toi ») |
| `opentv.display_message` | `display_message` | `text` ≤ 140 sans adresse, `seconds` 5–60, 1 / 5 s |
| `opentv.present_title` | `present_title` | `id` stable, `text` facultatif ≤ 140 sans adresse |
| `opentv.play_title` | `play_title` | `id` stable — seulement quand Alex demande de regarder |
| `opentv.close_player` | `close_player` | aucun paramètre |
| `opentv.remote_control` | `remote_control` | `key` (énumération de 7 touches) |

Muse formule ses propositions lui-même : les descriptions l'invitent à choisir avec `recommend`, `search`, `history`
et `catalogue_list`, à expliquer son choix en une phrase, puis à le montrer avec `present_title` (ou une bannière
`display_message`) plutôt qu'à recopier des pages brutes.

Chaque appel est validé strictement (champs inconnus, types, bornes, adresses ⇒ `invalid_params`, sans aucun appel),
puis délégué par le client MCP (`opentv_pi.mcp`) à l'**Agent Port** avec l'identité locale **dédiée**
`muse-device`, scopes **exactement** `control, display, feedback, read` — jamais l'identité `muse` d'un autre client local (ex. un client MCP de Muse émis
par `agent issue --id muse`), que `pair`/`unpair` n'émettent, ne révoquent ni n'invalident jamais. Son plafond est
l'autorisation de **Muse** sur la TV (`agents.LINKED_POLICY`) : « Aucun accès » au QR ou dans « Gérer les assistants »
la révoque dans la même écriture, et l'Agent Port réapplique ce plafond à chaque appel. `muse-device` est réservée :
`agent issue --id muse-device` est refusé. L'Agent Port reste la barrière (identité révoquée ⇒ `unauthorized`, scope
retiré ⇒ `forbidden_scope`).
Le pont refuse de s'enregistrer auprès de Muse si son identité authentifiée n'a pas **exactement** ces scopes
(`identity_scope_mismatch`, code 78, pas de redémarrage) : un retrait ou une réduction du pilotage sur la TV (ou
`muse revoke-control`) réduit l'identité dans la même écriture, l'Agent Port refuse aussitôt chaque outil retiré et
le pont ne se réenregistre plus. Les résultats repassent par un nettoyage : jetons-adresses retirés (détecteur explicite, au
moins aussi strict que le moteur « Pour toi » : schémas, `www.`, hôte/chemin, hôte:port, IPv4, hôtes nus des TLD
courants, `user:mot@hôte`, courriels, chemins absolus/`~/`/`../`/Windows) sans effacer les titres légitimes
(« Big Data: le film », « M.Butterfly », « Mr.Robot », « File: … », « Paris.Fin ») ; clés retirées par segment exact
(`token`, `secret`, `password`, `auth`, `url`, `uri`, `href`, `link`, `poster`, `path`, `src`, `socket`… —
`author`, `during`, `favourites`, `security` restent) ; tailles bornées, ≤ 64 Kio. Un paramètre facultatif à `null`
vaut « omis » (comme le SDK d'origine) ; une requête qui commence par `-` est un titre littéral (jamais une erreur
`usage`). Le pont ne lit jamais `catalogue.json` ni l'état OpenTV (inaccessibles dans son bac à sable). Journal : chaque
argument (nom de commande, raison de fermeture venus du réseau) est expurgé, neutralisé (contrôle, bidi) et borné
avant la mise en forme ; les traces d'exception sont rendues expurgées et bornées.

Comptes et fichiers : service `opentv-muse.service` (compte `opentv-muse`, jamais root, membre de `opentv-agent` ;
`ProtectSystem=strict`, aucun D-Bus, aucune capacité, `/var/lib/opentv` et les secrets OpenTV inaccessibles) ;
état `/var/lib/opentv-muse` 0700 (`identity.json`, `pairing.json`, `sdk_token` 0600) ; identité Agent Port
`/etc/opentv/agents/agent-muse-device-<uid>.json` (root:opentv-muse 0640). Fenêtre Bluetooth : `opentv-muse-pair.service`
(oneshot borné, seul à recevoir le groupe `bluetooth`, sans section `[Install]`). L'installation livre le SDK
**non apparié et désactivé** ; elle masque `musegadget.service` d'origine (lien `/dev/null` noté, retiré par
`uninstall.sh`) et, si une unité d'origine existe déjà, la laisse intacte et le pont refuse de s'apparier.

Approvisionnement épinglé (`scripts/pi4/vendor/muse/PIN.json`, jamais `@main`) : commit
`1db6a26f4ca56939c4465d8a12e241c1b1b5c8dc`, archive officielle
`https://github.com/facebookincubator/muse-gadget-sdk/archive/1db6a26f4ca56939c4465d8a12e241c1b1b5c8dc.tar.gz`
(SHA-256 `bc02e8dfb54b7fb52cd500c584f6cda732195a9c0b0cb8958bc3a668d72fb2ee`, 6 433 027 octets, vérifiée le
2026-10-28) ; 18 fichiers relus copiés octet pour octet + licence Apache-2.0, chacun épinglé par SHA-256 et revérifié
à chaque démarrage (fichier en trop, modifié ou absent ⇒ refus). Le dossier SDK étant en tête du chemin d'import, son
sommet n'admet que `PIN.json`, `LICENSE`, `musegadget/` et la roue épinglée (un `ssl.py`, un `websockets/`, un `.pth`
ou tout lien symbolique ⇒ refus) ; le bytecode en cache n'est jamais lu (`sys.pycache_prefix`), un `.pyc` planté ne
remplace donc jamais une source vérifiée. Dépendance `websockets` 17.1 : roue pure Python
`websockets-17.1-py3-none-any.whl`, SHA-256 `f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23`
(= `requirements.lock` du même commit), téléchargée par `prepare-pi.sh` seulement, vérifiée avant installation.
Paquets : `bluez python3-cryptography python3-dbus python3-gi`.

```sh
sudo /opt/opentv/bin/opentv-pi muse status        # SDK vérifié, BlueZ, appariement, service, lien, identité ; aucun secret
sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth   # compatibilité BlueZ du SDK (voir plus bas), redémarre bluetooth
                                                        # seulement si nécessaire ; --replace-mtu : valeur locale > 256
# porte humaine : Alex autorise Muse à PILOTER OpenTV (TV : Gérer les assistants > « Piloter OpenTV », ou ici)
sudo /opt/opentv/bin/opentv-pi muse authorize-control        # plan seul (rien écrit) ; --yes : ligne « muse » -> control
sudo /opt/opentv/bin/opentv-pi muse revoke-control           # retire control (politique + identité du pont, même écriture)
sudo /opt/opentv/bin/opentv-pi muse pair          # jeton SDK mgst_… saisi SANS écho (jamais en argument), puis
                                                  # app Muse > Réglages > Appareils > Mode développeur > + MuseGadgetXXXXXX
sudo /opt/opentv/bin/opentv-pi muse pair --replace-token   # nouveau jeton SDK ; --force : nouvel appariement
sudo /opt/opentv/bin/opentv-pi muse pair --replace-identity  # muse-device existante non émise par pair : remplacée
sudo /opt/opentv/bin/opentv-pi muse unpair [--forget-token]  # pont désactivé, appariement effacé, muse-device révoquée
```

`pair` vérifie d'abord (sans rien écrire) : root, SDK épinglé intact, roue présente, aucun service d'origine, compte
`opentv-muse`, groupe `bluetooth`, compatibilité BlueZ **active** (sinon `bluetooth_not_prepared` /
`bluetooth_restart_required` avec la commande à lancer), politique OpenTV autorisant Muse à **piloter**
(`muse_control_not_authorized` sinon, rien écrit), et que `muse-device` est absente ou prouvée à nous (jeton de notre
fichier client) — sinon `muse_device_identity_unexpected`, rien modifié, sauf `--replace-identity` explicite (révoque
`muse-device` seulement). Prouvée à nous avec d'autres scopes (ancien pont en lecture seule) : révoquée puis réémise
avec les scopes exacts, sans nouvel appariement Bluetooth. Il écrit le jeton (0600 opentv-muse, par descripteur),
émet `muse-device` (`control, display, feedback, read`, jamais au-delà du plafond, jamais `--force`), ouvre la fenêtre
(10 min) et n'active `opentv-muse.service` qu'**après** un appariement réussi ; puis il attend l'enregistrement du
lien. Tout échec (fenêtre fermée, unité en échec, Ctrl-C) révoque l'identité tout juste émise et retire son fichier
client. `unpair` ne révoque `muse-device` que si elle est la nôtre (sinon : conservée, signalée). Migration d'une
installation de développement antérieure (identité `muse` émise au compte `opentv-muse`) : `pair`/`unpair` retirent
`agent-muse-<uid opentv-muse>.json` et ne révoquent l'entrée `muse` que si elle porte encore CE jeton ; après la
mise à jour, le pont refuse de démarrer (code 78, identité absente) jusqu'à `sudo opentv-pi muse pair`, qui migre et
réémet `muse-device` sans nouvel appariement Bluetooth (appareil déjà apparié). Portes distinctes : appareil apparié,
service démarré, lien enregistré (`link.state=registered`), commande reçue (`journalctl -u opentv-muse` :
`muse_invoke`), réponse visible dans l'app Muse — seule la dernière prouve le parcours complet. Pour le pilotage,
quatre portes de plus, chacune distincte : action acceptée (`queued`), appliquée par la page `/tv` (`applied`),
lecture `playing` puis `closed` (`opentv-pi muse status` ne lit pas l'Agent Port : utiliser `status` via Muse), et
photo HDMI de la fiche « Proposé par Muse » puis du retour sur la même fiche — porte terrain.

Aide Mac (jamais versionnée avec un jeton, rien n'est stocké) : `scripts/pi4/tools/muse-pair-from-mac.sh
<admin>@<hôte-opentv>` lit `muse status`, propose l'autorisation du pilotage (Alex tape « oui » ; sinon rien écrit),
ouvre la page des jetons SDK, lit le jeton `mgst_…` **sans écho**, vérifie seulement sa forme et le passe une fois par
SSH à `muse pair --token-stdin` (jamais en argument, fichier, historique ni sortie ; variable effacée à chaque sortie).

Compatibilité BlueZ (exigée par l'installateur officiel du même commit, Android **et** iPhone) : `[GATT]
ExchangeMTU = 256` dans `/etc/bluetooth/main.conf` (l'app Android échoue au-delà de 512) et greffon `battery` de
bluetoothd désactivé par le drop-in `/etc/systemd/system/bluetooth.service.d/zz-opentv-muse.conf` (sinon BlueZ demande
à l'iPhone de se lier, ce que le gadget refuse, et l'appariement iOS peut tomber). `install.sh` l'applique dans sa
transaction (sauvegardes, retour arrière automatique en cas d'échec, idempotent) et ne redémarre bluetooth qu'après le
point de non-retour, sur la vraie racine, si quelque chose a changé. Changements notés dans
`/var/lib/opentv-install/muse-bluez.json` et retirés exactement par `uninstall.sh` (avec ou sans `--purge`) : fichier
inchangé ⇒ octets d'origine, modifié depuis ⇒ seules les lignes OpenTV. Jamais : une valeur `ExchangeMTU` locale > 256
remplacée sans `prepare-bluetooth --replace-mtu` (ligne d'origine notée et rendue), un drop-in étranger écrasé, une
commande bluetoothd complexe réécrite — `pair` refuse alors avec la marche à suivre. « Actif » = bluetoothd en cours
avec `--noplugin=battery` et démarré après la dernière modification de `main.conf`.

Désinstallation sans `--purge` : le pont est arrêté et son code retiré, mais l'état reste sur la carte, inactif —
appariement Muse (`pairing.json` : jetons d'appareil), identité d'appareil (`identity.json` : clé privée), jeton SDK
(`/var/lib/opentv-muse`), identité Agent Port `muse-device` (registre `/var/lib/opentv`) et son fichier client
`/etc/opentv/agents/agent-muse-device-<uid>.json`. Pour les effacer : `sudo opentv-pi muse unpair --forget-token`
avant la désinstallation, ou `--purge`. Dans tous les cas, retirer l'appareil dans l'app Muse.

Limites : la communauté Muse n'a pas de vérification fabricant à l'appariement (MITM actif possible pendant la
fenêtre : apparier sur un réseau de confiance). `MemoryDenyWriteExecute` du pont, la politique D-Bus BlueZ du groupe
`bluetooth` et l'appariement réel Android/iPhone avec la compatibilité BlueZ active sont à valider sur la Pi. Muse
continue de voir l'appareil jusqu'à son retrait dans l'app après `unpair`.

## 6 bis. « Regarder » (lecteur du site autorisé, plein écran direct)

Parcours : carte → fiche (« Regarder » en premier) → OK → écran OpenTV « Chargement… » → seul le lecteur dominant du
site, plein écran, avec au plus UN clic central hérité du OK → Retour/Quitter/Accueil (télécommande de la TV par
HDMI-CEC, télécommande USB, téléphone) → même carte, fiche rouverte sur « Regarder ». Aucun lecteur, lecteurs ambigus,
calque par-dessus, délai, plantage : « Lecture indisponible », la page du site n'est jamais montrée.

**Porte de production (fail-closed).** « Regarder » n'apparaît que si TOUT est vrai :
1. source approuvée : `/etc/opentv/source.env` (root:opentv 0640, hors Git, jamais livré ni écrasé) avec
   `OPENTV_SOURCE_APPROVED=1` et une racine de confiance `https://hôte/` valide ;
2. **lecture promue sur le terrain** : témoin `/etc/opentv/playback-promoted` (root:root 0644, JSON
   `{promoted, field_validated, version, code}`), jamais livré, jamais listé dans le manifeste, jamais écrit par
   `install.sh`. Il est lié à la version ET à l'empreinte de la **livraison installée entière** : SHA-256 du manifeste
   `/opt/opentv/MANIFEST.sha256` (root:root), après vérification que chaque fichier listé a toujours l'empreinte
   annoncée et que tous les fichiers dont dépend la lecture y figurent (modules, `tv.js`/`tv.html`/`tv.css`/`keys.js`,
   unités, politique du lecteur, lanceurs, résolveur). Toute livraison ou modification locale d'un fichier livré le
   rend caduc (`stale`) jusqu'à une nouvelle validation ; lien, FIFO, lien dur, propriétaire **non root** (partout :
   service, contrôleur, CLI), mode inscriptible, JSON invalide, manifeste absent/incomplet ⇒ refus ;
3. contrôleur joignable, qui relit lui-même le témoin à chaque ouverture (`not_promoted` sinon) **et toutes les 2 s
   pendant la lecture** (démotion ⇒ séance fermée), et dont l'aide des touches répond avec **au moins une
   télécommande physique capable réellement ouverte** (`devices ≥ 1`, sinon `keys_unavailable` : EACCES ou aucun
   périphérique exposant Retour/Quitter/Accueil ⇒ bouton absent et jamais de révélation). Le téléphone reste une
   sortie complémentaire, jamais la seule ;
4. remise à zéro de démarrage du service terminée (close_all d'une séance orpheline) : aucun close_all tardif ne peut
   fermer la première séance d'un service qui vient de démarrer.

`source.env` seul ne montre donc jamais « Regarder ». L'installation peut activer les deux services : ils restent
inertes. Promotion, **seulement après** la validation HDMI réelle (Regarder → lecture visible → Retour même carte,
décodage et rollback) :

```sh
sudo /opt/opentv/bin/opentv-pi playback status                      # promotion + source, lecture seule
sudo /opt/opentv/bin/opentv-pi playback promote --field-validated   # écrit le témoin (root)
sudo /opt/opentv/bin/opentv-pi playback demote                      # retire le témoin ET ferme la séance en cours
```

`uninstall.sh` retire toujours le témoin (même sans `--purge`) : une réinstallation exige une nouvelle promotion.

Architecture (aucune modification du kiosque /tv, jamais arrêté) :

- `opentv.service` (compte `opentv`) : porte de la source et de la promotion, résolveur d'adresse changeante du NanoPi
  (`opentv_bridge.Resolver`/`RouteMap`, chargés tels quels), automate typé `playback.py`, API `/api/tv/play*`
  (états `idle|loading|playing|closing|closed|unavailable`, jamais d'URL). Retour/Accueil du téléphone pendant une
  lecture : consommés et traités par le service.
- `opentv-player.service` (compte `opentv-kiosk`, même session Wayland que Cage, **sans groupe `input`**) :
  `playerd.py`. Socket Unix `/run/opentv-player/player.sock` accepté seulement pour l'UID du service OpenTV et root
  (SO_PEERCRED). Chromium séparé lancé par `/usr/bin/chromium` (wrapper Raspberry Pi OS), CDP par tube
  (`--remote-debugging-pipe`, aucun port), profil temporaire tmpfs supprimé à chaque fin, politique gérée **du
  lecteur** montée par systemd à la place de celle du kiosque (`/opt/opentv/share/chromium-player-policy`),
  téléchargements refusés. Cibles : jamais la boîte, la boucle locale, le réseau privé (RFC 1918, lien local, CGNAT)
  ni un nom local (`.local`, `.lan`, …) — refusées à l'ouverture puis après résolution DNS, avant tout lancement ;
  chaque requête de document du cadre principal est mise en pause (CDP `Fetch`, armé avant la première navigation) et
  seule l'origine validée est poursuivie avant la révélation, rien après (navigation bloquée ⇒ fin de séance) ; les
  documents des sous-cadres sont poursuivis sans lire leur URL. Unité : `IPAddressDeny=localhost link-local multicast`,
  `SystemCallFilter=~@debug` (`EPERM` : ni ptrace ni lecture mémoire d'un autre processus), `DevicePolicy=closed` +
  `DeviceAllow` DRM/V4L2/média/dma-heap/ALSA (décodage et affichage conservés), `/dev/input`, `/dev/uinput`,
  `/dev/cec*` inaccessibles. Politique du lecteur : `http://`, `localhost`, `127.0.0.1`, `[::1]`, `.local`, `.lan`…
  bloqués, restrictions d'accès au réseau privé/local de Chromium actives.
- `opentv-player-keys.service` (compte `opentv-keys`) : seul composant de lecture qui détient `input`. Il n'ouvre les
  périphériques evdev (lecture seule, sans saisie exclusive) que tant que le contrôleur est abonné, et ne transmet que
  des raisons typées (`back`, `exit`, `home`, `devtools`) et le **nombre réel** de périphériques capables ouverts
  (`devices`, mis à jour au branchement/débranchement ; 0 ⇒ abonnement refusé `no_device`, liaison désarmée). Le
  thread de surveillance est seul propriétaire de ses descripteurs (fermés dans son `finally`, jamais par `stop()`). Les groupes effectifs des unités sont prouvés par test
  (`TestEffectiveGroups`) : `input` seulement pour Cage (`opentv-kiosk.service`, comportement du kiosque inchangé) et
  pour cette aide.
- Cage n'a pas d'API « cacher/montrer » : la fenêtre du lecteur s'empile au-dessus de /tv avec un onglet local
  « Chargement… » (data:, aucun réseau) ; la fiche du site est préparée dans un onglet **d'arrière-plan** jamais peint,
  le lecteur est agrandi sur la page parente (monde isolé, aucune entrée dans l'iframe, aucune lecture de src).
  L'onglet n'est activé que si le lecteur couvre toute la fenêtre, qu'une grille de 25 points ne touche que lui ET
  qu'aucun calque de la page parente ne peut être peint au-dessus : `elementFromPoint` ignorant `pointer-events: none`,
  les styles calculés sont aussi vérifiés (élément ou pseudo-élément positionné au z-index maximal, couche supérieure
  `:modal`/`:popover-open`/plein écran étranger, lecteur ou ancêtre translucide, filtré, découpé ou masqué ; DOM trop
  grand pour être validé ⇒ refus). Le lecteur doit être dans le contexte d'empilement **racine** : l'agrandissement
  neutralise les contextes d'empilement de ses ancêtres, puis tout ancêtre qui en crée encore un (z-index positionné
  ou de flex/grid, position fixed/sticky, opacity, transform/perspective, filter/backdrop-filter, clip/mask, blend,
  isolation, will-change, contain, container-type, content-visibility…) ⇒ refus ; un conteneur de taille nulle à
  z-index maximal dont un descendant peut peindre (débordement, texte, ombre) ⇒ refus. Prouvé dans un vrai Chromium
  avec captures (`e2e_assist.js` : « prêt » ⇒ 100 % des pixels échantillonnés sont le lecteur). Pendant la lecture :
  contrôle rapide (couverture, empilement, transparence, couche supérieure, grille de 25 points) toutes les 0,5 s,
  balayage complet du DOM seulement après une mutation (au plus toutes les 1 s) et au moins toutes les 4 s.
- Fenêtres surgissantes : auto-attachement CDP avec `waitForDebuggerOnStart` — toute nouvelle page est mise en pause
  avant son premier script ou sa navigation, jamais relancée ; l'onglet attendu est réactivé (« Chargement… » avant la
  révélation, le lecteur après) **avant** la fermeture, la fiche non validée n'est donc jamais sélectionnée ; fermeture
  impossible ⇒ fin de séance. Mesuré sur Chromium réel (headless, vrai tube CDP, clic unique réel) : la surgissante
  en pause ne demande même pas sa page et n'exécute aucun script ; la fiche n'est chargée qu'une fois.
  **Limite restante du navigateur** : Chromium active lui-même un onglet demandé par la page à sa création ; pendant
  l'aller-retour CDP un onglet **vide** peut être peint quelques millisecondes. Aucune API CDP n'empêche cette
  création : gate terrain (mesure vidéo sur la TV). Autre constat réel : l'accusé du relâchement de la souris peut ne
  jamais revenir quand ce clic ouvre une surgissante ; il est attendu au plus 2 s, le clic compte comme livré et n'est
  jamais renvoyé.
- DevTools : `DeveloperToolsAvailability=1` dans la politique du lecteur, car la valeur 2 coupe aussi le tube CDP
  (mesuré sur le Pi). C'est sûr parce que : aucun port de débogage n'existe (seul le contrôleur détient le tube) ;
  `devtools://`, `chrome://`, `view-source:` et `file://` sont bloqués ; toute cible DevTools ou page inconnue est
  fermée dès son apparition (événements CDP + balayage toutes les 0,5 s) et la séance s'arrête si elle persiste ;
  F12, Ctrl+Maj+I/J/C et Ctrl+U, signalés par l'aide des touches, **ferment la séance** (retour OpenTV, même carte) —
  l'aide ne retient aucune touche, donc la fermeture est la seule garantie ; sur la page parente, un écouteur du monde
  isolé empêche aussi ces raccourcis tant qu'elle a le focus. Les télécommandes CEC/HID n'ont ni F12 ni Ctrl.
- Retour : fermeture = SIGKILL du groupe du lecteur, profil supprimé, puis `closed`. La fermeture n'attend jamais un
  clic en cours (un navigateur figé pendant le clic ne la retarde pas : < 2 s) ; création des ressources (abonnement
  aux touches, profil, Chromium, CDP) et démontage sont mutuellement exclusifs : une fermeture concurrente d'un
  lancement ne laisse ni navigateur, ni profil, ni abonnement orphelin. Dès que le lecteur est révélé, la
  page /tv rouvre la fiche sur la même carte **sous** la fenêtre du lecteur (jamais un « Chargement… » périmé quand il
  disparaît) ; après le retour, Retour/Accueil encore tenus (répétitions CEC) sont ignorés au moins 600 ms puis tant
  que les répétitions continuent. /tv se repeint (deux images) et acquitte avant le retour à `idle`.

Source (hors Git, jamais livrée par l'installation, jamais affichée) :

```sh
sudo install -m 0640 -o root -g opentv /dev/stdin /etc/opentv/source.env <<'EOF2'
OPENTV_SOURCE_APPROVED=1
OPENTV_SOURCE_TRUST_ANCHOR=https://<racine de confiance stable>/
EOF2
sudo systemctl restart opentv.service
```

Sans ce fichier, avec un fichier lisible par tous, sans approbation explicite ou sans racine de confiance : « Regarder »
n'apparaît pas. `OPENTV_SOURCE_KIND=official-stream` est réservé à une API/un flux HLS-DASH documenté fourni par
l'ayant droit ; aucun n'est configuré, ce mode reste refusé (`direct_mode_unconfigured`) et aucun flux n'est jamais
découvert, extrait ni relayé depuis le lecteur du site. Plafond de lecture : `OPENTV_PLAYER_MAX_S` (4 h par défaut).

Retour arrière : `sudo /opt/opentv/bin/opentv-pi playback demote` (séance en cours fermée, bouton retiré en ≤ 10 s) ;
complet :
`sudo systemctl disable --now opentv-player.service opentv-player-keys.service && sudo rm /etc/opentv/source.env &&
sudo systemctl restart opentv.service` (ou `uninstall.sh`, qui arrête aussi le contrôleur et retire la promotion).

## 6 ter. Catalogue : rafraîchissement quotidien

`opentv-catalogue-refresh.timer` (activé par `install.sh`) lance une fois par jour (04 h 30 + délai aléatoire ≤ 45 min ;
`Persistent=true` : un passage manqué, boîte éteinte, est rattrapé au démarrage suivant) le oneshot
`opentv-catalogue-refresh.service`, **sauté sans `/etc/opentv/source.env`** (aucune requête). Avec un `source.env`
présent mais non approuvé : passage `skipped`, aucune requête.

- **Compte et bac à sable** : `opentv` (sans groupe supplémentaire), aucune capacité, `NoNewPrivileges`,
  `ProtectSystem=strict` + écriture dans `/var/lib/opentv` seulement (`ReadWritePaths=-…` : dossier absent ⇒ refus
  propre `unsafe_dir`, jamais d'échec de montage) ; inaccessibles : état appareil/configuration/point d'accès/Wi-Fi
  (`device.json`, `setup-requested`, `hotspot.json`, `hotspot-rearm`, `wifi-apply.json`), jobs réseau, « Pour toi »,
  registre/verrou/audit des agents, téléphones appairés, profil du kiosque, journal d'installation, fichiers clients,
  profils Wi-Fi et sockets locaux `/run/opentv*` (Agent Port, lecteur, touches, CEC). Limite systemd : un de ces noms
  absent au démarrage ne peut pas être masqué (le passage partage le compte `opentv` du service principal ; défense en
  profondeur seulement). `PrivateDevices`, `@system-service`, réseau sortant
  seulement (`IPAddressDeny=localhost link-local multicast` ; toute adresse résolue non publique — RFC 1918, CGNAT,
  la boîte elle-même — est en plus refusée avant connexion). Priorité basse (`Nice=10`, E/S `idle`, `CPUQuota=50%`,
  `MemoryMax=256M`) : la lecture et l'accueil ne sont pas gênés ; **aucun service n'est redémarré** : le service
  recalcule « Pour toi » dès que la signature du catalogue (inode, taille, mtime_ns) change, et la TV redemande
  `/api/tv/foryou` toutes les 60 s ⇒ un remplacement réussi est visible en ≤ ~62 s (60 s + un poll de 2 s).
- **Moteur réutilisé** : `SourceConfig`/`Source` (même porte que « Regarder »), résolveur d'adresse changeante, analyse
  de la page de liste et validation `clean_item`/`load_catalogue_meta` de `opentv_bridge.py`, inchangé. Son
  `refresh_catalogue` n'est pas appelé (il écrit directement sa cible, accepte toute page non vide et perdrait les
  champs TMDb bruts, `attribution` et les affiches TMDb).
- **Verrou** `catalogue.json.lock` non bloquant : un passage concurrent sort aussitôt (`busy`, code 75, considéré comme
  succès par l'unité), sans requête ni écriture.
- **Candidat ensemencé** par le catalogue actuel : par `site_id` stable (sinon même chemin du même film), description,
  affiche, IMDb, TMDb et tout autre champ d'enrichissement sont conservés ; seuls chemin/identifiant de l'époque, hôte
  et titre/année/genre publiés viennent du site. Un nouveau film arrive sans note : le passage des notes TMDb (§6
  quater), déclenché juste après chaque rafraîchissement réussi, l'ajoute.
- **Validation avant remplacement** (candidat `.catalogue.json.<hasard>.candidate` dans le même dossier, relu depuis le
  disque) : JSON ≤ 8 Mio, `count == len(items)`, chaque élément accepté par le moteur avec titre non vide et
  `site_id`, `site_id`/chemins/identifiants de lien uniques, au moins `OPENTV_CATALOGUE_MIN_ITEMS` (20) et au plus
  2000 éléments, relecture par le moteur sans rien écarter. **Troncature suspecte refusée** : moins de
  (100 − `OPENTV_CATALOGUE_MAX_DROP_PCT`) % (75 %) du nombre actuel, ou moins de 75 % des `site_id` actuels conservés.
  Le catalogue actuel modifié pendant la passe (copie manuelle) ⇒ refus, la copie la plus récente reste.
- **Écriture** : instantané `catalogue.json.prev`, catalogue actuel revérifié juste avant `os.replace` atomique puis
  fsync du dossier ; candidats et `.tmp` orphelins de ce module (coupure) retirés sous le verrou. **Tout échec** (réseau,
  source, page vide, candidat invalide, baisse, écriture) laisse `catalogue.json` intact octet pour octet et retire le
  candidat ; liste identique ⇒ `unchanged`, rien n'est réécrit.
- **Journal/sorties** : codes, nombres, durées, SHA-256 — jamais d'hôte, de préfixe, de jeton, d'URL ni de titre.

```sh
sudo /opt/opentv/bin/opentv-pi catalogue status     # nombre, taille, SHA-256, date ; instantané ; dernier passage
sudo /opt/opentv/bin/opentv-pi catalogue refresh    # un passage MAINTENANT, via l'unité (jamais exécuté en root)
systemctl list-timers opentv-catalogue-refresh.timer ; journalctl -u opentv-catalogue-refresh -n 20 --no-pager
sudo /opt/opentv/bin/opentv-pi catalogue rollback   # catalogue précédent validé + rafraîchissement SUSPENDU
sudo /opt/opentv/bin/opentv-pi catalogue resume     # reprise explicite : la minuterie peut de nouveau mettre à jour
```

**Retour arrière persistant.** `rollback` pose d'abord le témoin `/var/lib/opentv/catalogue.json.hold` (compte
`opentv`, 0600), puis restaure `catalogue.json.prev` après les mêmes validations (sauf baisse/recouvrement) ; un
rollback refusé (instantané absent ou invalide, écriture impossible) retire le témoin qu'il venait de poser et laisse
le catalogue intact. Tant que le témoin existe, chaque passage — minuterie ou `catalogue refresh` — sort `held` (succès
de l'unité) **avant toute requête et sans charger le moteur**, catalogue intact octet pour octet. Un témoin anormal
(lien, FIFO, dossier, lien dur, autre propriétaire, > 4 Kio, contenu invalide) est traité comme une suspension :
`hold_unsafe`, unité en échec, aucune requête. `catalogue status` affiche `hold` (`held`, `safe`). `resume` retire le
nom sous le verrou (jamais suivi : un lien est retiré, pas sa cible) ; un dossier à la place du témoin est refusé
(`hold_unsafe`) et demande un nettoyage manuel. Sous root, `rollback`/`resume` ne s'exécutent jamais en root : l'action
fixe est déléguée au compte du service (`/usr/sbin/runuser -u opentv -- /usr/bin/python3 -I /opt/opentv/bin/opentv-pi
--env … catalogue rollback|resume`, sans shell, environnement minimal, 120 s au plus ; compte root ou inconnu refusé) et
seule sa ligne de résultat filtrée (codes, nombres) est rapportée. `install.sh` ne crée ni ne retire jamais le témoin
(une mise à jour garde la suspension) ; `uninstall.sh` le garde aussi, sauf `--purge` (état effacé).

**Accueil.** Une réponse « Pour toi » identique n'est jamais redessinée. Un nouveau catalogue arrive à l'accueil en
≤ ~62 s ; pendant une fiche, une feuille ou une lecture (chargement, lecture, fermeture), il est gardé en attente et
appliqué à la fermeture : le focus et le retour de « Regarder » suivent l'identifiant stable du film (pas sa
rangée/colonne) ; film retiré ⇒ même rangée, colonne bornée.

Un changement légitime de plus de 25 % est refusé chaque jour (unité en échec, `count_drop`/`low_overlap`) : relire la
source, puis installer un catalogue accepté par la procédure manuelle (§8) ou ajuster
`OPENTV_CATALOGUE_MAX_DROP_PCT` dans `/etc/opentv/opentv.env`. Désactiver : `sudo systemctl disable --now
opentv-catalogue-refresh.timer`.

## 6 quater. Notes TMDb après chaque rafraîchissement

`opentv-catalogue-refresh.service` déclenche par `OnSuccess=` (passage `updated`, `unchanged` ou `held`) le oneshot
**séparé** `opentv-catalogue-ratings.service` (sans section `[Install]` : jamais activé ni lancé à l'installation).
Il réutilise tel quel le moteur vérifié du NanoPi `opentv_tmdb.py` (livré inchangé dans
`/opt/opentv/lib/foryou/`, empreinte dans le manifeste) : `/find` par identifiant IMDb connu, sinon titre exact ou
normalisé + année compatible, sinon `ambiguous`/`unmatched` — jamais de choix parmi plusieurs candidats. Une note n'est
écrite que si TMDb a au moins un vote : **0 vote ⇒ `tmdb_rating: null` (« indisponible »), jamais 0** ; aucune note
inventée, IMDb et TMDb restent deux fournisseurs distincts (`imdb_*` jamais touchés).

- **Panne TMDb ≠ panne du catalogue.** Le rafraîchissement a déjà remplacé et validé le catalogue ; une panne TMDb,
  un jeton refusé ou un délai dépassé laisse ce catalogue **intact octet pour octet** (aucune annulation du
  rafraîchissement), l'unité des notes sort en échec et l'échec est rapporté : `provider_unavailable` (disjoncteur après
  5 erreurs consécutives ou échéance globale de 240 s), `auth_failed`, `partial` (éléments notés écrits, les autres
  repris au passage suivant). `catalogue status` → `ratings` (`credential`, `coverage` : `items`, `rated`, `matched`,
  `ambiguous`, `unmatched`, `pending` = jamais traités, `last` : dernier passage) ; `opentv-pi health` → `ratings` +
  avertissement `ratings_enrichment_failed` (l'état « ok » de l'appareil n'en dépend pas : les notes sont un
  enrichissement).
- **Écriture** : verrou commun `catalogue.json.lock` (exclut rafraîchissement/rollback/resume ; `busy`, code 75),
  catalogue suspendu (`.hold`) ⇒ `held` sans lire le jeton ni rien demander. Copie en mémoire où seuls `tmdb_*` et
  `attribution.tmdb` peuvent changer, puis contrôles : catalogue sans champs TMDb identique, mêmes nombre et
  identifiants (`site_id`, `source_id`, chemin) dans le même ordre, aucun champ TMDb invalide **introduit** (un
  élément déjà invalide resté identique après une erreur du fournisseur est conservé et compté `invalid_kept`),
  **perte de notes bornée**
  (au plus max(2, 10 %) des notes existantes par passage, sinon `rating_drop`, rien écrit), relecture du candidat par
  `load_catalogue_meta` (même nombre d'éléments servis). Candidat même dossier relu, catalogue revérifié inchangé
  (sinon `catalogue_changed`, la copie la plus récente gagne), `os.replace` atomique + fsync du dossier. Rien n'a
  changé ⇒ `unchanged`, aucun octet réécrit. `catalogue.json.prev` (retour arrière du rafraîchissement) n'est jamais
  touché ; horodatages des vérifications hors catalogue (`catalogue.json.tmdb-state.json`), statut
  `catalogue-ratings.json` (0600 `opentv`). L'accueil voit les notes comme tout remplacement : ≤ ~62 s.
- **Rythme** : seuls les nouveaux éléments (ou dont titre/année/IMDb a changé) sont cherchés ; une note trouvée est
  relue par son identifiant TMDb après `OPENTV_TMDB_RATING_MAX_AGE_DAYS` (7) jours, un titre non trouvé/ambigu est
  recherché de nouveau après `OPENTV_TMDB_RECHECK_DAYS` (30) ; au plus `OPENTV_TMDB_MAX_REQUESTS` (300) requêtes.
- **Reprise bornée** (`opentv-catalogue-ratings-retry.timer`, toutes les heures, activée à l'installation) : un
  passage n'est refait que si le **dernier** a échoué pour une cause transitoire (`provider_unavailable`, `partial`,
  `provider_error`, `internal_error`, `catalogue_changed`, `write_failed`, `busy`), au plus 5 fois par épisode, après
  30 min, 1 h, 2 h, 4 h puis 8 h depuis la fin du passage précédent ; ensuite `retry_exhausted` (avertissement
  `ratings_retry_exhausted`) jusqu'au passage quotidien, qui remet le compteur à zéro. Succès, jeton absent
  (`skipped`), suspension (`held`) ou refus non transitoire (`auth_failed`, `credential_*`, `rating_drop`,
  `invalid_candidate`…) : aucune reprise, aucune requête, statut intact. Une seule passe à la fois (verrou commun) ;
  le rafraîchissement et le passage quotidien sont ordonnés **après** une reprise en cours (ils l'attendent au lieu
  de trouver le verrou pris) ; une reprise lancée pendant eux sort `busy` sans rien noter. Toute exception de
  transport (lecture tronquée, connexion coupée, TLS) devient une erreur d'élément : élément gardé tel quel,
  disjoncteur, jamais de trace. `catalogue status` → `ratings.retry` (`attempts`, `max`, `state`, `not_before`).
- **Bac à sable** : celui du rafraîchissement (compte `opentv`, écriture `/var/lib/opentv` seulement, même état
  masqué), plus `source.env` et le cache du résolveur inaccessibles ; HTTPS sortant vers `api.themoviedb.org`
  seulement (nom exact et adresse publique vérifiés avant connexion, aucune redirection suivie).

**Jeton (secret).** `/etc/opentv/tmdb/token`, **root:opentv 0640**, dans le dossier `/etc/opentv/tmdb`
**root:opentv 0750** ; une ligne `TMDB_READ_TOKEN=<jeton v4>` (préféré : en-tête `Authorization`) ou
`TMDB_API_KEY=<clé v3>` ; modèle sans secret : `/opt/opentv/tmdb-token.example`. Chemin fixe (non configurable).
Jamais dans Git, le manifeste, un `EnvironmentFile=`, argv, un journal ni une sortie.

- **Masque par dossier.** Le dossier est créé par tmpfiles (`/etc/tmpfiles.d/opentv.conf`) à l'installation, avant
  le (re)démarrage des unités, puis à chaque démarrage avant toute unité ; toutes les unités autres que les deux des
  notes portent `InaccessiblePaths=-/etc/opentv/tmdb` (le kiosque, sans espace de montage, n'est pas du groupe
  `opentv` : le mode 0750 le lui ferme). Masquer le dossier et non le fichier : un remplacement atomique du jeton ne
  peut pas défaire le masque. Le dossier n'est jamais recréé ni renommé pendant que les services tournent.
- **Contrôles fail-closed** (lecture par le service, `catalogue status`, dépôt) : groupe `opentv` privé (aucun autre
  membre, aucun autre compte de ce groupe principal), dossier vrai dossier root:opentv sans écriture groupe ni accès
  « autres », fichier régulier à un seul lien, root:opentv, 0640 au plus avec lecture groupe ; parent root non
  inscriptible. Sinon `credential_unsafe` (raison dans `credential_why`) et aucune requête. Absent ⇒ `skipped`.
- **Dépôt** (root, sans écho, format validé) : refusé si le groupe n'est pas privé, ou si une unité du compte
  `opentv` en cours d'exécution (`opentv`, `opentv-cec`, `opentv-cec-announce`, `opentv-catalogue-refresh`) n'a pas
  le masque dans son espace de montage (`/proc/<pid>/mountinfo`) : `credential_exposed` + unités à redémarrer, rien
  écrit. Écriture atomique dans le dossier vérifié. `opentv-pi health` (root) avertit `ratings_credential_exposed`
  dans le même cas. `install.sh` ne crée, ne lit ni ne retire jamais le jeton ; `uninstall.sh` le garde (dossier
  compris), sauf `--purge`.

```sh
sudo /opt/opentv/bin/opentv-pi catalogue ratings-credential            # saisie masquée sur un terminal
# depuis le Mac (trousseau), sans afficher le jeton :
security find-generic-password -s com.paz.opentv.tmdb.read-access-token-v4 -a paz_alex -w |
  ssh <pi> sudo /opt/opentv/bin/opentv-pi catalogue ratings-credential
sudo /opt/opentv/bin/opentv-pi catalogue ratings    # un passage MAINTENANT, via l'unité (jamais exécuté en root)
sudo /opt/opentv/bin/opentv-pi catalogue status     # … "ratings": {"credential", "coverage", "last", "retry"}
journalctl -u opentv-catalogue-ratings -u opentv-catalogue-ratings-retry -n 20 --no-pager
```

`catalogue ratings` sous root : `busy` (code 75) si un autre passage tenait le verrou (la reprise horaire s'en
charge), `timeout` si `systemctl start` dépasse son délai (l'unité peut encore tourner : `systemctl status`), sinon
le résultat du passage relu dans son statut.

**Désactiver** sans désinstaller : `sudo rm /etc/opentv/tmdb/token` (passages suivants `skipped`, reprise sans
objet ; garder le dossier). **Revenir sur des notes** : il n'existe pas de retour arrière propre aux notes — ce
passage n'écrit jamais `catalogue.json.prev`. `catalogue rollback` rétablit le catalogue validé d'**avant le dernier
rafraîchissement** (il retire donc aussi ce rafraîchissement et les notes ajoutées depuis) et pose la suspension,
qui bloque rafraîchissement, notes et reprise ; `catalogue resume` lève la suspension, et le passage suivant
recherche de nouveau les notes manquantes.

This product uses the TMDB API but is not endorsed or certified by TMDB.

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
Image de démarrage : `uninstall.sh` traite d'abord `cmdline.txt`, `config.txt` et `plymouthd.conf` — tous vérifiés
avant la moindre écriture (partition montée, fichiers présents et attendus ; sinon refus, marqueurs et
`uninstall.sh` conservés pour réessayer). Il retire `splash`/`plymouth.ignore-serial-consoles` s'ils ont été ajoutés,
le suffixe `disable_splash=1` à l'octet près (s'il a été déplacé par une modification locale : seule la dernière ligne
`disable_splash=1` ajoutée, et son `[all]` seulement si aucune ligne non vide ne le suit — sinon `[all]` reste, pour
que les lignes ajoutées depuis n'héritent pas de la section conditionnelle précédente), rétablit `plymouthd.conf`
d'origine s'il n'a pas changé depuis (sinon seule la ligne `Theme=opentv` redevient le thème précédent ; un thème
choisi localement depuis est conservé), puis régénère l'initramfs en transaction (noyau v8 exact, sauvegarde SHA-256
avant, `lsinitramfs` + `initramfs8` = `initrd.img-<v>` après) et vérifie que le thème OpenTV n'y est plus. Échec :
octets d'avant restaurés et relus, puis **arrêt** (code 1) avant l'arrêt des services et le retrait des fichiers —
`/opt/opentv/uninstall.sh` et le marqueur restent ; corriger puis relancer `sudo /opt/opentv/uninstall.sh`.
Le paquet `plymouth` reste installé (sans `splash`, il n'affiche rien) ; `--purge` efface en plus l'état et les
sauvegardes OpenTV, jamais un paquet.

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
- CEC : `opentv-cec` déclare Playback « OpenTV » et fait une seule annonce de source active par démarrage (aucune
  réannonce, aucune commande de volume/standby). Sur la Samsung terrain, les six gestes ont été prouvés au bus, au noyau et visuellement après une
  annonce active manuelle consentie : Haut 103, Bas 108, Gauche 105, Droite 106, Select remappé vers Entrée 28 et
  Retour/Exit 174. Home ouvre la surcouche Samsung ; Home ou Retour la ferme, puis le contrôle revient à OpenTV. La TV
  n'émet aucun événement CEC détectable pour l'ouverture/fermeture de cette surcouche : ne jamais ajouter de réannonce
  périodique qui volerait le menu ou les applications Samsung. Le Pi réel a perdu la télécommande Samsung après un
  redémarrage tant qu'aucune annonce n'était faite ; l'annonce unique de démarrage corrige ce cas, **à revalider sur le
  terrain** (redémarrage du Pi, puis cycle Home ; extinction/rallumage complet). Protocole et matrice multimarque : [CEC_COMPATIBILITY.md](CEC_COMPATIBILITY.md).
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
  dépend) ; le compte `opentv-kiosk` possède tty1 et `opentv-kiosk.service` donne `input` à Cage (le compte lui-même
  n'en est plus membre ; le lecteur « Regarder » ne l'a jamais) : un clavier branché pilote Chromium en kiosque.
  **Gate terrain** de cette séparation : après `install.sh` puis redémarrage, `id opentv-kiosk` sans `input`,
  `/proc/<pid>/status` (Groups) de Cage avec `input`, de `playerd`/Chromium du lecteur sans `input`, et les sept
  touches CEC/USB toujours actives sur /tv. Atténué par la politique gérée (DevTools, impression, téléchargements, navigation externe bloqués), Cage
  sans changement de console et logind qui ignore Power ; pas de shell (`nologin`). Accès physique = confiance.
- Promotion du candidat Wi-Fi (`nmcli connection load <final> <candidat>` : l'UUID actif passe sur `opentv-wifi`,
  l'ancien UUID disparaît avec son fichier remplacé) : chemin de succès vérifié avec NetworkManager réel sur le Pi.
  Le mauvais mot de passe et sa restauration transactionnelle restent une gate terrain distincte.
- Serveurs HTTP et Agent Port : 16 connexions servies au plus (503 / `busy` au-delà), échéance globale par connexion
  (15 s HTTP, 10 s Agent Port) ; l'écriture d'une réponse vers un client qui ne lit pas reste bornée par le délai du
  socket, pas par l'échéance globale.
- « Pour toi » TV : la sélection est recalculée (catalogue relu + `Bridge.session`) toutes les 120 s au plus et aussitôt que la signature du catalogue change ; la TV la redemande toutes les 60 s et ne redessine les rangées que si la réponse a changé (différé pendant une fiche ou une lecture) ; coût CPU/mémoire de ce rechargement sur Pi 4 **à mesurer sur le terrain** (gros catalogue).
- Journal HTTP : erreurs et non-2xx toujours ; succès des polls (`/api/tv/*`, `/static/*`, `/healthz`) non
  journalisés, un échantillon agrégé `http_ok_sample` toutes les 600 requêtes.
- **« Regarder » : vérifié hors matériel seulement.** Livré désactivé (porte de production §6 bis). Avant
  `playback promote --field-validated` : sur la TV réelle, Regarder → lecteur seul plein écran (jamais la page du site)
  → Retour CEC/USB/téléphone → même carte ; « Lecture indisponible » sans lecteur ; tenue de Retour pendant la
  fermeture sans effet sur la fiche ; vidéo de l'apparition éventuelle d'un onglet vide lors d'une surgissante
  (limite Chromium) ; F12/Ctrl+Maj+I sur un clavier branché ⇒ retour OpenTV sans DevTools visible ; `Target.getTargets`
  liste bien la cible DevTools et `Target.closeTarget` la ferme sur le Chromium de Raspberry Pi OS ; décodage et
  thermique pendant la lecture ; `uninstall.sh` puis réinstallation ⇒ bouton absent jusqu'à nouvelle promotion.
- Notes TMDb (`opentv-catalogue-ratings.service`, reprise `opentv-catalogue-ratings-retry.timer`) : `OnSuccess=`
  (systemd ≥ 249, Bookworm 252), ordre `After=` entre reprise et rafraîchissement, masque du dossier
  `/etc/opentv/tmdb` dans les espaces de montage réels, condition `!/run/opentv-uninstalling`, résolution DNS et
  HTTPS vers `api.themoviedb.org` sous ce bac à sable, couverture réelle après un passage, et absence de jeton dans
  le journal : **non vérifiés sur la Pi**, à prouver par la ligne « Notes TMDb quotidiennes » de la grille terrain.
- Catalogue : à copier une première fois dans `/var/lib/opentv/catalogue.json` (propriétaire `opentv`, 0600) depuis la
  chaîne acceptée ; sans lui « Pour toi » affiche une sélection vide. Rafraîchissement quotidien (§6 ter) **vérifié hors
  matériel seulement** : à prouver sur la Pi (`systemd-analyze verify`/`security opentv-catalogue-refresh`, résolution
  DNS sous `IPAddressDeny`/`RestrictAddressFamilies`, un passage réel `updated` puis `unchanged`, `catalogue status`
  cohérent, aucune adresse dans `journalctl -u opentv-catalogue-refresh`, accueil peuplé sans redémarrage). La page de
  liste du site donne-t-elle bien tout le catalogue accepté (196 éléments au dernier relevé) ? Sinon chaque passage
  sera refusé (`count_drop`) jusqu'à ce que la source fournisse une liste complète.

## 9. Checklist terrain (critères distincts)

| Critère | Preuve exigée |
|---|---|
| **Configuré** | `sudo /opt/opentv/bin/opentv-pi preflight --text` OK ; `install.sh` sans erreur ; `opentv-pi health` → `ok:true`, manifeste OK, `opentv.service` et `opentv-net-helper.path` actifs, `NRestarts=0` (`systemctl show -p NRestarts opentv`) |
| **QR / réseau** | QR visible ; formulaire ouvert sur le téléphone ; second envoi du même QR refusé ; Ethernet puis Wi-Fi (`nmcli -t device status`) ; mot de passe absent de `journalctl -u opentv -u opentv-net-helper` et de `/var/lib/opentv` ; port 8081 fermé après configuration (`ss -ltn`) ; hotspot sans Ethernet : réseau `OpenTV-XXXX` visible puis disparu après succès ; hotspot laissé sans configuration : disparu après 20 min et absent de `nmcli connection show` ; hotspot + Ethernet + « garder » : AP supprimé ; **hotspot actif puis `systemctl stop opentv`** : AP disparu en ≤ 60 s (`journalctl -u opentv-ap-guard` : `ap_guard_stop`) ; mauvais mot de passe Wi-Fi : nouveau QR en ≤ 210 s ; `systemctl show -p ExecMainStartTimestamp,ActiveEnterTimestamp opentv-net-helper` < 120 s par activation ; `uninstall.sh` avec hotspot actif : AP absent ensuite |
| **UI visible** | photo/vidéo de la TV : tableau de bord 1920×1080 net (`wlr-randr` ou `/sys/class/drm/card*-HDMI-A-1/modes`), « Pour toi » navigable au clavier, message d'agent affiché ; `kill` de Chromium ⇒ retour automatique du kiosque ; `systemctl restart opentv` ⇒ « Reconnexion… » puis rechargement automatique et données de retour ; mode 1920×1080 effectif après redémarrage de la boîte |
| **CEC** | `sudo /opt/opentv/bin/opentv-pi cec` liste `/dev/cec0` avec une adresse physique ≠ `f.f.f.f` TV allumée ; `journalctl -b -u opentv-cec` : un seul `"boot_done"` puis seulement `"already_this_boot"`/`"boot_window_passed"` ; `cec-ctl -d /dev/cec0` : OSD `'OpenTV'`, masque ≠ `0x0000` ; après **redémarrage** : idem sans intervention, `announced_this_boot: true` ; TV en veille pendant toute la fenêtre de démarrage, puis allumée : **la TV ne se rallume pas et ne change pas d'entrée** à cause d'OpenTV (`cec-ctl -d /dev/cec0 -m` : aucune `IMAGE_VIEW_ON`/`ACTIVE_SOURCE` émise par OpenTV) |
| **Télécommande CEC** | après le démarrage (annonce automatique, sans `--activate`), TV allumée pendant la fenêtre : Haut, Bas, Gauche, Droite, OK (ouvre le panneau), Retour (le ferme) ; puis **après `sudo reboot`**, sans intervention, de nouveau les six touches ; Home Samsung ouvert pendant ≥ 3 min puis fermé : aucun retour forcé sur OpenTV, contrôle rendu à OpenTV ; `sudo systemctl restart opentv opentv-cec` pendant Home : menu Samsung intact ; touche Power de la TV : la boîte reste allumée ; un ensemble partiel = critère CEC échoué (repli téléphone) ; réactivation explicite seulement par `sudo /opt/opentv/bin/opentv-pi cec --activate` (TV allumée, jamais de réveil ni `IMAGE_VIEW_ON` au moniteur ; `activation.ok: true` seulement si l'annonce a été transmise ce démarrage) |
| **Télécommande téléphone** | iPhone et Android sur le même Wi-Fi : QR de la colonne de droite → « Oui, utiliser ce téléphone » → les 7 touches agissent en < 0,5 s ; l'URL ouverte est `<nom>.local:8082` (sinon noter le repli) ; ancien QR refusé ; « Oublier les téléphones » (Réglages) ⇒ le pavé demande de rescanner ; `systemctl restart opentv` puis `sudo reboot` (heure avant NTP) ⇒ téléphone toujours appairé ; nouvelle adresse DHCP ⇒ téléphone toujours appairé ; `ss -ltn` : `:8082` lié à l'adresse LAN (jamais `0.0.0.0`), absent pendant « Reconfigurer » ; ajout à l'écran d'accueil iOS fonctionnel ; aucun jeton dans `journalctl -u opentv` |
| **Télécommande USB** | télécommande 2,4 GHz branchée : flèches, OK, Retour, Accueil sur l'écran OpenTV ; Accueil/BrowserBack ne quittent jamais `/tv` ; écran QR : aucune touche n'agit ; **touche Power : la boîte reste allumée** (après redémarrage) ; clavier : F12/Ctrl+Maj+I, Ctrl+P, Ctrl+S, Ctrl+Alt+F2 sans effet |
| **Décodage** | H.264 local 1080p60 pendant 30 min : `/dev/video10` ouvert à chaque sonde, horloge H.264 ≈ 500 MHz, maximum 0,372 % d'images perdues, température maximale 41,3 °C, `hvs_underrun=0`, `get_throttled=0x0` ; kiosque restauré et résidus supprimés. 4K/HEVC/VP9/AV1 Chromium hors cible. |
| **Image de démarrage** | vidéo de la mise sous tension complète : **aucun arc-en-ciel**, image OpenTV (fond sombre, marque, « Démarrage… ») dès le noyau, sans texte Linux ni terminal, sans clignotement de console avant la première image de Chromium (au plus un noir bref de Cage) ; `sudo /opt/opentv/bin/opentv-boot-check` : `arc-en-ciel désactivé`, `thème opentv sélectionné`, `initramfs : thème opentv présent`, `écran texte NON écrit`, `retain-splash, DRM vc4`, `aucun secours Plymouth`, `Plymouth terminé`, `NRestarts=0`, `aucun SIGHUP`, `TTYReset=no TTYVHangup=no` ; `journalctl -b -u opentv-plymouth-quit` : `DRM vc4 /dev/dri/cardN tenu par le kiosque` ; `systemd-analyze verify` des unités kiosque/passage ; coupure secteur volontaire pendant `update-initramfs` NON exigée (prouvée hors matériel) ; arrêt/redémarrage : image sans « Démarrage… » ; mode de secours (`systemctl rescue` en essai contrôlé) : console visible ; `uninstall.sh` puis redémarrage : arc-en-ciel et démarrage d'origine rétablis |
| **Rollback** | `uninstall.sh` ⇒ console tty1 de retour, `/opt/opentv` absent, fichiers d'origine restaurés ; réinstallation propre ; `--purge` sans perte du Wi-Fi |
| **Assistants (Réglages)** | TV : Réglages → « Gérer les assistants » → « Non » au focus → « Oui » → QR dans la feuille, colonne Télécommande masquée ; iPhone (appareil photo) : 3 étapes, « Autorisé » et « Connecté » distincts, « Oui, enregistrer » ; TV : accueil + « C'est enregistré ! Assistants autorisés : N · connectés : M » ; `ss -ltn` : `:8081` lié à l'adresse LAN pendant le mode seulement, puis fermé ; Wi-Fi/nom inchangés (`nmcli -t device status`, `device.json`) ; aucun nouveau fichier dans `/etc/opentv/agents` ; second envoi du même QR refusé ; photo d'Alex |
| **Regarder** | `opentv-pi playback status` → `promotion: missing` après installation (bouton absent même avec `source.env`) ; après les essais HDMI ci-dessus et `playback promote --field-validated` : bouton présent ; `playback demote` pendant une lecture ⇒ lecteur fermé, retour même carte, bouton absent en ≤ 10 s ; télécommande USB débranchée et CEC absent ⇒ bouton absent (`devices: 0`) ; unité du lecteur : décodage H.264 matériel toujours actif (`/dev/video10` tenu) avec `DevicePolicy=closed` + `SystemCallFilter=~@debug`, `systemd-analyze security opentv-player` relu, `/dev/cec0`/`/dev/input` inaccessibles depuis le lecteur, `curl http://127.0.0.1:<port TV>` refusé depuis l'unité ; groupes effectifs (`/proc/<pid>/status`) : Cage avec `input`, `playerd`/Chromium du lecteur sans `input`, `opentv-player-keys` avec `input` seulement ; photo/vidéo d'Alex |
| **Catalogue quotidien** | `systemctl list-timers opentv-catalogue-refresh.timer` : prochain passage planifié ; `sudo /opt/opentv/bin/opentv-pi catalogue refresh` → `updated` (ou `unchanged`), puis `catalogue status` : nombre attendu, `snapshot` présent, SHA-256 lu ; second passage → `unchanged` sans réécriture ; `catalogue rollback` → `rolled_back`, `hold.held: true`, puis `catalogue refresh` → `held` et SHA-256 inchangé ; `catalogue resume` → `resumed`, passage suivant `updated`/`unchanged` ; fiche ouverte ou lecture pendant un remplacement ⇒ Retour sur la même carte (photo) ; `ls -l /var/lib/opentv/catalogue.json*` : tout `opentv` ; Wi-Fi coupé → `discovery_failed` et SHA-256 inchangé ; accueil peuplé avec le nouveau catalogue en ≤ 120 s (attendu ≤ ~62 s) sans redémarrage de service ; `journalctl -u opentv-catalogue-refresh` sans hôte, préfixe, jeton ni URL ; `systemd-analyze security opentv-catalogue-refresh` relu |
| **Notes TMDb quotidiennes** | `ls -ld /etc/opentv/tmdb` = `root opentv` `drwxr-x---` dès l'installation ; `getent group opentv` sans membre supplémentaire ; `sudo /opt/opentv/bin/opentv-pi catalogue ratings-credential` (jeton depuis le trousseau, jamais affiché) puis `ls -l /etc/opentv/tmdb/token` = `root opentv` `-rw-r-----` ; pour `opentv`, `opentv-cec` (pendant un passage) et le rafraîchissement : `grep ' /etc/opentv/tmdb ' /proc/<MainPID>/mountinfo` présent et `sudo nsenter -t <MainPID> -m ls /etc/opentv/tmdb` refusé ; jeton remplacé une 2e fois ⇒ masque toujours présent dans `opentv` sans redémarrage ; `catalogue refresh` puis `systemctl show -p ActiveEnterTimestamp,Result opentv-catalogue-ratings` (déclenchée après le rafraîchissement) ; `catalogue status` : `ratings.last.result` `updated`/`unchanged`, `coverage.pending` = 0, `rated` cohérent (aujourd'hui 110/116 : un spécial TV sans vote TMDb reste « indisponible », jamais 0) ; second `catalogue ratings` → `unchanged`, SHA-256 inchangé ; `IPAddressDeny` et DNS : passage réel réussi sous le bac à sable ; panne simulée (réseau coupé pendant le seul passage des notes) → `provider_unavailable`, unité `failed`, `health.warnings` = `ratings_enrichment_failed`, catalogue du rafraîchissement inchangé (SHA-256), puis réseau rétabli ⇒ `systemctl list-timers opentv-catalogue-ratings-retry.timer` et reprise réussie dans ≤ ~1 h 30 (`ratings.retry.attempts` 1, résultat `updated`), et heure suivante : reprise sans requête ; `journalctl -u opentv-catalogue-ratings*` et `-u opentv-catalogue-refresh` sans jeton, titre, hôte ni URL ; `systemd-analyze verify` et `security` des deux unités des notes relus ; `uninstall.sh` pendant un rafraîchissement ⇒ aucun passage des notes démarré (journal), `/run/opentv-uninstalling` retiré à la fin ; nouvelle note visible à l'accueil en ≤ 120 s sans redémarrage |
| **Agent** | `opentv-mcp` depuis le compte de l'agent : `tools/list` filtré par scopes, `recommend` sans URL, scope refusé, audit présent sans secret |

## 10. Développement

```sh
python3 -m unittest discover -s scripts/pi4/tests -t scripts/pi4      # depuis la racine du dépôt
python3.11 -m unittest discover -s scripts/pi4/tests -t scripts/pi4   # version de Bookworm
# bouts en bout réels (Chromium headless, temps réel) et analyse ES5 :
#   OPENTV_TEST_PLAYWRIGHT=<module playwright> OPENTV_TEST_ACORN=<module acorn> OPENTV_TEST_SHOTS=<dossier de captures>
#   (formulaires du téléphone sans JavaScript, Origin: null refusé, écran TV 1920×1080 sans chevauchement du QR,
#    « Gérer les assistants » de bout en bout : tests/e2e_agents.js, captures tv-agents-*.png / phone-agents-*.png ;
#    « Regarder » : tests/e2e_play.js, captures item/loading/playing/held/returned/unavailable.png ;
#    validation des calques du lecteur dans un vrai Chromium : tests/e2e_assist.js)
./scripts/pi4/install.sh --dry-run ; ./scripts/pi4/install.sh --root /tmp/rfs --skip-preflight
# image Plymouth : régénérée (Chromium headless, aucun réseau) puis réencodée en PNG RVB déterministe ; fichiers livrés
#   scripts/pi4/system/plymouth/opentv.png (1920×1080) et opentv-mask.png (bande sans « Démarrage… »)
python3 scripts/pi4/tools/make-plymouth-theme.py
```
`tests/test_plymouth.py` simule Plymouth et initramfs-tools dans des racines de test (fichiers Debian + faux
`update-initramfs`/`lsinitramfs` dans le PATH, activés par `OPENTV_INSTALL_TEST_BOOTCMDS=1` hors vraie racine
seulement ; le faux `update-initramfs` imite Raspberry Pi OS : `initrd.img-<v>` renommé, puis copie EN PLACE vers
`initramfs8`, avec troncature, blocage, écrivain qui ignore SIGTERM et SIGKILL d'`install.sh`/`uninstall.sh` simulés).
Ils prouvent l'ordre (thème complet et `Theme=opentv` avant la régénération), le noyau exact, l'idempotence, les
retours arrière et la restauration à l'octet après coupure (installation, retour arrière, désinstallation), la
désinstallation exacte, et l'absence de commande annexe sous réglages TTY (unités) — pas l'affichage : l'image réelle, l'absence d'arc-en-ciel/texte et le passage
sans clignotement restent la porte terrain « Image de démarrage » (§9).
