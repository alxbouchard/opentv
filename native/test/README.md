# Tests OpenTV natif (Playwright)

1. Décompresser le `.wgt` à tester ici : `unzip -o OpenTV.wgt -d native/test/wgt`
2. Depuis `native/test/` : `node test.js` (14), `node live.js` (12), `node animcheck.js`, `node scroll.js`, `node visual.js`
3. À chaque nouvelle version, mettre à jour dans `test.js` la version attendue (`vNN native`) et la version du service (`"version":NN`).

L'API `tizen` est simulée (addInitScript). Ces tests ne remplacent pas la vérification sur la TV (`/status`) ni la confirmation à l'écran.
