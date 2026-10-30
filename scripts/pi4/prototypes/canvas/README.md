# OpenTV Canvas — phase 1 (prototype, non installé)

Laboratoire visuel 1920×1080 et modèle de scène de référence pour **OpenTV Canvas**. Rien dans ce dossier n'est
copié par `install.sh`, référencé par le manifeste, importé par `opentv_pi/`, exposé à Muse ou à l'Agent Port, ni
déployé sur le Pi. La production (`scripts/pi4/ui/`, `opentv_pi/`, scripts d'installation) reste inchangée.

## Contenu

| Fichier | Rôle |
| --- | --- |
| `canvas_model.py` | Modèle typé stdlib : énumérations fermées, hachage canonique, révision, aperçu expirant (horloge monotone), événements d'interface typés (`ui_event`), confirmation `confirm=True` littérale, historique de 5, retour arrière/défaut, persistance atomique fail-closed. |
| `lab.html`, `lab.css`, `canvas.js` | Coque OpenTV fixe (ES5) et trois variantes exécutables. |

## Variantes

- **`mist` — Brume vivante** : la côte brumeuse v23 en procédural, trois bancs de brume et un rai de lumière lents.
- **`cinema` — Cinéma** : l'œuvre du titre au focus occupe la scène (Ken Burns lent), titre affiche, voile profond.
- **`canvas` — recommandé** : hybride. Aurore teintée par l'accent, brume héritée de `mist`, halo de l'œuvre au focus
  hérité de `cinema`, verre plus profond, raison explicable de la proposition et pastille de scène. Défaut immuable.

Toutes conservent la géométrie v23 : horloge/date en haut à gauche, Réglages 72 px en haut à droite, héros, rangées
(↑ remonte d'une rangée, ↑ depuis la première mène aux Réglages), colonne QR fixe de 300 px (QR 232 px), pied de page.

## Scène (données, jamais du code)

`schema_version` (1), `variant`, `wallpaper` (`brume|affiche|aurore`), `motion` (`full|reduced|off`), `accent`
(`ice|ember|jade|violet`), `shade` (`soft|balanced|deep`), `hero_focus` (`title|artwork|calm`) et `row_order`
(permutation partielle de `tonight|new|top|watchlist`, `tonight` obligatoire). Clé inconnue, valeur hors liste, URL,
CSS, balise ou chemin ⇒ refus, scène courante inchangée. Seules des classes CSS issues de ces listes sont appliquées.

## Mouvement et sobriété

- Animations continues : `transform`/`opacity` seulement, au plus quatre couches `.anim` ; aucune image, vidéo,
  police, worker, filtre, `backdrop-filter` ni flou ; aucune ressource externe (CSP `default-src 'none'`).
- Figées pendant panneau, feuille Réglages, lecture/chargement, configuration, hors-ligne, page cachée et
  performance dégradée (cadence d'images mesurée) ; supprimées en `reduced`/`off` et sous
  `prefers-reduced-motion`.
- Un aperçu est refusé pendant un panneau, la lecture/le chargement, la configuration, hors-ligne, ou dans les 1,5 s
  qui suivent une touche (mémoire spatiale) ; changer l'ordre des rangées conserve la rangée et le titre au focus.
- Dégradation : le détecteur réel de cadence (`requestAnimationFrame`) fige le fond, définitivement pour la session,
  quand ≥ 45 des 60 à 90 derniers intervalles dépassent 70 ms. Le bout en bout le pilote de façon déterministe avec
  l'horloge Playwright (16 ms ⇒ animé ; 100 ms ⇒ `degraded`). Les seuils réels (CPU, température, `vcgencmd
  get_throttled`, cadence HDMI) restent une porte terrain Pi différée : mesurer sur le Pi/TV avant toute promotion.

## Cycle de vie : annuler-sur-humain/occupé

- **Temps** : échéances d'aperçu et fenêtre de silence sur horloge monotone (`performance.now()` dans Chromium 56,
  repli monotone borné sinon ; `time.monotonic` côté Python). L'heure murale ne sert qu'à l'affichage : un saut
  d'heure système de ±1 jour ne prolonge ni ne tue un aperçu et ne verrouille jamais la navigation.
- **La main humaine gagne** : toute touche de navigation (flèches, OK, Retour) et toute transition fiche, Réglages,
  lecture, chargement, configuration ou hors-ligne annulent l'aperçu vivant **avant** la transition. La scène publiée
  revient, focus ré-ancré par identifiant de rangée et de titre ; si le titre au focus a changé (rangée absente de la
  scène publiée), la touche annule seulement et n'agit pas — OK n'ouvre jamais un titre que l'humain n'a pas vu.
- Une échéance ne reconstruit jamais les rangées sous un panneau, une lecture, un chargement, la configuration ou le
  hors-ligne : l'ordre en attente s'applique à la fin de l'état. Un aperçu terminé sans touche rend le focus d'avant.
- La confirmation est refusée (`ui_busy`) tant que l'interface est occupée, y compris pendant la fenêtre de silence.
- Python : `CanvasStore.ui_event(event)` avec `event ∈ {key, panel, playback, loading, setup, offline, idle}` (toute
  autre valeur ⇒ `invalid_event`) annule l'aperçu et suit l'état occupé ; `idle` termine l'état. `confirm` revérifie
  l'échéance après verrou + relecture, avant de publier.
- Entrées fail-closed : identifiant d'aperçu = 32 hex minuscules, hachages = 64 hex minuscules, vérifiés avant
  `compare_digest` (`confirm("é", True)` ⇒ `unknown_preview`) ; `at` borné (0 … an 9999) sans conversion flottante ;
  toute `TypeError`/`ValueError`/`OverflowError` de lecture ⇒ magasin `corrupt`, défaut affiché.

## API du laboratoire (`window.OTVCanvas`, objets gelés)

| Espace | Fonctions | Qui |
| --- | --- | --- |
| `agent` | `preview(patch, ms)`, `confirm(true)`, `cancel()` | chemin agent : seul un aperçu temporaire ; publier exige `confirm(true)` littéral (consentement du propriétaire relayé) hors état occupé |
| `owner` | `select(name)`, `patch(patch)`, `undo()`, `reset()` | **propriétaire seulement** (Réglages, commandes du laboratoire) ; jamais exposé à un agent |
| `lab` | `setState(flag, on)`, `snapshot()`, `validate(scene)`, `presets()` | simulation et lecture seule |

Côté Python, le chemin agent est `preview` → `confirm(preview_id, True)` ; `rollback` et `reset_default` sont
réservés au propriétaire. Aucune fonction agent ne publie sans aperçu + confirmation.

Requête du laboratoire : seuls `variant` et `motion` sont lus ; un encodage invalide (`%E0%A4%A`, `%`) donne
`bootError = "invalid_query"`, la scène par défaut et un laboratoire utilisable.

## Lancer

```sh
cd scripts/pi4/prototypes/canvas && python3 -m http.server 8765 --bind 127.0.0.1
# puis ouvrir http://127.0.0.1:8765/lab.html?variant=mist|cinema|canvas
```

Les commandes du laboratoire (variante, aperçu 10 s, garder/annuler, historique, états) sont sous le cadre TV ; ce
sont des commandes **propriétaire**.

## Tests

```sh
cd scripts/pi4
python3 -m unittest tests.test_canvas_prototype          # aussi sous python3.11
OPENTV_TEST_PLAYWRIGHT=<module playwright> OPENTV_TEST_ACORN=<module acorn> \
OPENTV_CANVAS_SHOTS=../../.artifacts/opentv-canvas python3 -m unittest tests.test_canvas_prototype
```

Le scénario réel (`tests/e2e_canvas_prototype.js`) sert seulement les trois fichiers du laboratoire, bloque toute
autre requête et capture `mist.png`, `cinema.png`, `canvas.png` (non suivis par Git). Avec `OPENTV_TEST_ACORN`, il
analyse aussi `canvas.js` en ES5 et le test exige `acorn: "ok"` (en plus du test d'analyse séparé).

Non-impact (toujours exécuté) : `install.sh --dry-run` ne copie ni ne manifeste aucun fichier du prototype, aucun
fichier de production ne nomme ni n'importe `canvas_model`, et importer tout `opentv_pi` ne charge aucun module
Canvas. Comparaison à un commit de référence : revue seulement,
`OPENTV_CANVAS_BASELINE=<commit> python3 -m unittest tests.test_canvas_prototype`.

## Limites assumées (phase 1)

- Le catalogue, le QR (motif non scannable) et la « lecture » sont simulés ; le laboratoire ne contient aucun lecteur.
- L'historique du laboratoire est en mémoire ; seule la référence Python persiste sur disque.
- Aucun chemin agent : pas de commande Muse, pas de portée Agent Port, pas d'écriture depuis le réseau.
- Validé en Chromium headless seulement : ni Pi, ni HDMI, ni TV réelle (CPU/température/cadence restent des portes
  physiques de la phase suivante).
