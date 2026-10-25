# opentv-bridge — appli externe « Films » sur HDMI 1 (côté NanoPi)

Implémente `native/NANOPI-API.md` (API v1) pour OpenTV v21. **Non déployé.** Python 3.13 standard, utilisateur `alex`.

## Architecture

```
TV 192.168.2.14 (OpenTV v21, tvwindow HDMI1 BEHIND)
   │  POST /v1/session|key|close, GET /v1/health   (clé d'appli + 6 touches, jamais d'URL)
   ▼
NanoPi 192.168.2.28:8097  opentv-bridge.service (systemd --user, KillMode=control-group, OOMScoreAdjust=600)
   ├─ Controller : session unique, état starting→catalogue→player→dashboard, curseur du catalogue
   ├─ Resolver   : ancre https://fakrov.com/ → hôte/préfixe/jeton courants (migration annoncée, cache 0600 v2)
   ├─ RouteMap   : site_id stable → b/<jeton>/<lien> de l'époque courante (routes.json 0600, borné)
   ├─ RoutingMonitor (optionnel, ROUTING_REFRESH_S) : revalidation d'adresse en arrière-plan
   ├─ Catalogue  : catalogue.json (métadonnées seules) ; rafraîchi depuis la page de liste découverte
   ├─ UI locale  : 127.0.0.1:8098/ui/ (vue passive, CSP stricte, aucun HTML injecté)
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

## Tests (Mac, sans NanoPi ni TV)

```sh
python3 -m unittest -v native.test.test_opentv_bridge   # 67 tests, dont Chromium réel headless par CDP
cd native/test && node bridge_e2e.js                     # 17 : vraie page OpenTV v21 ↔ vrai pont (sous-processus)
```

## Déploiement (NON fait — exige l'accord d'Alex)

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
6. Livrer OpenTV v21 (build/signature/installation via le chemin NanoPi validé), `/status`, confirmation d'Alex.

Retour arrière : `./rollback-bridge.sh` (ne retire que ce service).
