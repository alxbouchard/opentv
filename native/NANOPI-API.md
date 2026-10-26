# Contrat OpenTV ↔ NanoPi : applis externes sur HDMI 1 (API v1)

Une **appli externe** est une tuile OpenTV (`data-key="ext-<clé>"`, `data-kind="nanopi"`, badge
« HDMI 1 ») dont le contenu est rendu par le NanoPi sur HDMI 1. Elle n'est **jamais** lancée par
`tizen.application.launch` ; YouTube, Netflix et les autres applis Tizen restent des tuiles `app-<id>`.
Déclaration : `NOVA_NANOPI.base` et `NOVA_EXTERNAL` dans `app/config.js` (base acceptée seulement
en `http://` vers 127.0.0.1 / 10.x / 172.16-31.x / 192.168.x, sinon aucune tuile externe).

## Frontière de confiance

- OpenTV envoie seulement : une **clé d'appli** (`[a-z0-9-]{1,24}`), un **id de session** reçu du NanoPi,
  une **touche** parmi `up down left right ok back`, une **raison** de fermeture. Jamais d'URL ni de contenu.
- OpenTV ne recopie aucun texte du NanoPi dans la page ; seuls `ok`, `state` (liste fermée) et
  `session` (`[A-Za-z0-9_-]{8,64}`) sont lus. Toute autre réponse = échec.
- Le NanoPi possède la résolution des sites, le catalogue, le navigateur et ses délais.
- Service NanoPi `opentv-bridge`, port 8097, LAN uniquement, utilisateur `alex` non root, supervisé
  **séparément** du rendu du tableau de bord (`muse-display.service` n'est ni arrêté ni modifié).
  Phase 1 : liste d'IP sources autorisées = IP de la TV (192.168.2.14) ; réponses
  `Access-Control-Allow-Origin: *` ; requêtes simples sans en-tête (pas de pré-vol sur Chromium 56) ;
  limite 10 req/s ; toutes les actions journalisées sans secret. Phase 2 (si besoin) : jeton
  d'appairage affiché sur HDMI 1 et confirmé par Alex. Muse/Hermes n'utilisent pas cette API : ils
  passent par le service de contrôle sécurisé distinct.

## Points d'accès

| Requête | Réponse 200 | Erreurs |
|---|---|---|
| `POST /v1/session?app=catalogue` (Films) ou `?app=pourtoi` (Pour toi) | `{ok:true, session, state:"starting"\|"catalogue"}` ; idempotent si déjà ouvert | 404 `unknown_app`, 409 `busy`, 503 `discovery_failed` |
| `POST /v1/key?session=S&k=K` | `{ok:true, state}` (après traitement de la touche) | 400 `bad_key`, 410 `no_session` |
| `POST /v1/close?session=S&reason=R` ou `?app=A&reason=R` | `{ok:true, state:"dashboard"}` ; idempotent | — |
| `GET /v1/health?session=S` | `{ok:true, api:1, state, session\|null}` | — |

`state` ∈ `starting`, `catalogue`, `player`, `dashboard`. `dashboard` ou un `session` différent signifie
que la session est terminée : OpenTV quitte le mode externe et laisse HDMI 1 affiché (tableau de bord).

## Comportement attendu du NanoPi

- **Catalogue** : interface propre (titres, affiches, description, notes IMDb et TMDb étiquetées séparément), navigable aux flèches,
  rendue localement par le NanoPi. Métadonnées seulement.
- **Lecture** : `ok` sur un titre ouvre **uniquement la page publique actuelle du film** dans Chromium
  plein écran (`state:"player"`). Aucune extraction, proxy, réécriture, blocage ou contournement des
  flux vidéo ou protections du site. Si une source légitime/autorisée est fournie plus tard, le lecteur
  peut l'utiliser à la place.
  Assistance « un clic » côté NanoPi seulement (contrat inchangé) : après le OK, l'unique lecteur visible dominant
  de la page (iframe, même d'une autre origine, ou `<video>`) est agrandi sur la page parente et reçoit au plus
  un clic central, s'il n'est pas recouvert ; sinon la page publique reste telle quelle (voir BRIDGE.md).
- **Retour** : lecteur → ferme la page et restaure le catalogue à la même position ; racine du
  catalogue → ferme la session (`dashboard`). `close`, échec, ou inactivité (catalogue 5 min sans
  touche ; lecteur : plafond configurable) → session fermée, tableau de bord affiché.
- **Préfixe rotatif** (ex. `https://<origine>/<préfixe>/`) : découvert depuis la racine `https://<origine>/`
  (premier lien relatif `^[a-z0-9]{5,16}$`, ou en-tête `Location` d'une redirection). Exiger **même
  hôte + HTTPS** (une redirection vers `http://` du même hôte est remontée en `https://`, tout autre
  hôte refusé), 3 redirections max, réponse `text/html` 200 sur `/<préfixe>/`. Cache
  `{préfixe, découvertLe, validéLe}` en fichier 0600 : TTL 6 h ; 404/redirection sur le préfixe en
  cache → une seule redécouverte ; échec → dernier préfixe valide gardé 24 h max, puis
  `503 discovery_failed`. Toute URL ouverte dans le lecteur doit avoir l'origine découverte et commencer
  par `/<préfixe>/`.

## Appli « Pour toi » (`pourtoi`, OpenTV v23)

Même contrat, mêmes états : `catalogue` désigne ici la vue personnelle (rangées + panneau d'actions), `player` la
page publique ouverte par « Regarder » (même chemin que Films). OpenTV relaie seulement les 6 touches ; les actions
(J'aime, Pas pour moi, Déjà vu, À voir plus tard, Pas ce soir) sont décidées et enregistrées par le NanoPi
(`scripts/nanopi/FORYOU.md`). Une seule session à la fois : passer de Films à Pour toi ferme l'une puis ouvre l'autre.

## Côté OpenTV (v22, inchangé en v23)

- `EXT` : `""` → `"opening"` (POST session) → `"on"` (HDMI 1 `BEHIND`, page transparente, touches
  relayées). Session acceptée **avant** d'afficher HDMI 1 ; échec = message, accueil, aucun écran noir.
- `EXT_FOCUS` : `nanopi` (flèches/OK/Retour → `/v1/key`, file bornée à 1 en vol + 4) ou `opentv`
  (touche rouge 403 ou `/cmd?cmd=focus` : rangée OpenTV par-dessus ; Retour/10 s inactif → `nanopi`).
- Exit (10071) → `close` + tableau de bord ; `home` → `close` + menu OpenTV ; lancement d'une appli
  Tizen, ouverture d'un écran, ou OpenTV en arrière-plan → `close`. `GET /v1/health` toutes les 2 s ;
  3 échecs consécutifs → fin + « NanoPi injoignable ».
- `/status.client.screen` = `ext:<n|o>:<état>` pendant une session (pas de changement du service).

## Implémentation NanoPi (opentv-bridge)

`scripts/nanopi/opentv_bridge.py` + `BRIDGE.md`. Précisions : débit 20 req/s (OpenTV compte un 429 comme échec) ;
`503 display_unavailable` tant que l'affichage n'est pas validé (`DISPLAY_BACKEND=off`) ; un `close` portant une
session périmée ne ferme pas la session courante ; le préfixe est validé en suivant ≤3 redirections même origine
restant sous `/<préfixe>/` (302 vers `/<préfixe>/home/<jeton>`). Migration de domaine (fakrov.com → wadrox.com)
annoncée par la racine de confiance et validée côté pont, sans changement du contrat ni de l'appli TV.
