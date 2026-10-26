# OpenTV — guide pour les agents

Document de passation : comment OpenTV est construit, comment le livrer sur la TV, ce qui a été
essayé et pourquoi certaines choses sont impossibles. À lire en entier avant de modifier quoi que ce soit.
Langue de travail avec Alexandre : **français**, réponses courtes et directes. Toujours pousser sur `main`.

---

## 1. En une minute

- **OpenTV** = écran d'accueil maison pour une TV Samsung, qui remplace le fouillis du Smart Hub :
  tuiles des applis installées avec leurs vrais logos, fonds photo, panneau « Apparence »,
  télécommande depuis le téléphone, et une veille qui affiche **HDMI 1 en direct** avec une rangée
  d'applis en bas (comme le Home Samsung).
- C'est une **application web Tizen native** (`.wgt`), avec sa propre icône, installée en **mode
  développeur**. Elle ne dépend ni de TizenBrew ni d'Internet (sauf Google Fonts, avec repli).
- Code actuel : dossier **`native/`**. Version en production sur la TV : **OpenTV v20 native**
  (service v17). Les fichiers à la racine (`app/`, `service.js`, `package.json`) sont l'**ancien
  module TizenBrew** (v1 à v12), gardé pour l'historique : ne pas les modifier.

## 2. Le matériel

