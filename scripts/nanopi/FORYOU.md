# « Pour toi » — recommandations locales, historique et rétroaction (NanoPi)

Tuile OpenTV v23 « Pour toi » (appli externe NanoPi, clé `pourtoi`) : même pont, même session, même kiosque et
même lecteur que « Films » ; seules la vue locale et la navigation changent. Module dédié
`opentv_foryou.py` (Python standard), vue `opentv_foryou_ui.html`. Plan : `docs/plans/2026-10-25-pour-toi.md`.

## Architecture

```
OpenTV (tuile ext-pourtoi) ── POST /v1/session?app=pourtoi, /v1/key (6 touches), /v1/close, /v1/health
        │                       (contrat API v1 inchangé : aucune URL, mêmes états)
        ▼
opentv-bridge (opentv_bridge.py, crochets fins)
   ├─ Controller.open("pourtoi") : charge le catalogue accepté, calcule une sélection FIGÉE pour la séance
   ├─ Controller.key : Nav (rangées + panneau d'actions) ; « Regarder » = _open_item de Films (routage
   │   site_id, assistance de lecture, Retour → vue Pour toi au même endroit)
   ├─ UI locale 127.0.0.1:8098 : /ui/pourtoi/ (vue), /ui/foryou.json (rangées), /ui/state.json (curseur, marques)
   └─ opentv_foryou.py (chargé par chemin explicite, compatible python3 -I)
        ├─ Store   : STATE_DIR/foryou.sqlite3 (0600, dossier 0700)
        ├─ recommend() : fonction pure, horloge injectée, tri déterministe + explications
        ├─ Nav     : curseur rangée/colonne + panneau, sans effet de bord
        └─ CLI JSON : status | recommend | search | feedback | history (Muse/Hermit, local uniquement)
```

Aucun nouveau port, `ALLOW_IPS` inchangé, `CATALOGUE_REFRESH_S=0` inchangé, aucune requête réseau
supplémentaire. Le moteur ne voit que les métadonnées du catalogue accepté (`catalogue.json`, filtré par
`clean_item`) ; aucune URL de fiche, d'affiche ou de flux n'apparaît dans la CLI. La vue locale reçoit
l'affiche https (vignette `w300`) comme la vue Films, jamais le chemin de fiche.

## Données (SQLite, schéma 1, `PRAGMA user_version`)

