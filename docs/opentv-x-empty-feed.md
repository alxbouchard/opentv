# X TV 0.7.19 — accueil vide et récupération à la télécommande

## Constat établi, cause inconnue

Le signalement terrain décrit **For you sélectionné, petit spinner, zéro post, aucun message d'erreur**.
Aucun DOM/traçage réseau réel n'est disponible ici. Aucun accès au compte, fil privé, SSH, sauvegarde de
session, API X ni à la TV. La fixture locale représente cet état, pas une copie pixel-identique de la
photo et pas une preuve de la cause de l'arrêt du chargement.

Entrée et réouverture : `https://x.com/home` (`ui/tv.js`, après acquittement du témoin de présence),
même kiosque/profil. Retour Samsung passe par l'aide evdev existante et redémarre le kiosque sur `/tv` ;
ni ce mécanisme, ni CEC, ni le profil, ni les politiques Chromium ne changent.

Avant : `primaryColumn` suffisait à activer le mode **fil**, même sans premier article. `x.js` absorbait
les flèches sans publication navigable ; `nav.js` désactivait sa navigation des contrôles natifs.
Un Retry natif devenait inaccessible au D-pad et l'aide proposait des actions de posts inexistants.
**Défaut de récupération reproduit, pas cause démontrée du spinner distant.**

La même fixture reste vide sans extension, en production 0.7.15
(`4e209869a78e0a3feddd3c0d1b7110ec8fdd42a3`) et avec le candidat lorsque sa source simulée ne termine
jamais. Les trois affichent l'article différé quand la fixture termine, y compris par IntersectionObserver.
Rien ici ne prouve une panne extérieure de X, ni un masquage CSS causant l'incident réel. Des erreurs HTTP
sur des racines CDN n'établissent ni l'un ni l'autre.

## Changement

- Mode **récupération**, séparé de la connexion, tant que la colonne n'a pas de tweet **rendu**
  (pas seulement un article placeholder). Retry/onglets natifs atteignables aux flèches ; OK laissé à
  Chromium ou au gestionnaire clavier du site. Un simple rôle ARIA sans gestionnaire reste inactif :
  aucun repli `.click()`, même si un article ou une modale apparaît pendant la touche. Le repli historique
  de connexion ne peut pas traverser un changement de mode ; aucune préférence de connexion automatique
  ni réarmement dans le fil vide. La sonde conserve son schéma existant (`controls`).
- Sur `/home` avec colonne mais sans post rendu ni modale : message honnête **« Aucune publication
  affichée pour l'instant. Le chargement peut encore aboutir. »**, bouton natif **Recharger X**.
  Aucun diagnostic par timeout, recharge automatique, clic simulé, touche forgée, API X, faux fil ou action de compte.
- Depuis la page sans contrôle focalisé : **↑ puis OK** sélectionne/active Recharger X ; sinon ↑/↓ pour
  l'atteindre. Activation humaine fiable, répétition refusée ; état et focus relus au clic. Article/modale
  apparu entre-temps ⇒ pas de recharge. Profil conservé, aucune promesse de réparer la source distante.
  Le dernier emplacement de focus est réservé au bouton, même si les contrôles X atteignent le plafond
  de parcours ; aucune consigne Recharger lorsque le panneau est caché faute d'espace.
- Panneau mesuré contre contrôles, progression, erreurs et états natifs : espace libre vertical ou rien
  si aucun emplacement n'est disponible. Pas de focus automatique ni de défilement à l'insertion.
  Dialogues/connexion séparés et prioritaires.
- Articles tardifs, rendu révélé sur place, retrait/remplacement virtualisé : aide et récupération mises
  à jour. Le style TV reste lié à la colonne pour conserver la géométrie des placeholders lazy ; aucun
  changement de largeur présenté comme réparation réseau.
- Un article en chargement (`aria-busy` ou indicateur de progression), même haut de 180 px, ne compte
  que si un marqueur de publication propre à cet article est rendu : texte, photo, vidéo ou carte.
  Pas de lecture de message ni de texte obligatoire. Un autre post prêt reste navigable.
- Le OK sur J’aime/Enregistrer est vérifié **avant** de céder les touches au mode récupération.
  Le clic clavier natif est aussi revérifié si la page a recyclé le post pendant la touche. Le focus
  seul, même rétabli par le site, ne vaut pas sélection d’un nouveau post.
- **`x.css` inchangé** : sa liste exacte de
  display:none masque l'interface de page et des contrôles secondaires identifiés individuellement, pas
  d'enveloppe générique du fil/lazy. Alert/progression/retry de la fixture restent visibles. Cela ne prouve
  pas tous les futurs DOM X sûrs : une vraie erreur placée sous une toolbar spécifiquement masquée demanderait
  sa propre preuve, pas un diagnostic inventé.

