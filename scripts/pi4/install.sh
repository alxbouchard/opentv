#!/bin/bash
# OpenTV Raspberry Pi 4 — installation / mise à jour réversible (Raspberry Pi OS Lite Bookworm 64-bit, « Legacy »).
#
#   sudo ./install.sh                      installation réelle (préflight strict, sauvegardes, manifeste, services)
#   ./install.sh --dry-run                 affiche le plan complet, n'écrit rien (macOS/Linux, sans root)
#   ./install.sh --root /tmp/rfs --skip-preflight   construit un rootfs de test sans root (aucun systemctl)
#
# Rien n'est téléchargé ni flashé. 1080p : ajoute video=HDMI-A-1:1920x1080@60 à /boot/firmware/cmdline.txt (sauvegardé,
# retiré par uninstall.sh). Mise à jour : fichiers de l'ancien manifeste absents du nouveau retirés (sauvegardés),
# services redémarrés. Tout échec AVANT l'écriture du manifeste ⇒ retour automatique à l'état précédent. Après ce
# point de non-retour (sysusers, tmpfiles, systemctl), un échec s'arrête avec un message explicite : pas de retour
# automatique, la commande de retour arrière est affichée.
# Retour arrière : /opt/opentv/uninstall.sh (restaure les fichiers sauvegardés).
set -Eeuo pipefail
umask 022

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ROOT="/"
DRY=0
SKIP_PREFLIGHT=0
ENABLE=1

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  echo "Options : --dry-run  --root <dossier absolu>  --skip-preflight (racine de test seulement)  --no-enable"
}
die() { echo "opentv-install: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root exige un chemin"; ROOT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    --no-enable) ENABLE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done

