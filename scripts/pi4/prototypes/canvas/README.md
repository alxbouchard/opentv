# OpenTV Canvas — phase 1 (prototype, non installé)

Laboratoire visuel 1920×1080 et modèle de scène de référence pour **OpenTV Canvas**. Rien dans ce dossier n'est
copié par `install.sh`, référencé par le manifeste, importé par `opentv_pi/`, exposé à Muse ou à l'Agent Port, ni
déployé sur le Pi. La production (`scripts/pi4/ui/`, `opentv_pi/`, scripts d'installation) reste inchangée.

## Contenu

| Fichier | Rôle |
| --- | --- |
| `canvas_model.py` | Modèle typé stdlib : énumérations fermées, hachage canonique, révision, aperçu expirant (horloge monotone), confirmation `confirm=True` littérale, historique de 5, retour arrière/défaut, persistance atomique fail-closed. |
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
- Un aperçu est refusé pendant un panneau, la lecture, la configuration, hors-ligne, ou dans les 1,5 s qui suivent une
  touche (mémoire spatiale) ; changer l'ordre des rangées conserve la rangée et le titre au focus.

## Lancer

```sh
cd scripts/pi4/prototypes/canvas && python3 -m http.server 8765 --bind 127.0.0.1
# puis ouvrir http://127.0.0.1:8765/lab.html?variant=mist|cinema|canvas
```

Les commandes du laboratoire (variante, aperçu 10 s, garder/annuler, historique, états) sont sous le cadre TV.

## Tests

```sh
cd scripts/pi4
python3 -m unittest tests.test_canvas_prototype          # aussi sous python3.11
OPENTV_TEST_PLAYWRIGHT=<module playwright> OPENTV_TEST_ACORN=<module acorn> \
OPENTV_CANVAS_SHOTS=../../.artifacts/opentv-canvas python3 -m unittest tests.test_canvas_prototype
```

Le scénario réel (`tests/e2e_canvas_prototype.js`) sert seulement les trois fichiers du laboratoire, bloque toute
autre requête et capture `mist.png`, `cinema.png`, `canvas.png` (non suivis par Git).

## Limites assumées (phase 1)

- Le catalogue, le QR (motif non scannable) et la « lecture » sont simulés ; le laboratoire ne contient aucun lecteur.
- L'historique du laboratoire est en mémoire ; seule la référence Python persiste sur disque.
- Aucun chemin agent : pas de commande Muse, pas de portée Agent Port, pas d'écriture depuis le réseau.
- Validé en Chromium headless seulement : ni Pi, ni HDMI, ni TV réelle (CPU/température/cadence restent des portes
  physiques de la phase suivante).
