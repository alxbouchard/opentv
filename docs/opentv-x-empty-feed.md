# X TV 0.7.16 — accueil vide et récupération à la télécommande

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

- Mode contrôles jusqu'à un tweet **rendu** (pas seulement un article placeholder). Retry/onglets natifs
  atteignables aux flèches ; Entrée native sur un bouton natif.
- Sur `/home` avec colonne mais sans post rendu ni modale : message honnête **« Aucune publication
  affichée pour l'instant. Le chargement peut encore aboutir. »**, bouton natif **Recharger X**.
  Aucun diagnostic par timeout, recharge automatique, clic simulé, touche forgée, API X, faux fil ou action de compte.
- Depuis la page sans contrôle focalisé : **↑ puis OK** sélectionne/active Recharger X ; sinon ↑/↓ pour
  l'atteindre. Activation humaine fiable, répétition refusée ; état et focus relus au clic. Article/modale
  apparu entre-temps ⇒ pas de recharge. Profil conservé, aucune promesse de réparer la source distante.
- Panneau mesuré contre contrôles, progression, erreurs et états natifs : espace libre vertical ou rien
  si aucun emplacement n'est disponible. Pas de focus automatique ni de défilement à l'insertion.
  Dialogues/connexion séparés et prioritaires.
- Articles tardifs, rendu révélé sur place, retrait/remplacement virtualisé : aide et récupération mises
  à jour. Le style TV reste lié à la colonne pour conserver la géométrie des placeholders lazy ; aucun
  changement de largeur présenté comme réparation réseau.
- Identité, cibles et activation J'aime/Enregistrer inchangées. **`x.css` inchangé** : sa liste exacte de
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

RED observé sur production : Retry inaccessible. RED supplémentaires : panneau couvrant Retry, récupération
périmée après révélation sur place, bouton masqué encore annoncé. GREEN à lire dans les journaux du run réel,
pas déduit de ce document. Ajouter les tests existants ciblés fil/actions et sécurité/connexion ; aucune
matrice longue de release complète n'est revendiquée.

## Gate terrain et prochain geste

Ni push ni déploiement dans cette tâche. Après revue indépendante puis installation séparément autorisée
du SHA exact : conserver la connexion du propriétaire, ouvrir X avec la Samsung. Si le spinner persiste,
**↑ puis OK sur Recharger X** (pas de flèche droite) ; observer si de vrais posts apparaissent. Si Retry
est proposé par X, le sélectionner aux ↑/↓ puis OK. Retour doit toujours quitter X vers OpenTV.
Spinner toujours présent ⇒ problème source/session/réseau/UI non résolu, pas réparation validée.
Ne pas déconnecter le compte, sonder le fil privé, élargir les politiques ou forcer la lecture.