## Validation ciblée

`tests/e2e_x_empty.js` : Chromium complet, extension chargée, pages anonymes locales interceptées ; toutes
les autres requêtes sont coupées. `tests/test_x_empty.py` installe le candidat dans un rootfs temporaire,
puis exécute sans extension → production → candidat **séquentiellement**. Objet Git production obligatoire,
jamais silencieusement ignoré. Utiliser un TMPDIR géré et conserver les preuves :

```sh
export OPENTV_TEST_PLAYWRIGHT=/chemin/absolu/node_modules/playwright
export OPENTV_TEST_CHROME_FULL=/chemin/absolu/chrome-complet
export OPENTV_TEST_SHOTS=/chemin/absolu/preuves
export TMPDIR=/chemin/absolu/scratch
PYTHONPATH=scripts/pi4 python3 -m unittest tests.test_x_empty -v
```

Cas : première ouverture ; spinner persistant (zéro navigation/post automatique) ; erreur native + Retry
par D-pad et OK natif fiable ; IntersectionObserver lent ; non-recouvrement d'un Retry bas ; aucun espace
libre ; retrait virtualisé ; article masqué/révélé sur place ; insertion différée ; modale prioritaire ;
connexion générique ; réouverture depuis la destination `/tv` de Retour ; rejet du clic/touche synthétique
avec bouton déjà focalisé ; recharge humaine unique qui peut toujours échouer honnêtement ; succès tardif
puis navigation des posts. **Retour physique Samsung n'est pas testé par la modélisation de sa destination.**

Revue de la première candidate : RED sur `fffd9a3` (div rôle button : Entrée réelle puis clic synthétique),
puis GREEN en mode récupération distinct : rôle sans activation, rôle avec gestionnaire clavier du site
(une seule Entrée), arrivée d'une modale/post pendant la touche, disparition de modale vers fil vide
(aucun clic en attente), Retry natif, Recharger et plafond de 405 contrôles.

RED observé sur production : Retry inaccessible. RED supplémentaires : panneau couvrant Retry, récupération
périmée après révélation sur place, bouton masqué encore annoncé. GREEN à lire dans les journaux du run réel,
pas déduit de ce document. Ajouter les tests existants ciblés fil/actions et sécurité/connexion ; aucune
matrice longue de release complète n'est revendiquée.

## Gate terrain et prochain geste

Pour la contre-revue hors ligne : dans un clone local avec historique, lancer la commande de validation
ci-dessus. `test_recycled_action_and_loading_article` installe successivement les cinq extensions en
rootfs temporaires. Comparer `recycled-*.json` : 0.7.15 bloque le OK sur B dans la transition avant/après
rafraîchissement, `689c9dd` le laisse passer ; les deux sélectionnent le squelette. Le nouveau SHA doit
laisser zéro activation de B, un OK initial valide sur A par appui tenu, et sélectionner Recharger puis
Retry, le média apparu sur place, le vrai post voisin et le bouton de modale.

Comparer aussi `unmarked` : même transition, mais la cellule perd `data-testid="tweet"`.
Après rafraîchissement, la fixture attend le retrait de l’anneau, redonne le focus au bouton J’aime
et vérifie qu’il l’a repris. Sur `2601777`, OK active B ; OK tenu active A puis B. Sur le candidat,
attendre `[]` pour l’appui simple et seulement A pour l’appui tenu, avant **et** après rafraîchissement.
Le marqueur du bouton suffit pour reconnaître l’action à contrôler, pas pour l’autoriser.

Pour le partage manuel (←), lire dans les mêmes JSON `shareA`, `shareStale` et `shareReselected`.
Sur `298e0c6`, la cellule A devient B sans changer de nœud : le journal passe de `[A]` à `[A,B]`
sans nouvelle sélection. Sur le correctif, il doit rester `[A]`, puis devenir `[A,B]` seulement
après resélection explicite. Hors colonne ou sans permalien vérifiable, aucun message supplémentaire.
Le journal passif du service worker de test ne conserve que les identifiants de fixture ; il ne répond
à aucun message. Le texte de la notification d’un récepteur non appairé ne prouve pas l’absence d’envoi.

Ni push ni déploiement dans cette tâche. Après revue indépendante puis installation séparément autorisée
du SHA exact : conserver la connexion du propriétaire, ouvrir X avec la Samsung. Si le spinner persiste,
**↑ puis OK sur Recharger X** (pas de flèche droite) ; observer si de vrais posts apparaissent. Si Retry
est proposé par X, le sélectionner aux ↑/↓ puis OK. Retour doit toujours quitter X vers OpenTV.
Spinner toujours présent ⇒ problème source/session/réseau/UI non résolu, pas réparation validée.
Ne pas déconnecter le compte, sonder le fil privé, élargir les politiques ou forcer la lecture.
