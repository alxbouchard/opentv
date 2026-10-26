# opentv-bridge — appli externe « Films » sur HDMI 1 (côté NanoPi)

Implémente `native/NANOPI-API.md` (API v1) pour OpenTV v23 (tuiles « Films » et « Pour toi »). Python 3.13 standard,
utilisateur `alex`. « Pour toi » (recommandations locales, historique SQLite, CLI agent) : voir `FORYOU.md`.

## Architecture

```
TV 192.168.2.14 (OpenTV v22, tvwindow HDMI1 BEHIND)
   │  POST /v1/session|key|close, GET /v1/health   (clé d'appli + 6 touches, jamais d'URL)
   ▼
NanoPi 192.168.2.28:8097  opentv-bridge.service (systemd --user, KillMode=control-group, OOMScoreAdjust=600)
   ├─ Controller : session unique, état starting→catalogue→player→dashboard, curseur du catalogue
   ├─ Resolver   : ancre https://fakrov.com/ → hôte/préfixe/jeton courants (migration annoncée, cache 0600 v2)
   ├─ RouteMap   : site_id stable → b/<jeton>/<lien> de l'époque courante (routes.json 0600, borné)
   ├─ RoutingMonitor (optionnel, ROUTING_REFRESH_S) : revalidation d'adresse en arrière-plan
   ├─ Catalogue  : catalogue.json (métadonnées seules) ; rafraîchi depuis la page de liste découverte
   ├─ UI locale  : 127.0.0.1:8098/ui/ (Films) et /ui/pourtoi/ (Pour toi) : vues passives, CSP stricte, aucun HTML injecté
   ├─ opentv_foryou.py : « Pour toi » (foryou.sqlite3 0600 sous STATE_DIR, UID FORYOU_SOURCE_ID/site_id, CLI JSON locale)
   └─ ChromiumKiosk (enfants, pendant la session uniquement)
        weston --backend=drm --renderer=pixman --shell=kiosk (1920x1080, prend le maître DRM)
        chromium --kiosk --ozone-platform=wayland, profil temporaire, piloté par CDP sur tube (fd 3/4,
        aucun port de débogage) : Page.navigate, Input.dispatchKeyEvent, téléchargements refusés,
        fenêtres surgissantes refermées.
muse-display.service (dashlive.py → /dev/fb0) : JAMAIS arrêté/signalé/modifié. Pendant la session, le
compositeur détient le maître DRM et l'fbdev est masqué ; à la sortie du compositeur, l'émulation fbdev
du noyau restaure l'affichage du tableau de bord (à valider sur le terrain, voir Bloquants).
```

### Parcours

| Action TV | Pont | Écran HDMI 1 |
|---|---|---|
| OK sur la tuile Films | `POST /v1/session` → `starting` (réponse immédiate), lance weston+chromium en tâche de fond | tableau de bord → catalogue (~1 resynchro HDMI au changement de mode) |
| Flèches | curseur Python (grille 6 col.), poussé à la page par CDP | sélection |
| OK | URL = origine validée + `/<préfixe>/` + chemin exact (catalogue même époque, ou table site_id) → `Page.navigate` → `player` ; sinon « Page indisponible » | page publique du film, plein écran |
| (après OK, auto) | assistance de lecture bornée (voir ci-dessous) : lecteur unique agrandi sur la page parente + **un** clic central | lecteur plein écran, lecture lancée |
| Flèches/OK en lecteur | `Input.dispatchKeyEvent` vers la page | — |
| Retour (lecteur) | renavigue vers l'UI locale, curseur conservé → `catalogue` | catalogue |
| Retour (catalogue) | fin `back` → navigateur puis compositeur fermés | tableau de bord |
| Exit / Home / appli native / OpenTV en arrière-plan | `POST /v1/close` (OpenTV) | tableau de bord |

### Fins automatiques (chien de garde 1 s) → `dashboard`

