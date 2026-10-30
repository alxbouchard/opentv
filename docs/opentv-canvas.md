# OpenTV Canvas — passation d'architecture (Raspberry Pi 4, 0.7.0)

Document de reprise pour les agents de développement. Mode d'emploi utilisateur, budget et contrôles terrain :
`scripts/pi4/README.md` §6 quinquies et §9. Plans : `docs/plans/2026-10-30-opentv-canvas-phase1.md` (prototypes) et
`docs/plans/2026-10-30-opentv-canvas-phase2.md` (intégration produit, tâches 1–6).

État à la livraison 0.7.0 : tâches 1 à 5 implémentées et vérifiées **hors matériel** (Python 3.11 et 3.13,
Playwright/Acorn réels, racines d'installation temporaires). Tâche 6 (revues, push, installation sur la Pi,
canari de performance, nouvelle promotion de « Regarder », photo d'Alex) reste à faire : rien n'est déployé.

## 1. Frontière produit

- **Coque immuable** : configuration QR, Réglages, feuilles de confirmation, navigation et mémoire spatiale, QR de
  télécommande, lecteur « Regarder » et son retour, hors-ligne, CEC, permissions, secrets, récupération.
- **Couche Canvas** : fond animé, accent, voile, halo du héros, ordre des rangées « Pour toi ». Une scène est une
  donnée validée par OpenTV, jamais du code : aucun HTML/CSS/JS, URL, chemin, commande ni texte libre.

## 2. Schéma fermé v1 (`opentv_pi/canvas_schema.py`, miroir exact dans `ui/canvas.js`)

| Champ | Valeurs |
|---|---|
| `schema_version` | `1` |
| `wallpaper` | `brume`, `affiche`, `aurore` |
| `motion` | `off`, `calm`, `alive` |
| `accent` | `ice`, `ember`, `jade`, `violet` |
| `shade` | `soft`, `balanced`, `deep` |
| `hero_focus` | `title`, `artwork`, `calm` |
| `row_order` | permutation de `tonight`, `watchlist`, `new`, `top`, `more` ; `tonight` toujours en premier |

Préréglages : `canvas` (défaut : `aurore`/`calm`/`ice`/`balanced`/`title`), `mist` (`brume`), `cinema`
(`affiche`/`ember`/`deep`/`artwork`, rangées `tonight, top, new, watchlist, more`). Clé inconnue, valeur hors
énumération, doublon, type inattendu ⇒ refus (`invalid_scene`) et état courant intact octet pour octet. Document
persistant : `epoch`, `history` (5 entrées max : `agent`, `at`, `hash`, `revision`, `scene`), `revision`
(≤ 2^53−1), `schema_version`, `state_hash` (SHA-256 canonique).

## 3. Modules et frontières

| Fichier | Rôle | Interdit |
|---|---|---|
| `opentv_pi/canvas.py` | façade publique (`CanvasStore`, `store_for`, `__all__`, ≤ 450 lignes) | logique d'E/S ou de rendu |
| `opentv_pi/canvas_schema.py` | schéma, préréglages, hachage, `CanvasError` (codes fermés, sans `assert`) | E/S |
| `opentv_pi/canvas_storage.py` | dossier, verrou, publication « échouer et restaurer » (`restore_failed`) | chown/chmod par chemin |
| `opentv_pi/canvas_activity.py` | raisons d'occupation imbriquées (jetons) + fenêtre humaine `NAV_QUIET_S` | horloge murale |
| `opentv_pi/canvas_roles.py` | façades Agent (aperçu, annulation) / Propriétaire (confirmer, appliquer, annuler, réinitialiser) | publication par un agent |
| `opentv_pi/canvas_tv.py` | vue TV épurée, feuille, routes `/api/tv/canvas/*`, occupation service/page, audit | chemins, identités brutes, raisons internes dans la vue |
| `opentv_pi/canvas_tools.py` | permission facultative `canvas`, outils MCP, codes stables, délai après refus | confirmation, scène brute, `row_order` |
| `ui/canvas.js` | peinture seulement : classes `cv-*` fermées, 4 couches, pause/dégradé (sonde de cadence par rafales, 12 s)/mouvement réduit | réseau, minuteurs d'API, boucle rAF permanente, texte du service |
| `ui/tv.js` | cycle de vie : état, feuille, routes bornées, adoption causale, Réglages « Ambiance OpenTV » | styles venus d'une scène |

Ne jamais nommer un module de production `canvas_model` (marqueur du prototype `scripts/pi4/prototypes/canvas/`, jamais
installé). Ajouter tout nouveau module Canvas à `PRODUCTION_CANVAS_MODULES` (`tests/test_canvas_prototype.py`) et à
`promotion.REQUIRED`.

## 4. Routes TV (loopback, clé de page, UID du kiosque, Host, JSON)

`POST /api/tv/canvas/event` (touche humaine ordinaire : annule l'aperçu), `/api/tv/canvas/ui` (ensemble complet des
raisons de la page `{busy, page, seq}`, bail 9 s), `/api/tv/canvas/confirm` (`{preview_id, confirm:true}` = « Oui »),
`/api/tv/canvas/cancel` (« Non »), `/api/tv/canvas/rollback` et `/api/tv/canvas/reset` (`{confirm:true}`),
`/api/tv/canvas/apply` (`{changes:{preset|wallpaper|motion|accent|shade|hero_focus}, confirm:true}`). Réponse
`{ok, error, outcome: applied|unchanged|see_state, result, canvas}` ; `restore_failed` ⇒ 503 `see_state` et état relu,
jamais « inchangé ». `GET /api/tv/state` porte `canvas` (statut `ok|corrupt|unavailable`, scène, révision, empreinte,
aperçu, feuille) sans chemin ni raison d'occupation.

## 5. Agents (Agent Port / MCP)

- Permission **facultative** `canvas` (`validate.OPTIONAL_SCOPES`), jamais un niveau emboîté ; hors `AGENT_LEVELS`,
  `MUSE_SCOPES`, `LINKED_REQUIRED`. Toute réécriture de politique passe par `agents.level_scopes` /
  `agents.with_optional`. Configuration QR : `set_policy(..., preserve=OPTIONAL_SCOPES)` garde la permission lors
  d'un simple changement de Wi-Fi ou de niveau, la retire si « Aucun accès » est choisi explicitement (niveau
  enregistré non vide → vide), et garde une ligne « ambiance seulement ».
