# Autostart OpenTV depuis le NanoPi

Le NanoPi existant (`192.168.2.28`) surveille la TV et lance `OpnTV00001.OpenTV` une seule fois après un vrai cycle arrêt → allumage. Cette fonction ne touche pas au dashboard, à `muse-display`, à `musegadget`, au réseau ni au menu de service Samsung.

## Implémentation

- `opentv_autostart.py` : Python 3.11+ standard library, protocole SDB minimal, vérification HTTP `/status` avec `client.build`, état persistant et journaux JSON.
- `opentv-autostart.service` : service systemd **utilisateur** avec restart borné.
- `install.sh` : installe en `DRY_RUN=1` par défaut.
- `rollback.sh` : désactive et retire seulement ce service.

Détection :

1. port SDB `26101` ouvert ;
2. `cec-client` confirme `power status: on` ; un état CEC inconnu ou transitoire échoue fermé ;
3. après le délai de boot, `/status` est relu ; si OpenTV est déjà actif avec un `client.build`, aucune commande n'est envoyée ;
4. sinon `0 was_execute OpnTV00001.OpenTV` est envoyé, avec essais et délais bornés ;
5. succès uniquement après `/status` HTTP 200, `ok:true` et `client.build` non vide.

Une coupure réseau doit durer au moins `OFF_DEBOUNCE_S` avant de réarmer un nouveau cycle. Un redémarrage du service alors que la TV est déjà allumée ne lance rien (`LAUNCH_ON_START=0`).

## Installation

Depuis le répertoire `scripts/nanopi/` copié sur le NanoPi :

```sh
./install.sh
systemctl --user status opentv-autostart.service
journalctl --user -u opentv-autostart.service --no-pager
```

Configuration : `~/.config/systemd/user/opentv-autostart.env`.

Défaut sûr :

```text
TV_HOST=192.168.2.14
LAUNCH_ON_START=0
DRY_RUN=1
CEC_CMD=cec-client -s -d 1
CEC_STDIN=pow 0
OFF_DEBOUNCE_S=15
```

Après observation en dry-run, activer :

```sh
python3 - <<'PY'
from pathlib import Path
p = Path.home()/'.config/systemd/user/opentv-autostart.env'
s = p.read_text()
assert 'DRY_RUN=1' in s
p.write_text(s.replace('DRY_RUN=1', 'DRY_RUN=0'))
PY
systemctl --user restart opentv-autostart.service
```

Suspension d'urgence sans désinstaller :

```sh
mkdir -p ~/.local/state/opentv-autostart
touch ~/.local/state/opentv-autostart/disabled
```

Pour autoriser de nouveau : supprimer `disabled`, puis attendre un nouveau cycle d'alimentation réel.

Un service agent peut supprimer temporairement l'autostart en écrivant une époque Unix future dans `~/.local/state/opentv-autostart/suppress-until` avant de réveiller volontairement la TV pour afficher seulement le dashboard.

## Vérification

Tests hors TV :

```sh
python3 -m unittest -v native.test.test_opentv_autostart
```

Vérification réelle obligatoire :

1. TV en veille : journal `tv_off_at_start` ou `tv_off` ;
2. allumage : `tv_on`, puis un seul `launch_attempt` ;
3. `cycle_outcome=launched_verified` ;
4. `curl http://192.168.2.14:8096/status` rapporte le build attendu ;
5. confirmation visuelle d'Alex ;
6. deuxième cycle complet pour la persistance ;
7. `muse-display.service` reste actif avec le même renderer.

## Déploiements WGT après la bascule Host PC IP

La clé de signature reste sur le Mac. La TV accepte maintenant le SDB du NanoPi. Les prochains paquets signés doivent être transférés au NanoPi puis installés depuis celui-ci par un chemin de déploiement vérifié. Ne remettez pas la clé d'auteur sur le NanoPi.

## Retour arrière

```sh
./rollback.sh
```

Cela ne modifie ni OpenTV installé, ni le dashboard, ni la TV. Pour rendre le SDB au Mac, Alex remet physiquement la Host PC IP à `192.168.2.13` puis redémarre la TV.