`tv-lost` (aucune requête de la TV depuis `HEARTBEAT_S`=30 s ; OpenTV sonde toutes les 2 s), `start-timeout`
(45 s), `display-died` (navigateur/compositeur/CDP morts), `idle` (catalogue sans touche 300 s), `player-cap`
(14 400 s ; pas d'inactivité en lecture), `display-failed`, `return-failed`, `shutdown`. Panne du pont :
systemd tue le groupe (navigateur + compositeur) ; OpenTV sort après 3 échecs `/v1/health` (« NanoPi injoignable »).

### Écarts / précisions au contrat v1

- Limite de débit **20 req/s** par IP (contrat : 10) : OpenTV compte un 429 comme échec et ferme après 3 ;
  une touche maintenue dépasse 10/s. Option : garder 10 et faire ignorer le 429 par OpenTV.
- `503 display_unavailable` quand `DISPLAY_BACKEND=off` (défaut de l'installateur) : OpenTV le traite comme
  tout échec d'ouverture (message, accueil, pas d'HDMI).
- `close` avec un `session` périmé ne ferme pas la session courante (réponse `dashboard` quand même).
- Validation du préfixe : `/<préfixe>/` répond aujourd'hui **302 → `/<préfixe>/home/fakrov`** et
  `/<préfixe>` **301 → `http://`** même hôte. Le résolveur suit ≤3 redirections même origine **restant sous
  `/<préfixe>/`**, exige 200 `text/html`, et mémorise ce chemin comme page de liste.

## Assistance de lecture « un clic » (`PLAY_ASSIST`, défaut 1)

Constat terrain : la fiche publique validée charge **une grande iframe de lecteur d'une autre origine** ; un
`<video>` HTML5 n'est jamais visible depuis le document principal. Après le OK de l'utilisateur et la
navigation vers la fiche, `ChromiumKiosk.play_assist` (thread `play-assist`, une seule exécution par OK) :

1. sonde toutes les 0,4 s, au plus `PLAY_ASSIST_S` (15 s, ≤ 60), le **document principal seulement**, dans un
   monde isolé CDP (`Page.createIsolatedWorld`) : les scripts de la page ne voient pas nos scripts et ne peuvent
   pas falsifier `elementFromPoint`/`getBoundingClientRect` ; l'origine du document doit être exactement celle de
   l'URL validée, sinon `foreign` et aucune action ;
2. retient l'unique `<iframe>`/`<video>` visible dominant (≥ 480×270 et ≥ 15 % de la fenêtre ; si deux candidats
   ont moins d'un facteur 2 d'écart → `ambiguous`, aucune action ; les petites iframes publicitaires sont ignorées) ;
3. l'agrandit **sur la page parente** par style en ligne `!important` (`position:fixed; top/left/right/bottom:0;
   100vw×100vh; border:0; z-index:2147483647`) et `overflow:hidden` sur `html`/`body` — rien n'est retiré, masqué
   ni modifié dans l'iframe ;
4. attend `PLAY_ASSIST_SETTLE_S` (1,5 s), revérifie que le lecteur couvre la fenêtre **et** que
   `elementFromPoint(centre)` est bien ce lecteur (sinon `maximized-obstructed` : jamais de clic sur une surcouche),
   puis envoie **un seul** clic gauche réel (`Input.dispatchMouseEvent`) au centre. Pas de boucle, pas de nouvelle
   tentative : un OK = au plus un clic. Le focus passe à l'iframe, les flèches/OK suivants y sont relayés.

Jamais : lecture de `src`/`currentSrc`/`srcObject`, accès au contenu de l'iframe (`contentWindow`/`contentDocument`),
requête réseau, URL de flux, suppression de publicité, `muted`, contournement de protection. Les scripts sont des
constantes statiques (test `StaticScriptTests`). Retour (lecteur) annule l'assistance (événement + `nav_lock` : aucun
clic possible après la navigation de retour) et ramène le catalogue ; fermeture de session idem. Tout échec
(`no-player`, `ambiguous`, `foreign`, `error`…) laisse la page publique utilisable, avec les touches relayées comme
avant. Résultat journalisé : `{"event":"play_assist","outcome":…}`. `PLAY_ASSIST=0` = comportement précédent.

## Notes TMDb (`opentv_tmdb.py`, commande ponctuelle — jamais en arrière-plan)

Fournisseur **distinct** d'IMDb : champs `tmdb_*` uniquement (`tmdb_id`, `tmdb_rating` /10 = `vote_average`
arrondi, `tmdb_votes`, `tmdb_match_status` ∈ matched|ambiguous|unmatched, `tmdb_match_method`,
`tmdb_confidence`, `tmdb_reason`, `tmdb_evidence`, `tmdb_match_key`, `tmdb_rating_updated_at`) et
`attribution.tmdb`. L'UI montre le logo officiel d'attribution (`tmdb-logo.svg`, variante `blue_short` publiée sur
<https://www.themoviedb.org/about/logos-attribution>), un badge « TMDb x.x · n votes » séparé du badge IMDb et la mention
« This product uses the TMDB API but is not endorsed or certified by TMDB. » dès qu'un titre est apparié.

- **Correspondance prudente** : (0) `imdb_id` déjà connu → `/find` (unique, année ±1) ; (1) titre exact
  (casse/espaces/apostrophes, titre localisé ou original) + même année → `high`, ou année ±1 si candidat
  unique → `medium` ; (2) titre normalisé (accents, ponctuation, article initial) + même année → `medium` ;
  sinon `ambiguous` (plusieurs candidats plausibles, jamais de choix) ou `unmatched` (`no_year`,
  `year_mismatch`, `no_title_match`, `no_results`, `provider_gone`). 0 vote → note `null`, jamais 0. Le pont
  (`clean_item`) n'expose une note que pour `matched` avec votes > 0.
- **Coût borné** : `--max-requests` (400), ≤ 10 req/s, une reprise sur 429 (`Retry-After` ≤ 10 s), coupe-circuit
  après 5 erreurs transitoires consécutives (valeurs antérieures conservées), langues `fr-FR,fr-CA`
  (2e recherche seulement si la 1re ne donne rien). Réutilisation par `tmdb_match_key` (titre+année+imdb_id) ;
  notes rafraîchies après 7 j, non-appariés revérifiés après 30 j ; horodatages dans
  `catalogue.json.tmdb-state.json` pour que `catalogue.json` reste identique octet pour octet sans changement.
- **Écriture atomique + retour arrière** : verrou `catalogue.json.lock`, refus si le fichier a changé pendant la
  passe, refus si un champ non `tmdb_*` diffère ou si un champ tmdb est invalide, copie `catalogue.json.tmdb.bak`
  (0600), remplacement atomique, relecture (restauration automatique si elle diverge). Le pont relit le
  catalogue à chaque ouverture de session : aucun redémarrage nécessaire.
- **Secret** : environnement uniquement (`TMDB_READ_TOKEN` v4 de préférence, sinon `TMDB_API_KEY` v3), retiré de
  `os.environ` à la lecture, jamais en argument/journal/disque. 401/403 → arrêt, rien n'est écrit (code 3).
  API épinglée sur `https://api.themoviedb.org/3` (seul `http://127.0.0.1:<port>/3` est admis pour les tests).