- Outils (liste recalculée par session selon `whoami`, portée vérifiée à chaque appel avant tout débit) :
  `canvas_status`, `canvas_preview` (préréglage + valeurs énumérées), `canvas_cancel` (son propre aperçu, 32 hex).
  Débits `canvas_preview` (1 / 5 s, 20 / 10 min) par agent canonique, sans remboursement ; délai de 120 s après un
  « Non », une échéance ou une touche humaine. Codes stables : `tv_busy`, `preview_pending`, `no_change`,
  `unknown_preview`, `invalid_params`, `canvas_cooldown`, `canvas_unavailable`, `store_unavailable` (messages français
  dans `mcp.ERRORS_FR`).
- Muse : exactement 11 commandes inchangées ; une identité liée (`muse-device`) qui porterait `canvas` sort de
  `LINKED_REQUIRED` ⇒ `bridge_paused`. Aucun agent ne confirme, ne touche aux Réglages, au QR ou à la lecture.

## 6. Paquet et cycle de vie (tâche 5)

- `install.sh` livre `opentv_pi/*.py` (dont les 7 modules Canvas) et `ui/canvas.js` en 0644, listés au manifeste
  `/opt/opentv/MANIFEST.sha256`. Aucun fichier de `prototypes/` n'est installé.
- État : `/var/lib/opentv/canvas` (`state.json`, `state.json.lock`), dossier créé par
  `system/tmpfiles-opentv.conf` (`d /var/lib/opentv/canvas 0700 opentv opentv -`) à l'installation (avant tout
  (re)démarrage d'unité) et à chaque démarrage. Root n'y crée jamais de fichier : le service crée son verrou à son nom
  (`O_EXCL`) ; un dossier au mauvais mode/propriétaire n'est jamais « réparé » (fail-closed). `Config.canvas_dir`
  est la seule source du chemin (`canvas.store_for`).