case "$ROOT" in /*) ;; *) die "--root doit être un chemin absolu" ;; esac
if [ "$ROOT" != "/" ]; then ROOT="${ROOT%/}"; fi
LIVE=0
[ "$ROOT" = "/" ] && LIVE=1
if [ "$SKIP_PREFLIGHT" = 1 ] && [ "$LIVE" = 1 ]; then
  die "--skip-preflight est réservé aux racines de test (--root)"
fi
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
  die "installation réelle : exécuter en root (sudo), ou utiliser --dry-run / --root <dossier>"
fi

dest() { if [ "$ROOT" = "/" ]; then printf '%s' "$1"; else printf '%s%s' "$ROOT" "$1"; fi; }
act() { if [ "$DRY" = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }
host() {   # commande système : seulement sur la vraie racine, hors dry-run
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then "$@"
  elif [ "$DRY" = 1 ]; then say "[dry-run] $*"
  elif [ -n "${OPENTV_INSTALL_TEST_FAIL:-}" ] && case "$*" in *"$OPENTV_INSTALL_TEST_FAIL"*) true ;; *) false ;; esac; then
    say "[racine de test, échec simulé] $*"   # tests seulement : jamais sur la vraie racine (LIVE=1 ci-dessus)
    return 1
  else say "[racine de test, non exécuté] $*"; fi
}
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
listed() {   # listed <ligne> <liste> : appartenance exacte, sans pipe (pas de faux négatif SIGPIPE sous pipefail)
  case $'\n'"$2"$'\n' in *$'\n'"$1"$'\n'*) return 0 ;; esac
  return 1
}
in_scope() {   # chemins qu'OpenTV a le droit de retirer (même périmètre que uninstall.sh)
  case "$1" in
    /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- fichiers livrés (source|destination|mode)
FILES=()
add() { FILES+=("$1|$2|$3"); }
for f in "$HERE"/opentv_pi/*.py; do add "$f" "/opt/opentv/lib/opentv_pi/$(basename "$f")" 644; done
for b in opentv-pi opentv-mcp opentv-net-helper opentv-kiosk-browser; do add "$HERE/bin/$b" "/opt/opentv/bin/$b" 755; done
for u in tv.html tv.js tv.css setup.css fallback.html; do add "$HERE/ui/$u" "/opt/opentv/share/ui/$u" 644; done
add "$REPO/native/app/qrcode.js" /opt/opentv/share/ui/qrcode.js 644                  # réutilisé tel quel
add "$REPO/scripts/nanopi/opentv_foryou.py" /opt/opentv/lib/foryou/opentv_foryou.py 644  # moteur « Pour toi »
add "$REPO/scripts/nanopi/opentv_bridge.py" /opt/opentv/lib/foryou/opentv_bridge.py 644  # load_catalogue_meta
for u in opentv.service opentv-kiosk.service opentv-net-helper.service opentv-net-helper.path \
         opentv-ap-guard.service opentv-ap-guard.timer; do
  add "$HERE/systemd/$u" "/etc/systemd/system/$u" 644
done
add "$HERE/system/sysusers-opentv.conf" /etc/sysusers.d/opentv.conf 644
add "$HERE/system/tmpfiles-opentv.conf" /etc/tmpfiles.d/opentv.conf 644
add "$HERE/system/pam-opentv-kiosk" /etc/pam.d/opentv-kiosk 644
add "$HERE/uninstall.sh" /opt/opentv/uninstall.sh 755
add "$HERE/README.md" /opt/opentv/README.md 644
add "$HERE/opentv-pi.env.example" /opt/opentv/opentv-pi.env.example 644
NEW_DSTS=""
for e in "${FILES[@]}"; do
  src="${e%%|*}"; rest="${e#*|}"
  [ -f "$src" ] || die "source manquante : $src"
  NEW_DSTS="$NEW_DSTS${rest%%|*}"$'\n'
done

# ---------------------------------------------------------------- préflight strict
if [ "$SKIP_PREFLIGHT" = 1 ]; then
  say "préflight : ignoré (racine de test $ROOT)"
elif command -v python3 >/dev/null 2>&1 && pf="$(python3 -I "$HERE/bin/opentv-pi" preflight --root "$ROOT" --text)"; then
  say "$pf"
  say "préflight : OK"
else
  say "${pf:-python3 introuvable}"
  if [ "$DRY" = 1 ]; then
    say "préflight : ÉCHEC — dry-run : le plan est affiché, une installation réelle serait refusée"
  else
    die "préflight échoué : installation refusée, aucun fichier modifié (dépendances : sudo ./prepare-pi.sh --yes)"
  fi
fi

# ---------------------------------------------------------------- copie avec sauvegardes et retour arrière
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK="/var/lib/opentv-install/backup-$TS-$$"   # unique : deux exécutions dans la même seconde ne se mélangent pas
MANIFEST="/opt/opentv/MANIFEST.sha256"
CMDF="/boot/firmware/cmdline.txt"
VIDEO_ARG="video=HDMI-A-1:1920x1080@60"
VIDEO_REC="/var/lib/opentv-install/cmdline-video.added"
OLD_LIST=""
if [ -f "$(dest "$MANIFEST")" ]; then OLD_LIST="$(awk '{print $2}' "$(dest "$MANIFEST")")"; fi
NEW=()
SAVED=()
REMOVED=()
STARTED=0
CUR=""
CMDLINE_CHANGED=0
VIDEO_REC_NEW=0

rollback() {
  set +e
  trap - ERR
  [ "$DRY" = 1 ] && exit 1
  if [ "$STARTED" = 1 ]; then
    echo "opentv-install: échec — retour à l'état précédent" >&2
    local d
    [ -n "$CUR" ] && rm -f -- "$(dest "$CUR").opentv-tmp"
    rm -f -- "$(dest "$MANIFEST").tmp" "$(dest "$CMDF").opentv-tmp"
    for d in ${NEW[@]+"${NEW[@]}"}; do rm -f -- "$(dest "$d")"; done
    for d in ${SAVED[@]+"${SAVED[@]}"} ${REMOVED[@]+"${REMOVED[@]}"}; do
      mkdir -p -- "$(dirname "$(dest "$d")")"
      cp -p -- "$(dest "$BK$d")" "$(dest "$d")" || echo "opentv-install: restauration impossible : $d" >&2
    done
    if [ "$CMDLINE_CHANGED" = 1 ]; then
      cp -p -- "$(dest "$BK$CMDF")" "$(dest "$CMDF")" || echo "opentv-install: restauration impossible : $CMDF" >&2
    fi
    [ "$VIDEO_REC_NEW" = 1 ] && rm -f -- "$(dest "$VIDEO_REC")"
  fi
  exit 1
}
trap rollback ERR

backup() {   # copie de sécurité d'un fichier existant dans $BK (même arborescence)
  act mkdir -p "$(dirname "$(dest "$BK$1")")"
  act cp -p -- "$(dest "$1")" "$(dest "$BK$1")"
}

say "installation dans $ROOT (sauvegardes : $BK)"
for e in "${FILES[@]}"; do
  src="${e%%|*}"; rest="${e#*|}"; dst="${rest%%|*}"; mode="${rest##*|}"
  D="$(dest "$dst")"
  if [ -f "$D" ] && [ "$(sha256 "$D")" = "$(sha256 "$src")" ]; then
    act chmod "$mode" "$D"
    continue
  fi
  STARTED=1
  CUR="$dst"
  if [ -e "$D" ]; then
    backup "$dst"
    SAVED+=("$dst")
    if ! listed "$dst" "$OLD_LIST"; then   # fichier étranger : restauré à la désinstallation
      if [ "$DRY" = 1 ]; then say "[dry-run] sauvegarde d'origine : $dst"
      else printf '%s\n' "$dst" >> "$(dest "$BK/BACKUP.list")"; fi
    fi
  else
    NEW+=("$dst")
  fi
  act mkdir -p "$(dirname "$D")"
  act cp -- "$src" "$D.opentv-tmp"
  act chmod "$mode" "$D.opentv-tmp"
  act mv -f -- "$D.opentv-tmp" "$D"
done
CUR=""

# ---------------------------------------------------------------- mise à jour : fichiers de l'ancien manifeste retirés
while IFS= read -r old; do
  [ -n "$old" ] || continue
  if listed "$old" "$NEW_DSTS"; then continue; fi
  if ! in_scope "$old"; then say "ignoré (hors périmètre OpenTV) : $old"; continue; fi
  [ -e "$(dest "$old")" ] || [ -L "$(dest "$old")" ] || continue
  STARTED=1
  backup "$old"
  REMOVED+=("$old")
  act rm -f -- "$(dest "$old")"
  say "retiré (absent du nouveau manifeste) : $old"
done <<EOF
$OLD_LIST
EOF

# ---------------------------------------------------------------- 1080p garanti (paramètre noyau, réversible)
C="$(dest "$CMDF")"
if [ ! -f "$C" ]; then
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then die "$CMDF introuvable : Raspberry Pi OS Bookworm requis"; fi
  say "cmdline.txt absent ($ROOT) : 1080p non appliqué"
elif grep -q 'video=HDMI-A-1:' "$C"; then
  say "cmdline.txt : video=HDMI-A-1 déjà présent, inchangé"
elif [ "$DRY" = 1 ]; then
  say "[dry-run] ajouter $VIDEO_ARG à $CMDF (sauvegarde $BK$CMDF ; retiré par uninstall.sh)"
else
  STARTED=1
  backup "$CMDF"
  CMDLINE_CHANGED=1
  {
    printf '%s %s\n' "$(head -n 1 "$C")" "$VIDEO_ARG"
    tail -n +2 "$C"
  } > "$C.opentv-tmp"
  chmod "$(stat -c %a "$C" 2>/dev/null || stat -f %Lp "$C")" "$C.opentv-tmp"
  mv -f -- "$C.opentv-tmp" "$C"
  if [ ! -f "$(dest "$VIDEO_REC")" ]; then
    mkdir -p -- "$(dirname "$(dest "$VIDEO_REC")")"
    printf '%s\n' "$VIDEO_ARG" > "$(dest "$VIDEO_REC")"
    VIDEO_REC_NEW=1
  fi
  say "cmdline.txt : $VIDEO_ARG ajouté — 1080p effectif au prochain redémarrage (sudo reboot)"
fi

ENVF="/etc/opentv/opentv.env"
if [ ! -e "$(dest "$ENVF")" ]; then
  STARTED=1
  NEW+=("$ENVF")
  act mkdir -p "$(dest /etc/opentv)"
  act cp -- "$HERE/opentv-pi.env.example" "$(dest "$ENVF")"
  act chmod 640 "$(dest "$ENVF")"
else
  say "configuration conservée : $ENVF"
fi

# ---------------------------------------------------------------- manifeste SHA-256 (fichiers livrés seulement)
if [ "$DRY" = 1 ]; then
  say "[dry-run] manifeste $MANIFEST : ${#FILES[@]} fichiers"
else
  tmp="$(dest "$MANIFEST").tmp"
  : > "$tmp"
  for e in "${FILES[@]}"; do
    rest="${e#*|}"; dst="${rest%%|*}"
    printf '%s  %s\n' "$(sha256 "$(dest "$dst")")" "$dst" >> "$tmp"
  done
  chmod 644 "$tmp"
  mv -f -- "$tmp" "$(dest "$MANIFEST")"
  say "manifeste : $(wc -l < "$(dest "$MANIFEST")" | tr -d ' ') fichiers"
fi
trap - ERR

# ---------------------------------------------------------------- point de non-retour : fichiers et manifeste en place
# Les étapes suivantes modifient l'état du système (comptes, dossiers, unités) : aucun retour automatique fiable n'est
# possible. Un échec s'arrête ici avec la commande exacte de retour arrière (jamais de faux « rollback »).
post_fail() {
  echo "opentv-install: ÉCHEC après l'installation des fichiers : « $1 »" >&2
  echo "opentv-install: fichiers OpenTV et manifeste en place ; aucun retour automatique à ce stade." >&2
  echo "opentv-install: revenir à l'état précédent : sudo /opt/opentv/uninstall.sh   (sauvegardes : $BK)" >&2
  echo "opentv-install: ou corriger la cause (journalctl -xe) puis relancer : sudo ./install.sh" >&2
  exit 1
}
post() { host "$@" || post_fail "$*"; }
post systemd-sysusers /etc/sysusers.d/opentv.conf
post systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf
post chown root:opentv "$ENVF"
post systemctl daemon-reload
if [ "$ENABLE" = 1 ]; then
  # opentv-ap-guard.timer : garde root du Wi-Fi temporaire, indépendante du service Python
  post systemctl enable opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service
  post systemctl restart opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service
fi
if ! host python3 -I /opt/opentv/bin/opentv-pi health; then
  say "santé : à vérifier (sudo /opt/opentv/bin/opentv-pi health ; journalctl -u opentv -u opentv-kiosk)"
fi
if [ "$CMDLINE_CHANGED" = 1 ]; then say "1080p : redémarrer la boîte pour appliquer $VIDEO_ARG"; fi
say "terminé. Retour arrière : sudo /opt/opentv/uninstall.sh   (ajouter --purge pour effacer l'état)"