| Table | Contenu |
|---|---|
| `prefs` | par film : `opinion` (`like` \| `dislike`, exclusifs), `seen`, `watchlist`, `snooze_until` (« Pas ce soir », 12 h), titre/genre au moment de l'action |
| `events` | journal des **changements** (une action déjà appliquée n'ajoute rien) : horodatage, film, action, origine `tv`/`cli` |
| `signals` | signaux de séance `opened` (film ouvert depuis Films ou Pour toi). **Jamais** interprété comme « vu » ni comme goût |
| `snapshots`, `catalogue_seen` | empreinte de chaque catalogue accepté et date de première apparition de chaque film ; le 1er catalogue **non vide** accepté est la référence (jamais « nouveau ») ; un catalogue vide n'est jamais enregistré |

- Identifiant stable : `<FORYOU_SOURCE_ID>/<site_id>`, ex. `films/17397`. `FORYOU_SOURCE_ID` (défaut `films`,
  écrit explicitement par `install-bridge.sh`) est un espace de noms **logique** validé strictement
  (`[a-z][a-z0-9-]{1,23}`, aucun point : jamais un domaine) ; il ne dépend ni de `SITE_ROOT`, ni du domaine, du
  préfixe, du jeton ou des routes. Changer l'ancre (fakrov → wadrox) ou subir une migration de domaine garde
  donc les mêmes identifiants : historique et rétroaction restent `in_catalogue=true`. Ne changer
  `FORYOU_SOURCE_ID` que pour un catalogue réellement différent (les anciens identifiants deviendraient
  orphelins). Valeur invalide : la tuile « Pour toi » est refusée (404 `unknown_app`, journal
  `foryou_config_invalid`), Films intact ; la CLI répond `{"ok":false,"error":"config"}` (code 2).
  Un élément sans `site_id` n'est ni recommandé ni annotable.
- Actions (idempotentes, valeur explicite) : `like`, `dislike`, `clear-opinion`, `seen`, `unseen`,
  `watchlist`, `unwatchlist`, `not-tonight`, `clear-not-tonight`.
- Bornes : 5000 événements et 5000 signaux (les plus anciens sont élagués), 50 instantanés + la référence.
- Base absente/illisible/corrompue/schéma plus récent/verrouillée : la tuile fonctionne en profil vide, les
  actions affichent « Action non enregistrée : historique indisponible. », journal `foryou_store_error`.
- Contention (sauvegarde, CLI, verrou externe RESERVED/EXCLUSIVE) : une seule attente, `BUSY_TIMEOUT_MS`
  (200 ms, réessais Python de 10 ms ; gestionnaire interne de SQLite désactivé car son sommeil peut s'arrondir
  à ~1 s). Côté pont, un disjoncteur monotone (`STORE_COOLDOWN_S`, 5 s) suit toute erreur : profil vide et
  aucune requête SQLite pendant le refroidissement, **un seul** `foryou_store_error` par panne, puis
  `foryou_store_recovered`. Aucun appel SQLite n'a lieu sous le verrou global du contrôleur ; l'instantané
  Films et le signal « ouvert » passent par une file bornée (32, un seul thread qui s'arrête au repos) :
  `/v1/session`, `/v1/key`, `/v1/close` et `/v1/health` restent < 1 s même base verrouillée (testé).
  Une ouverture abandonnée par la TV (`/v1/close?app=…` pendant la préparation) ne crée aucune séance.
- Données personnelles : uniquement sur le NanoPi, jamais dans Git ni dans les journaux (le journal du pont
  ne contient que `foryou_feedback {action, changed}`).

## Moteur (déterministe, explicable)

1. Candidats : catalogue accepté avec `site_id`, sans « vu », sans « Pas pour moi », sans « Pas ce soir »
   actif, sans `site_id` dont la table de routage prouve l'absence.
2. Tri lexicographique : **goût appris** (poids de genre : +1 J'aime, −1 Pas pour moi, +0,5 À voir plus
   tard ; « vu » seul est neutre) → **qualité** (palier de confiance par votes — IMDb ≥ 10 000 / ≥ 1 000 /
   > 0, TMDb apparié ≥ 1 000 / ≥ 100 / > 0 — puis note, puis votes ; IMDb sinon TMDb, jamais de moyenne) →
   **nouveauté** (ajout détecté par instantanés < 30 j, sinon année de sortie récente) → titre → identifiant.
   Goût d'un film = somme des poids de ses genres (≤ 4, comparés sans casse ni accents). Le goût n'est appris
   **que** des marques explicites : ouvrir/regarder un film, parcourir les rangées ou le temps écoulé ne le
   modifient jamais. Ex. : 3 « Pas pour moi » d'animation ⇒ `Animation −3`, et tout autre film d'animation
   passe sous les genres neutres ou aimés.
3. **Diversité** : pas deux genres principaux identiques d'affilée si une alternative de même goût existe
   dans les 6 suivants.
4. Rangées (≤ 12, chaque film une seule fois) : « Le choix de ce soir » (meilleur de la liste « À voir plus
   tard » s'il y en a, sinon tête du classement), « À voir plus tard », « Parce que tu as aimé « X » » (si un
   J'aime avec genre existe), « Nouveautés disponibles », « Les mieux notés que tu n'as pas vus » (palier ≥ 2),
   « Autres suggestions ».
   Chaque rangée automatique choisit ses **membres** selon son critère, mais les **ordonne toujours** par goût
   appris d'abord, puis par son critère propre, puis par la clé globale : Nouveautés = goût → date d'ajout
   (sinon goût → qualité → année), Mieux notés = goût → note → votes, Parce que tu as aimé = goût → qualité
   (uniquement des films partageant un genre avec X **et** de goût net > 0, pour que l'explication reste
   vraie), Autres = classement global. Un genre rejeté ne passe donc jamais devant un genre neutre ou aimé.
   Seule exception volontaire : « À voir plus tard » est un choix explicite film par film — ses films restent
   proposés (ordre d'ajout) et « Le choix de ce soir » prend le mieux classé (par goût) de cette liste.
   « Pas pour moi » exclut ce film précis de toutes les rangées et pèse −1 sur chacun de ses genres.
5. Explications : valeurs brutes par fournisseur (`IMDb 8,2/10 · 584 479 votes`, `TMDb 7,9/10 · 4 200 votes`),
   `Parce que tu as aimé « X »`, `Dans ta liste « À voir plus tard »`, `Ajouté au catalogue le JJ/MM/AAAA` ou
   `Sorti en AAAA`. Profil vide : note honnête « Ton profil est encore vide : sélection d'après les notes IMDb
   ou TMDb (avec leur nombre de votes) et les nouveautés… ». Aucun score inventé n'est exposé.

## Télécommande (vue `/ui/pourtoi/`)

| Contexte | Touche | Effet |
|---|---|---|
| rangées | ◀ ▶ ▲ ▼ | film / rangée (colonne mémorisée par rangée) |
| rangées | OK | ouvre le panneau d'actions, **Regarder** présélectionné |
| rangées | Retour | fin de séance → tableau de bord |
| panneau | ◀ ▶ | Regarder · J'aime · Pas pour moi · Déjà vu · À voir plus tard · Pas ce soir |
| panneau | OK | Regarder → page publique (lecteur Films) ; autres boutons = bascules (rappuyer annule) |
| panneau | Retour | ferme le panneau |
| lecteur | Retour | revient à la vue Pour toi, même position |

« Pas pour moi » (préférence durable, exclut le film et pèse −1 sur chacun de ses genres dans toutes les
rangées automatiques) est distinct de « Pas ce soir »
(mis de côté 12 h, aucun effet sur le goût). Pendant une séance, rien ne disparaît : le film est grisé et
marqué (♥ ✕ ✓ + ☾) ; la sélection est recalculée à la séance suivante. Ouvrir un film ne le marque jamais vu.

## CLI agent (Muse / Hermit)

Locale, sans réseau, JSON trié sur une ligne (stdout), codes de sortie : 0 ok, 2 usage
(`{"ok":false,"error":"usage"}`), 3 base indisponible, 4 identifiant inconnu du catalogue. Les retraits
(`clear-opinion`, `unseen`, `unwatchlist`, `clear-not-tonight`) restent permis sur un identifiant déjà annoté
qui a quitté le catalogue ; toute écriture positive exige un film du catalogue accepté.

```sh
P='python3 -I ~/opentv-bridge/opentv_foryou.py'
$P status                                           # schéma, base, catalogue, compteurs du profil
$P recommend --limit 10 [--offset 0]                # classement paginé (1–50) + « tonight »
$P recommend --rows [--limit 12]                    # rangées telles que vues à la TV
$P search --query "toy story" [--limit 10] [--offset 0]   # 1–80 caractères, insensible aux accents
$P feedback --id films/17397 --action like          # like|dislike|clear-opinion|seen|unseen|watchlist|unwatchlist|not-tonight|clear-not-tonight
$P history [--list events|opened|liked|disliked|seen|watchlist|not-tonight] [--limit 20] [--offset 0]
```

- Chemins : `STATE_DIR` (défaut `~/.local/state/opentv-bridge`, comme le service), `CATALOGUE_FILE`,
  `FORYOU_DB`, `FORYOU_SOURCE_ID` ; à défaut d'environnement, ces clés sont lues dans
  `~/.config/systemd/user/opentv-bridge.env` (lecture seule, aucune exécution ; guillemets systemd `KEY="v"`
  ou `KEY='v'` retirés). `SITE_ROOT` n'est pas lu.
- **Aucune URL** dans les sorties : toute valeur textuelle (titre, requête, raisons, note, titres de rangée,
  détail d'erreur, objets imbriqués, titres déjà stockés) passe par un nettoyage global (`scrub`) qui retire
  `schéma://…`, `www.…`, `//hôte…`, `magnet:…`, `data:…`, `javascript:…` (et `vbscript:`/`file:`/`blob:`) et
  l'hôte nu `domaine.tld/chemin` ou `a.b.c.d/chemin` ; « Mission // Impossible », « 2/3 », « Big Data: le
  film » restent intacts. Un titre normal est rendu à l'identique, un titre réduit à une URL
  devient `""`. Une requête `search` qui ressemble à une URL est refusée (code 2) sans être réaffichée.
  Ni chemin de fiche, ni affiche ; résumés tronqués (300). `recommend`/`status` enregistrent seulement
  l'instantané du catalogue (idempotent). La vue locale 127.0.0.1 garde uniquement l'affiche https.
- Muse via `system.run` : exécuter la commande ci-dessus sous l'utilisateur `alex` ; Hermes via SSH idem.
  Ne jamais passer d'URL ni de commande en argument : les entrées sont typées et refusées sinon.

## Installation / mise à jour

`./install-bridge.sh` (inchangé dans son principe) compile et copie aussi `opentv_foryou.py` et
`opentv_foryou_ui.html` (0644, `.prev` conservé). Aucun paquet hôte. La base est créée au premier usage
(tuile ou CLI) sous `STATE_DIR`. `APPS` vaut désormais `catalogue,pourtoi` par défaut (un fichier env
existant sans `APPS` hérite du défaut ; `APPS=catalogue` désactive la tuile côté pont). Retour arrière :
`./rollback-bridge.sh` (la base est conservée ; la supprimer à la main efface l'historique).

## Tests

```sh
python3 -m unittest -v native.test.test_opentv_foryou   # 80 : schéma, idempotence, moteur, Nav, CLI (aucune URL),
                                                        #      identité stable fakrov→wadrox, pont, contention
                                                        #      SQLite (RESERVED/EXCLUSIVE/corrompue), Chromium réel
cd native/test && node ext.js && node bridge_e2e.js      # tuile Pour toi ↔ faux NanoPi / vrai pont
```

Limites : la nouveauté n'existe qu'à partir du 2e catalogue non vide accepté (sinon repli sur l'année de sortie) ;
les genres viennent du catalogue (25 % sans genre aujourd'hui) ; le goût est appris par genre seulement.