Déploiement (depuis le Mac, après installation du pont ; trousseau → tube → NanoPi, sans écho) :

```sh
security find-generic-password -s com.paz.opentv.tmdb.read-access-token-v4 -a paz_alex -w \
  | ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh --dry-run'   # compteurs seulement, rien n'est écrit
security find-generic-password -s com.paz.opentv.tmdb.read-access-token-v4 -a paz_alex -w \
  | ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh'             # écrit catalogue.json (+ .tmdb.bak)
ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh status'           # matched/ambiguous/unmatched/rated, sans secret
ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh rollback'         # restaure la copie d'avant enrichissement
```

Codes de sortie : 0 ok, 2 configuration/verrou, 3 authentification TMDb, 5 validation (rien n'est écrit).

## Migration de domaine et routage (fakrov.com → wadrox.com)

Faits vérifiés (25 oct. 2026) : `https://fakrov.com/` répond 200, titre « fakrov devient wadrox.com », ancre
`https://wadrox.com/` ; `https://wadrox.com/` lie le préfixe `99c7f65` ; `/99c7f65/` 302 → `/99c7f65/home/wadrox` ;
fiches `b/wadrox/<lien>`. Le `data-id` interne est stable (Super Mario 17397) mais le lien change
(52277985 → 87837453).

- **Ancre de confiance** : `SITE_ROOT=https://fakrov.com/` reste la seule configuration. Une migration n'est
  adoptée que si la racine *actuellement de confiance* n'offre plus de préfixe valide **et** publie une ancre
  exacte `https://<hôte>/` dont le nom figure dans son texte. Le nouvel hôte doit être un nom DNS public
  (pas d'IP, de port, d'identifiants, de nom local, ni de `http`), sa racine ne doit pas rediriger hors de
  l'hôte, sa liste doit être `/<p>/home/<jeton>` avec jeton ∈ nom d'hôte et ≥3 fiches `/<p>/b/<jeton>/<n>`
  sans fiche d'un autre jeton. ≤2 sauts par vérification ; si l'hôte migré tombe, retour à l'ancre.
- **Cache** `prefix.json` v2 : `anchor, host, prefix, token, listing_path, validated_at, checked_at, history,
  via`. L'ancien format v1 est relu (hôte = ancre, jeton déduit). Un cache d'une autre ancre est ignoré.
  Époque inchangée : horodatages en mémoire, disque réécrit au plus toutes les `PREFIX_TTL_S/4` (≤1 h).
- **Déclencheurs** : (1) ouverture de session par l'utilisateur — revalidation si la dernière vérification a
  plus de `SESSION_CHECK_S` (300 s), en tâche de fond sans retarder la réponse ; (2) optionnel,
  `ROUTING_REFRESH_S` (0 par défaut, minimum 300, exige `SOURCE_APPROVED=1`) — thread supervisé du service :
  vol unique (aucun chevauchement, ni avec une ouverture), 3 GET bornés par vérification, repli ×2 jusqu'à
  ×8 après échec, dernière époque valide conservée, journaux `routing_check_ok|failed|skipped`.
  `CATALOGUE_REFRESH_S` reste 0 : ni catalogue ni IMDb ne sont recollectés.
- **Lecteur** : jamais de calcul d'identifiant. Élément de la même époque (hôte+jeton) → son chemin.
  Sinon → table `site_id → chemin` construite à partir de la page de liste puis des pages
  `api_films.php?offset=&limit=20&folder=<p>&pr=<jeton>` seulement tant que des site_id du catalogue
  manquent (≤`ROUTE_MAX_PAGES`=25, `hasMore=false` = conclusif). Liens revalidés (même hôte,
  `/<p>/b/<jeton>/<n>`), doublons/conflits écartés. Retiré, ambigu ou sans `site_id` → « Page indisponible ».
- **Catalogue hérité** : `bridge-catalogue.json` n'a que les anciens liens ; ajouter le `site_id` hors ligne
  depuis le magasin brut : `STATE_DIR=… python3 opentv_bridge.py backfill-site-ids current.json`
  (correspondance exacte `link_id`, même hôte, copie `.bak` 0600). Essai sur copie : 196/196 remplis.

## Banc d'essai GPU (`opentv_gpu_trial.py`, manuel, fail-closed)

Le profil de production reste **weston pixman + Chromium `--disable-gpu` 1920x1080** ; le banc ne modifie ni
`opentv-bridge.env`, ni le pont, ni `muse-display`. État au 25 oct. 2026 : **NO-GO** (noyau constructeur 4.19.232,
`/dev/mali0` Midgard propriétaire, `renderD128` = rockchip-drm, aucun panfrost). Il faudra un noyau mainline
avec panfrost (et sans kbase) avant tout essai.

```sh
install -m 0644 opentv_gpu_trial.py ~/opentv-bridge/       # pas dans install-bridge.sh : outil ponctuel
python3 -I ~/opentv-bridge/opentv_gpu_trial.py preflight    # JSON ; exit 0 GO, 1 NO-GO
python3 -I ~/opentv-bridge/opentv_gpu_trial.py run --dry-run          # contrôles + commande, rien n'est lancé
python3 -I ~/opentv-bridge/opentv_gpu_trial.py run --duration 60 --url chrome://gpu
```

- **`preflight`** (`--root` pour les tests, lecture seule) exige : noyau ≥ 5.15 ; aucun `/dev/mali*`,
  `/sys/class/misc/mali0`, module `mali*`/`*kbase*` ; un `renderD*` dont le pilote sysfs (lien `driver` **et**
  `uevent`) est `panfrost`, avec `/dev/dri/renderD*` caractère 226:N identique au sysfs et lisible/écrivable ;
  `panfrost_dri.so`, `weston` + `gl-renderer.so`, `chromium`, `systemd-run`, `/run/seatd.sock` ; une sonde EGL
  (sous-processus, plateforme *device* limitée à ce nœud de rendu, jamais le nœud primaire/maître DRM) dont
  `GL_RENDERER` contient `Panfrost` (llvmpipe/softpipe refusés). Sortie sans environnement ni secret.
- **`run`** : préflight GO obligatoire, puis refus (exit 3) si : root, `XDG_RUNTIME_DIR` douteux, verrou déjà
  pris, gestionnaire `systemctl --user` injoignable, `muse-display` pas `active`/PID mort, pont en transition ou
  en session (`127.0.0.1:8098/ui/state.json` ≠ `dashboard` ou illisible), tout processus
  `weston`/`chromium` existant, unité `opentv-gpu-trial-*` déjà chargée, env du pont illisible.
- Lancement : `systemd-run --user --unit=opentv-gpu-trial-<horodatage>.service --collect`, `Type=exec`,
  `RuntimeMaxSec=durée+30` (rollback indépendant même si la session SSH tombe), `KillMode=mixed`,
  `TimeoutStopSec=10` puis SIGKILL, `OOMScoreAdjust=600`, `Restart=no`. Dans l'unité : `weston --backend=drm
  --renderer=gl --shell=kiosk` (HDMI-A-1 1920x1080) puis Chromium Wayland kiosque **sans `--disable-gpu`**
  (`--ignore-gpu-blocklist`, profil temporaire, URL limitée à `chrome://gpu`, `about:blank` ou l'UI locale du
  pont). Durée 10–300 s. Arrêt : Chromium puis Weston (SIGTERM 5 s, puis SIGKILL).