| Élément | Valeur |
|---|---|
| TV | Samsung **UN55NU6900** (2018), Tizen **4.0**, moteur web **Chromium 56** (menu de service : `U-F-NU71-55`, région USA) |
| IP TV | `192.168.2.14` (réseau local d'Alexandre) |
| Mac d'Alexandre | `192.168.2.13` (MacBook Air M4) = **« Host PC IP »** du mode développeur de la TV |
| DUID TV | `uuid:abc7fc91-3509-47c5-99b6-c69d9c3113b8` |
| Source principale | **HDMI 1** (un tableau de bord / PC) |
| Compte Samsung sur la TV | **pas celui d'Alexandre** → ne pas y toucher (changer de compte demande une réinitialisation) |

Mode développeur : sur la TV, Applis → `12345` → Developer mode ON + Host PC IP. Seule l'IP réglée
là peut parler à la TV par **sdb** (port `26101`). Si l'IP ne correspond pas, la connexion TCP
s'ouvre mais la TV ne répond jamais à la poignée de main (timeout). Changer l'IP impose un
redémarrage de la TV.

## 3. Arborescence

```
AGENTS.md            ce document
native/              ← L'APPLI ACTUELLE
  config.xml         manifeste Tizen (ids, privilèges, service, icône)
  icon.png           icône OpenTV 512×512
  app/index.html     page (charge qrcode.js, config.js, app.js avec ?b=<version>)
  app/app.js         toute la logique de l'écran (ES5, ~650 lignes, une IIFE)
  app/config.js      catalogue des marques, actions, fonds, designs, réglages d'apparence
  app/app.css        styles (compatibles Chromium 56)
  app/qrcode.js      générateur de QR (bibliothèque minifiée)
  app/logos/ backgrounds/ designs/   SVG des marques, photos de fond, vignettes de designs
  service/index.js   service Node de fond (Node 4.4, ES5) : HTTP sur :8096
  INSTALL.md         notes de build/installation
  test/              tests Playwright + fausse TV (voir §9)
scripts/
  build.py           construit + signe + vérifie dist/OpenTV.wgt (une commande, voir §7)
  deploy.sh          envoi + install + relance + vérification (à lancer là où sdb atteint la TV)
  install.mjs        envoi du .wgt sur la TV (protocole sync de sdb, en JS pur)
  sdb.mjs            exécute des commandes « shell: » sdb (en JS pur)
  autostart.mjs      ancien prototype Node d'autostart (ne pas déployer sur le NanoPi)
  nanopi/            autostart Python supervisé, pont Films/« Pour toi » (opentv_foryou.py, FORYOU.md),
                     installation, tests et retour arrière
docs/plans/          plans d'implémentation (ex. 2026-10-25-pour-toi.md)
app/ service.js package.json README.md   ancien module TizenBrew (historique)
```

## 4. Architecture de l'appli native

### 4.1 Manifeste (`native/config.xml`)
- Package `OpnTV00001`, appli `OpnTV00001.OpenTV`, nom « OpenTV », `required_version="3.0"`,
  profil `tv-samsung`, contenu `app/index.html`.
- Service : `OpnTV00001.OpenTVService` → `service/index.js`, avec `auto-restart="true" on-boot="true"`
  (ignorés par cette TV, voir §11).
- Privilèges (tous **publics**, compatibles avec notre signature) : `internet`, `network.public`,
  `tv.inputdevice`, `unlimitedstorage`, `application.launch`, `application.info`, `tv.window`.

### 4.2 Écran (`app/app.js`)
Une seule IIFE, aucune dépendance. Points clés :

- **Démarrage (`boot`)** : applique les préférences, lance l'horloge, démarre le service
  (`startService`), lit la liste des applis (`listApps`), puis affiche l'accueil (`goHome`).
- **Liste et lancement des applis** : directement par l'API Tizen de la page
  (`tizen.application.getAppsInfo` / `tizen.application.launch`). Le service n'est **pas** nécessaire
  pour ça. Hors TV (navigateur), un catalogue de démonstration est généré.
- **Reconnaissance des applis** : `brandFor(id)` fait correspondre le **nom** de l'appli installée au
  catalogue `NOVA_BRANDS` (config.js : nom, mots-clés `match`, logo, couleurs). `pickForBrand` /
  `dedupeByBrand` gardent une seule tuile par marque (la TV a 2 ou 3 « YouTube », etc.).
  `isSystemApp` cache les composants système (préfixes `org.tizen.`, `com.samsung.`, `com.webos.`,
  `org.volt.`, OpenTV lui-même, et une liste de mots dans les noms). La TV renvoie ~103 applis, dont
  presque toutes système : l'accueil n'en montre que 6 (Netflix, YouTube, Prime Video, Spotify,
  Live TV, Internet).
- **Écrans** : pile `stack` (`home`, `apps` = « Choisis tes applis », `designs`, `phone`) + panneau
  superposé `look` (Apparence). `open()`, `back()`, `goHome()`.
- **Navigation à la télécommande** : spatiale. `move(dir)` choisit l'élément `.focusable` le plus
  proche dans la direction (distance + écart latéral × 2,2). Pas de focus natif du navigateur : la
  classe `.focused` marque l'élément. `select()` exécute l'action liée (`bind(key, fn)` sur
  `data-key`). Touches : flèches, Entrée, Retour Samsung `10009`, Exit `10071`.
- **Préférences** : `localStorage["opentv.prefs"]` (fond, tuiles, libellés, logo, police, horloge,
  veille, applis choisies). Appliquées comme classes CSS sur `#stage` (`font-*`, `tiles-*`…).
- **Retour au premier plan** (`visibilitychange`) : retour à l'accueil, sélection sur la première
  tuile. Passage en arrière-plan : coupe la vidéo HDMI.

### 4.3 Veille « HDMI 1 en direct » (machine d'état `LIVE`)
- `""` normal → `"full"` : menu descendu, HDMI 1 plein écran → `"peek"` : rangée d'applis en bas,
  par-dessus HDMI 1.
- Entrées : 3 min d'inactivité sur l'accueil, ou **Retour** depuis l'accueil (si Apparence → Veille =
  « HDMI 1 en direct »). En `full`, la 1re touche fait remonter le menu (`peek`) sans déplacer la
  sélection. Retour, ou 10 s d'inactivité → redescend. Lancer une appli, ouvrir un écran ou
  Apparence → `liveOff()` (vidéo coupée, fond photo revenu).