- Bac à sable : `opentv.service` est le seul lecteur/écrivain (`ReadWritePaths=/var/lib/opentv`). Toutes les autres
  unités `.service` portent `InaccessiblePaths=-/var/lib/opentv/canvas` (ou masquent `/var/lib/opentv` entier : pont
  Muse). On masque le **dossier**, jamais `state.json` : un masque de fichier est défait par le remplacement atomique et
  sauté s'il manque au démarrage. Le kiosque n'a pas d'espace de noms (Cage/PAM, sandbox setuid de Chromium) : il est
  protégé par les droits Unix (`opentv-kiosk` hors du groupe `opentv`, parents 0700) et n'utilise que l'API.
  `opentv-splash.service` démarre en `sysinit` (`DefaultDependencies=no`) : si le dossier n'existe pas encore, son
  masque « - » est sauté — sans effet, ce oneshot root n'écrit que sur tty1. Tests :
  `tests/test_canvas_packaging.py` (matrice des unités) et `tests/test_catalogue_refresh.py` (liste dérivée de
  `Config`, `canvas_dir` compris).
- Mise à jour : le contenu de l'état n'est jamais lu ni écrit ; retour arrière automatique (échec avant le manifeste)
  rend fichiers Canvas, unités et tmpfiles à l'octet sans toucher l'état ; `uninstall.sh` conserve la scène,
  `--purge` efface `/var/lib/opentv` (`rm -rf`, aucun lien suivi).
- Promotion de « Regarder » : `promotion.REQUIRED` exige `ui/canvas.js` et chaque module Canvas dans le manifeste ;
  l'empreinte de livraison est le SHA-256 du manifeste complet après vérification de chaque fichier. Toute livraison
  ou modification d'un fichier Canvas (ou le passage à 0.7.0) rend la promotion `stale` jusqu'au canari HDMI réel puis
  `sudo /opt/opentv/bin/opentv-pi playback promote --field-validated`.

## 7. Tests

`test_canvas.py` (magasin, crash/fsync, verrous), `test_canvas_tv.py` (API, feuille, occupation, audit),
`test_canvas_ui.py` + `e2e_canvas_ui.js` + `e2e_canvas_service.js` (Chromium réel 1920×1080 : pause, dégradé,
mouvement réduit, adoption causale, hors-ligne), `test_canvas_tools.py` (permission, MCP, débits, Muse inchangé),
`test_canvas_prototype.py` (non-impact du prototype), `test_canvas_packaging.py` (tmpfiles, unités, installation,
mise à jour, retour arrière, désinstallation/purge, promotion, documentation, version). Les E2E exigent les vrais
dossiers de modules `OPENTV_TEST_PLAYWRIGHT` et `OPENTV_TEST_ACORN` (jamais `1`) ; un `skipped` n'est pas un vert.

## 8. Gates terrain (jamais prouvés hors matériel)

1. `systemd-analyze verify` et `security` sur Bookworm (absents sous macOS) ; dossier créé par tmpfiles réel
   (`drwx------ opentv opentv`).
2. Masques réels : `/var/lib/opentv/canvas` présent dans `/proc/<MainPID>/mountinfo` des unités masquées, absent
   pour `opentv` ; `opentv-kiosk` refusé sur `/var/lib/opentv`.
3. Budget de performance (README §6 quinquies) : CPU/RSS Chromium + Cage vs référence « Coupé », cadence sans mode
   dégradé, température, `get_throttled=0x0`, `hvs_underrun` stable ; aucune animation pendant la lecture.
4. Feuille d'un assistant autorisé sur la vraie TV (« Non » au focus, Retour = Non, « Oui » publie), assistant
   non autorisé refusé, rollback et ambiance d'origine visibles à distance de canapé.
5. « Regarder » : ambiance figée, lecteur seul, retour même carte, puis nouvelle promotion seulement après ce canari.
6. Photo/vidéo d'Alex avant de déclarer la porte visuelle franchie.