- Surveillance chaque seconde : PID/`NRestarts`/état de `muse-display` modifiés → arrêt immédiat ; une session du
  pont qui démarre → arrêt. Nettoyage toujours (succès, échec, Ctrl-C) : `stop`, `kill SIGKILL` de l'unité,
  puis SIGKILL des seuls PID de son cgroup ; vérification qu'aucun weston/chromium ne reste.
- Journaux plafonnés dans `$XDG_RUNTIME_DIR/opentv-gpu-trial-<horodatage>/` (weston 256 Kio, chromium 512 Kio,
  3 essais conservés) + `session.json` (issue, codes, lignes `GL renderer`, erreurs du processus GPU).
- Codes : 0 essai complet **et** `GL renderer … Panfrost` dans weston sans erreur GPU Chromium ; 1 NO-GO ;
  2 usage ; 3 refus ; 4 essai échoué/interrompu mais nettoyé ; 5 **alerte** (restes, `muse-display` changé ou
  env modifié). Un code 0 ne prouve pas l'image : photo d'Alex exigée pendant l'essai **et** après le retour au
  tableau de bord.

## Tests (Mac, sans NanoPi ni TV)

```sh
python3 -m unittest -v native.test.test_opentv_bridge   # 69 tests, dont Chromium réel headless par CDP
python3 -m unittest -v native.test.test_opentv_play_assist   # 17 : assistance de lecture, 9 en Chromium réel (iframe d'une autre origine)
python3 -m unittest -v native.test.test_opentv_tmdb   # 27 : correspondance, budget, panne, atomicité, CLI/secret, UI
python3 -m unittest -v native.test.test_opentv_gpu_trial   # 68 : banc GPU, mocks seulement (aucun affichage)
python3 -m unittest -v native.test.test_opentv_foryou      # 70 : Pour toi (base, moteur, navigation, CLI, pont, contention SQLite, Chromium réel)
cd native/test && node bridge_e2e.js                     # 25 : vraie page OpenTV v23 ↔ vrai pont (Films + Pour toi)
```

