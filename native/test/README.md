# Tests OpenTV natif (Playwright)

1. Décompresser le `.wgt` à tester ici : `unzip -o OpenTV.wgt -d native/test/wgt`
2. Depuis `native/test/` : `node test.js` (14), `node live.js` (12), `node ext.js` (27, faux NanoPi sur :18097), `node animcheck.js`, `node scroll.js`, `node visual.js`.
   Captures : `OPENTV_SHOTS=<dossier>` (défaut : dossier temporaire)
3. À chaque nouvelle version, mettre à jour dans `test.js` la version attendue (`vNN native`) et la version du service (`"version":NN`).

L'API `tizen` est simulée (addInitScript). Ces tests ne remplacent pas la vérification sur la TV (`/status`) ni la confirmation à l'écran.

Autostart NanoPi sans Node : depuis la racine, `python3 -m unittest -v native.test.test_opentv_autostart`.
La suite exerce le vrai démon Python contre une fausse TV SDB/HTTP et vérifie également les scripts d'installation/rollback.

Pont NanoPi des applis externes (scripts/nanopi/opentv_bridge.py, voir scripts/nanopi/BRIDGE.md) :
`python3 -m unittest -v native.test.test_opentv_bridge` (69, dont Chromium réel headless),
`native.test.test_opentv_play_assist` (17, assistance de lecture, Chromium réel) et `native.test.test_opentv_tmdb` (27) puis, depuis `native/test/`,
`node bridge_e2e.js` (17 : vraie page OpenTV ↔ vrai pont en sous-processus, affichage nul).
