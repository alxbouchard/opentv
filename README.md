# OpenTV — écran d'accueil pour TV Samsung (TizenBrew)

Un accueil personnalisable qui remplace le fouillis du Smart Hub : tes applis avec
leurs vrais logos, des fonds photo, et un panneau Apparence. Les applis non installées
sur la TV se masquent automatiquement.

## Installation
1. Installe TizenBrew sur la TV.
2. Dans TizenBrew : Ajouter un module GitHub → `alxbouchard/opentv@main`.
3. Lance OpenTV. Pour l'avoir au démarrage : réglages TizenBrew → AutoLaunch → OpenTV.

Module `app` TizenBrew : `service.js` (liste et lance les applis via l'API Tizen),
`app/` (l'interface). Pensé pour Tizen 4.0+ / Chromium 56+.