## Déploiement contrôlé

1. **Prérequis root** (Alex) : `sudo apt install chromium weston seatd` ; `sudo systemctl enable --now seatd` ;
   `alex` est déjà dans `video`/`render`/`input`. Aucun autre service système touché.
2. `./install-bridge.sh` → service en `DISPLAY_BACKEND=off` ; contrôle lecture seule que `muse-display`
   (état, PID, redémarrages) est identique avant/après, sinon retour arrière automatique.
3. L'installation garde `SOURCE_APPROVED=0`, `CATALOGUE_REFRESH_S=0` et `ROUTING_REFRESH_S=0` : aucune collecte ni ouverture de
   source externe n'est alors possible, même si le backend d'affichage est changé par erreur. Ne mettre
   `SOURCE_APPROVED=1` qu'après confirmation humaine que la source et son mode d'accès sont autorisés.
4. Essai terrain manuel du compositeur **sans OpenTV**, TV allumée sur HDMI 1, photo à chaque étape :
   lancer weston+chromium 30 s, les arrêter ; exiger le tableau de bord intact (même PID `dashlive.py`).
5. Passer `DISPLAY_BACKEND=chromium`, redémarrer le service, puis parcours complet depuis la TV.
5 bis. Optionnel : enrichissement TMDb (`tmdb-enrich.sh --dry-run`, puis sans) ; `PLAY_ASSIST=1` (défaut) — essai
   terrain d'un film : lecteur plein écran + lecture après OK, Retour → catalogue, photo.
6. Livrer OpenTV v23 (build/signature/installation via le chemin NanoPi validé), `/status`, confirmation d'Alex.
   Le pont doit être mis à jour (`install-bridge.sh`) **avant** la WGT v23 : un ancien pont répond 404 `unknown_app`
   à la tuile « Pour toi » (message « indisponible », Films intact).

Retour arrière : `./rollback-bridge.sh` (ne retire que ce service).