- Vidéo : `tizen.systeminfo.getPropertyValue("VIDEOSOURCE")` → source HDMI n°1 (même débranchée),
  puis `tizen.tvwindow.setSource(...)` et `tizen.tvwindow.show(ok, err, ["0px","0px","1920px","1080px"], "MAIN", "BEHIND")`.
  La vidéo est **derrière** la page : `html.live` rend `html`/`body` transparents, `#stage.live`
  cache le fond photo, l'horloge, le titre et l'aide. En `peek` : tuiles compactes sur **une rangée**
  en bas (défilement horizontal `keepInRow`), boutons ronds à droite, dégradé sombre en bas
  seulement. Échec vidéo → retour à l'horloge (`liveFail`), jamais d'écran noir.

### 4.4 Service (`native/service/index.js`, Node 4.4)
Démarré par la page via
`tizen.application.launchAppControl(new tizen.ApplicationControl("http://tizen.org/appcontrol/operation/service"), "OpnTV00001.OpenTVService")`.
Serveur HTTP sur `0.0.0.0:8096` :

| Route | Rôle |
|---|---|
| `/version` | `{version}` (actuellement 17) |
| `/info` | `{ip, port, version}` (sert au QR de la télécommande) |
| `/remote` | page web de télécommande pour téléphone (pavé tactile : glisser = flèches, toucher = OK, Retour, Accueil) |
| `/cmd?cmd=up\|down\|left\|right\|ok\|back\|home\|live` | dépose une commande |
| `/poll` | `{n, cmd}` : l'écran interroge toutes les 400 ms et exécute les nouvelles commandes |
| `/report?build=&screen=&apps=&focus=&live=&tvw=` | l'écran rapporte son état |
| `/status` | état complet : version, dernier rapport de l'écran, journal du démarrage auto, uptime |
| `/apps`, `/launch?id=` | liste/lancement via le service (repli de l'ancien mode module) |
| `/shutdown` | arrête le service (utilisé pour remplacer une ancienne version) |

- **Remplacement après mise à jour** : l'écran exige `version >= SVC_VERSION`. Si un ancien service
  tourne, il en lance un nouveau, qui obtient `EADDRINUSE`, lit `/version` de l'ancien et lui envoie
  `/shutdown` s'il est plus vieux.
- **Démarrage auto (tentative)** : le service relance OpenTV s'il détecte un saut d'horloge > 20 s
  (sortie de veille) ou s'il démarre < 180 s après le boot. Inopérant sur cette TV (§11), sans danger.
- Le téléphone **ne peut pas** piloter la TV elle-même (volume, allumage, applis Samsung) : voir §11.

## 5. Contraintes Chromium 56 / Node 4.4 (à respecter absolument)

- **JS en ES5** : pas de `let`/`const`, de fonctions fléchées, de gabarits `` ` ``, de `class`, de
  `async`. Vérifier avec `npx esbuild@0.19.12 native/app/app.js --target=chrome56 --outfile=/dev/null`.
  Service : Node 4.4, `new Buffer`, pas de `Buffer.from`, pas de `WebSocket` global.
- **CSS** : pas de `grid`, pas de `gap` en flexbox, pas de `inset`, pas de `backdrop-filter`. Mises en
  page en `inline-block`/flexbox + marges, positions `top/left/right/bottom` explicites. Préfixes
  `-webkit-` pour les transitions et transformations.
- **`scrollIntoView({block:"nearest"})` est ignoré** par Chromium 56, qui aligne alors l'élément en
  HAUT. Utiliser `scrollIntoViewIfNeeded(false)`, et **jamais** pendant le mode direct : c'est la
  cause du bug v19 (la rangée apparaissait en haut puis redescendait). La page elle-même ne doit
  jamais défiler (`resetPageScroll`).
- `XMLHttpRequest` en échec déclenche `readyState 4` **et** `onerror` : la fonction `xhr()` a un garde
  « une seule réponse ». Ne pas le retirer, sinon les tentatives se dédoublent en boucle.
- **Cache** : la TV peut garder d'anciens fichiers. Chaque version change `?b=<n>` dans `index.html`.

## 6. Versions

- `BUILD` dans `app.js` (affiché en bas à droite, ex. `OpenTV v20 native`) et `?b=` dans `index.html`
  : à monter **ensemble** à chaque livraison.
- `SVC_VERSION` (app.js) **=** `VERSION` (service/index.js) : à monter ensemble seulement si le
  service change.

## 7. Construire et signer le `.wgt`

Même méthode que TizenBrew pour les TV 2017-2018 : **certificat d'auteur maison + certificat
distributeur Tizen public du SDK**. Aucun compte Samsung, aucun DUID.

- Outil : Tizen Studio **CLI** 5.6 pour Linux (`web-cli_Tizen_Studio_5.6_ubuntu-64.bin`, sur
  download.tizen.org). L'installeur refuse root : l'installer sous un utilisateur normal
  (`--accept-license <dossier>`).
- Profil de signature (`OpenTV-Old`) dans `tizen-studio-data/profile/profiles.xml` :
  - auteur : `opentv-author.p12` (CA `developer/tizen-developer-ca.cer`) ;
  - distributeur : `distributor/sdk-public/tizen-distributor-signer.p12`, mot de passe
    `tizenpkcs12passfordsigner` (CA `tizen-distributor-ca.cer`).
- **La clé d'auteur n'est pas dans Git.** Elle est sur le Mac d'Alexandre :
  `~/Downloads/Nova-TV-Free/opentv-signing/` (`opentv-author.p12` + `opentv-author.password.txt`).
  **Toutes les mises à jour doivent être signées avec elle**, sinon la TV refuse de remplacer l'appli.
- **En une commande** (fait tout ce qui suit et vérifie le résultat) :
  ```sh
  OPENTV_KEY=/chemin/opentv-author.p12 OPENTV_KEY_PASSWORD_FILE=/chemin/opentv-author.password.txt \
      python3 scripts/build.py          # -> dist/OpenTV.wgt
  ```
  Testé depuis une copie neuve du dépôt : produit exactement le paquet installé sur la TV.
- Commandes équivalentes à la main :
  ```sh
  tizen build-web -e '.*' -- native            # produit native/.buildResult
  tizen package -t wgt -s OpenTV-Old -o release -- native/.buildResult
  ```
  Pièges : `tizen package` demande le mot de passe auteur sur un vrai terminal (TizenBrew passe par
  `expect`, nous par `pexpect`). Après la 1re signature, le CLI réécrit le mot de passe dans
  `profiles.xml` et n'arrive plus à le relire (« Invaild password ») : **régénérer `profiles.xml` en
  clair avant chaque signature**. Ne jamais supprimer `/dev/null` dans la machine de build (le script
  `tizen.sh` en dépend).
- Contrôle : le `.wgt` doit contenir `author-signature.xml` et `signature1.xml` ; la chaîne doit être
  « Tizen Developers CA » (auteur) et « Tizen Public Distributor CA » (distributeur), comme le TizenBrew officiel.

## 8. Installer sur la TV

Machine qui installe = celle dont l'IP est la **Host PC IP** (aujourd'hui le Mac). Dans les sessions
Claude, on passe par le pont vers le Mac : une VM Linux (sous-réseau NAT `172.16.10.x`) qui joint la
TV ; le dossier connecté `~/Downloads/Nova-TV-Free` y apparaît dans `~/mnt/Nova-TV-Free`.

```sh
scripts/deploy.sh 192.168.2.14 dist/OpenTV.wgt
```
1. `install.mjs` pousse le `.wgt` dans `/home/owner/share/tmp/sdk_tools/OpenTV.wgt` (sync sdb) ;
2. `0 vd_appinstall OpnTV00001 <chemin>` → attendre `install completed` et `cmd_ret:0` ;
3. `0 debug OpnTV00001.OpenTV` → relance **à neuf** (`0 was_execute` ne fait que ramener au premier
   plan l'instance préchargée, qui garde l'ancien code) ;
4. lit `http://192.168.2.14:8096/status` : `client.build` doit afficher la nouvelle version.

Les commandes sdb autorisées en mode développeur sont limitées (`0 vd_appinstall`, `0 was_execute`,
`0 debug`, `0 getduid`…) : pas de shell libre, pas de lecture de fichiers.

## 9. Tests (`native/test/`, Playwright, Chromium préinstallé)

Décompresser le `.wgt` livré dans `native/test/wgt/`, puis lancer depuis `native/test/` :

| Script | Vérifie |
|---|---|
| `test.js` | 14 vérifications : service lancé par `launchAppControl`, `/version`, tuiles (doublons, système cachés), version affichée, télécommande téléphone, lancement d'appli, `/remote`, `/status`, retour au premier plan, aucune erreur JS, service démarré une seule fois |
| `live.js` | 12 vérifications du mode direct : HDMI 1 choisi, vidéo DERRIÈRE, page transparente, menu descendu/remonté, Retour, lancement → vidéo coupée, inactivité 3 min |
| `animcheck.js` | la rangée monte **uniquement depuis le bas**, la page ne défile jamais (Chromium moderne + comportement Chromium 56 simulé) |
| `scroll.js` / `visual.js` | défilement de la rangée avec 12 applis ; captures avec une fausse image HDMI |
| `faketv.mjs` | fausse TV (port sdb + service) pour tester `autostart.mjs` |
| `test_opentv_autostart.py` | 43 tests stdlib du démon Python NanoPi : SDB fragmenté, cycles, délais, reprises, CEC, journaux, installation et rollback |
| `ext.js` / `bridge_e2e.js` | tuiles externes « Films » et « Pour toi » contre un faux NanoPi / le vrai pont (33 / 25) |
| `test_opentv_foryou.py` | 70 tests « Pour toi » : base SQLite (schéma, idempotence, vu ≠ ouvert, référence non vide), contention SQLite < 1 s (RESERVED/EXCLUSIVE/corrompue), moteur, navigation, CLI agent sans URL, UID `films/<site_id>` stable malgré SITE_ROOT, pont, Chromium réel |

Les tests remplacent l'API `tizen` par une maquette (`addInitScript`). Ils ne remplacent pas la TV :
après chaque livraison, vérifier `/status` sur la vraie TV, et faire confirmer l'écran par Alexandre
(photo ou vidéo).

## 10. Diagnostic à distance

```sh
curl http://192.168.2.14:8096/status          # version, écran, applis, focus, mode direct, résultat tvwindow
curl "http://192.168.2.14:8096/cmd?cmd=right"  # piloter l'écran comme la télécommande téléphone
curl "http://192.168.2.14:8096/cmd?cmd=live"   # forcer le mode HDMI 1 en direct
```
`status.client.tvw` = `src HDMI1 show ok` quand la vidéo est affichée. `status.uptime` = secondes
depuis le démarrage de la TV (petit = la TV a redémarré à froid).

## 10 bis. Le NanoPi (source HDMI 1, passerelle de Muse)

Information transmise par Muse (agent d'Alexandre), état au 24 oct. 2026 — non vérifié depuis le dépôt :

- **NanoPi M4B** (FriendlyELEC, RK3399, 2 Go), Debian 13, branché en HDMI sur la TV : **c'est la source
  HDMI 1**. LAN `192.168.2.28`, Tailscale `100.101.68.113`. Le tableau de bord qu'on voit derrière
  OpenTV en mode direct, c'est lui (`muse-display.service`, `/home/alex/muse-tv-ui/`, `dash.json`).
- **Muse** (l'assistant IA) y est relié par le Muse Gadget SDK Linux (`/opt/musegadget`,
  `musegadget.service`, gadget « NanoPi-M4B »). Muse n'a pas de SSH : il agit uniquement par la
  commande `system.run` du SDK (utilisateur `alex`).
- **Hermes** (autre agent d'Alexandre) a l'accès SSH (utilisateur `alex`, par Tailscale) pour le travail
  de fond. Les décisions sensibles restent à Alexandre.
- **HDMI-CEC** fonctionne depuis le NanoPi (`/dev/cec0`) : veille, réveil, lecture de l'état
  d'alimentation de la TV. Les touches relayées par CEC (volume, chaînes) sont ignorées par la TV.
- **Décision d'Alexandre (24 oct.)** : l'intégration Muse ↔ OpenTV passe par le NanoPi. Muse appelle
  le service d'OpenTV en HTTP sur le LAN (`http://192.168.2.14:8096/status`, `/cmd?cmd=…`), ce qui ne
  marche que lorsqu'OpenTV est ouvert.
- **Pistes et décisions** :
  1. API agent dans OpenTV (`/show` pour afficher n'importe quoi, `/open?app=`, `/live`, `/home`) ;
  2. démarrage auto d'OpenTV depuis le NanoPi : **autorisé le 25 oct. 2026**. La Host PC IP a été
     changée par Alexandre vers `192.168.2.28` et la poignée de main SDB puis le lancement manuel
     d'OpenTV v20/service v17 ont été vérifiés. Utiliser uniquement `scripts/nanopi/` ;
  3. navigateur moderne : Chromium sur le NanoPi (sortie HDMI 1), piloté par Muse, au lieu d'un
     navigateur dans OpenTV (qui aurait le Chromium 56 de la TV).
- Pièges `system.run` : un processus d'arrière-plan meurt à l'expiration de l'appel (utiliser
  `setsid nohup … < /dev/null &`), `pkill -f` peut se tuer lui-même. Ne pas toucher au rendu du
  tableau de bord, au réseau, au SDK ni aux services sans l'accord d'Alexandre.

## 10 ter. « Pour toi » (OpenTV v23, côté NanoPi)

Tuile externe `pourtoi` (après « Films ») : sélection personnelle du même catalogue accepté, actions à la
télécommande (Regarder, J'aime, Pas pour moi, Déjà vu, À voir plus tard, Pas ce soir), historique local SQLite
(`foryou.sqlite3` 0600 sous `STATE_DIR` du pont, jamais dans Git) et CLI JSON locale pour Muse/Hermit
(`python3 -I ~/opentv-bridge/opentv_foryou.py status|recommend|search|feedback|history`). Aucun nouveau port,
aucune URL dans la CLI ; identifiants `films/<site_id>` (`FORYOU_SOURCE_ID`, indépendants du domaine) ;
ouvrir un film n'est jamais « vu ». Détails, commandes exactes et limites :
`scripts/nanopi/FORYOU.md`. Service Tizen inchangé (v17) ; mettre à jour le pont avant la WGT v23.

## 11. Limites connues (testées, ne pas reproposer sans élément nouveau)

- **Remplacer Tizen / démarrer directement sur OpenTV / détourner le bouton Home** : impossible,
  démarrage et touche Home verrouillés par Samsung, pas de root public pour ce modèle.
- **Piloter la TV depuis le téléphone** (volume, allumage, applis Samsung) : l'API télécommande
  Samsung (`wss://TV:8002/api/v2/channels/samsung.remote.control`) ferme les connexions venant de la
  TV elle-même (localhost **et** IP réseau, diagnostic « closed_early »), et Safari refuse le certificat
  auto-signé. → l'appli **SmartThings** pour ça.
- **Démarrage automatique d'OpenTV à l'allumage** : impossible sur cette TV sans appareil externe.
  - La TV **redémarre à froid** à chaque allumage (uptime ~1-5 min) et tue toutes les applis, alors
    que « Instant On » et « Always Instant On Support » sont déjà sur ON dans le menu de service
    (photo prise par Alexandre). Restauration d'une image système propre → aucun processus ne survit.
  - `on-boot="true"` du service : ignoré.
  - Réglage « Autorun Last App » (activé, « Autorun Smart Hub » désactivé) : ne relance pas une
    appli installée en mode développeur ; la TV revient sur HDMI 1.
  - « Power On with Mobile » activé : aucun effet.
  - SmartThings (`custom.launchapp` → `launchApp`) : bloqué, la TV est sur le compte Samsung de
    quelqu'un d'autre, et en changer impose une réinitialisation (refusé).
  - Le lancement SDB par le **NanoPi existant** est maintenant autorisé ; aucun nouvel appareil.
    Le raccourci OpenTV épinglé dans Home reste le retour arrière.
- **TizenBrew** : avec Host PC IP ≠ `127.0.0.1`, TizenBrew ne peut plus lancer ses modules. OpenTV
  natif n'en a pas besoin. Pour revenir à TizenBrew : remettre `127.0.0.1`.
- **Menu de service** : ne rien y changer. Les options « Support » (Bluetooth, Local Dimming,
  HDR+, Ambient…) correspondent à du matériel absent sur ce modèle.

## 12. Historique

1. Parti du modèle libre « Nova TV » (dossier Nova-TV-Free sur le Mac).
2. v1-v12 : **module TizenBrew** (`alxbouchard/opentv@main` chargé par jsDelivr). Problèmes : cache
   jsDelivr sur `@main` (utiliser des SHA de commit pour tester), service TizenBrew gardé jusqu'au
   redémarrage de la TV, tentatives de relais télécommande abandonnées (§11).
3. v13-v20 : **appli native** avec sa propre icône (idée d'Alexandre : « se baser sur TizenBrew,
   qui est open source »). v14 : garde xhr, filtres. v15-v16 : remplacement du service après mise à
   jour, cache-buster, `0 debug`, `/status`. v17 : veille HDMI 1 en direct. v18 : rangée en bas.
   v19 : tentatives de démarrage auto. v20 : correctif du saut de la rangée (§5).

## 13. Manière de travailler avec Alexandre

- Français, direct, concis. Il corrige vite les hypothèses : vérifier avant d'affirmer.
- Toutes les mises à jour sur **`main`** du dépôt `alxbouchard/opentv`.
- Tester avant de livrer (suites ci-dessus + `/status` sur la TV), puis lui demander de confirmer à
  l'écran : il envoie des photos et vidéos de la TV, à analyser image par image.
- Ne jamais toucher au compte Samsung de la TV, ni au menu de service. Pas d'appareil externe.
- Ne jamais mettre la clé de signature ou son mot de passe dans Git (le dépôt est public).

## 14. Passation à une autre équipe : ce qui n'est PAS dans le dépôt

| Élément | Où | Comment le transmettre |
|---|---|---|
| Clé d'auteur `opentv-author.p12` + mot de passe | Mac d'Alexandre : `~/Downloads/Nova-TV-Free/opentv-signing/` | En privé (gestionnaire de mots de passe), **jamais dans Git**. Sans elle, impossible de mettre à jour l'OpenTV installé : il faudrait le désinstaller (préférences perdues) et réinstaller sous une nouvelle clé. |
| Accès à la TV en mode développeur | TV : Applis → `12345` → Host PC IP | La machine de l'équipe (ou le NanoPi) doit avoir l'IP réglée là, et être sur le même réseau local. |
| Accès au NanoPi | SSH `alex` par Tailscale (Hermes), Muse par le Gadget SDK | Décision d'Alexandre. |
| Outils | Tizen Studio CLI 5.6, Java 8 (JAXB), Python 3 + pexpect, Node, Playwright | Installation décrite dans `scripts/build.py` et §9. |

Tout le reste (code, icône, images, tests, scripts de build et de déploiement, historique) est dans le dépôt.
