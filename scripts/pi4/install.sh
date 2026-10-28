#!/bin/bash
# OpenTV Raspberry Pi 4 — installation / mise à jour réversible (Raspberry Pi OS Lite Bookworm 64-bit, « Legacy »).
#
#   sudo ./install.sh                      installation réelle (préflight strict, sauvegardes, manifeste, services)
#   ./install.sh --dry-run                 affiche le plan complet, n'écrit rien (macOS/Linux, sans root)
#   ./install.sh --root /tmp/rfs --skip-preflight   construit un rootfs de test sans root (aucun systemctl)
#
# Rien n'est téléchargé ni flashé. cmdline.txt (sauvegardé, jetons ajoutés notés puis retirés exactement par
# uninstall.sh) : 1080p (video=HDMI-A-1:1920x1080@60) et démarrage silencieux (quiet loglevel=3 logo.nologo
# vt.global_cursor_default=0 systemd.show_status=false) ; écran OpenTV sur tty1 (opentv-splash), pas d'invite tty1
# tant que le kiosque est activé. Mise à jour : fichiers de l'ancien manifeste absents du nouveau retirés (sauvegardés),
# services redémarrés. Tout échec AVANT l'écriture du manifeste ⇒ retour automatique à l'état précédent. Après ce
# point de non-retour (sysusers, tmpfiles, systemctl), un échec s'arrête avec la commande de retour arrière.
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
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
# Écritures durables (carte SD, /boot/firmware en FAT, cache d'écriture) : fsync explicite des fichiers et dossiers
# nommés (GNU coreutils « sync FICHIER… », Raspberry Pi OS) ; à défaut (sync sans argument : macOS, ancien sync),
# sync global. Échec des deux ⇒ erreur (jamais de renommage d'un contenu non écrit).
durable_sync() { sync -- "$@" 2>/dev/null || sync; }
# replace_durably <temp> <cible> : temp complet synchronisé AVANT le renommage atomique (même dossier), puis cible et
# dossier synchronisés APRÈS. Une coupure à tout instant laisse l'ancien contenu complet ou le nouveau, jamais un vide.
replace_durably() {
  durable_sync "$1" || return 1
  mv -f -- "$1" "$2" || return 1
  durable_sync "$2" "$(dirname "$2")"
}
put_durably() { printf '%s\n' "$2" > "$1.opentv-tmp" && replace_durably "$1.opentv-tmp" "$1"; }   # <chemin> <contenu>
listed() {   # listed <ligne> <liste> : appartenance exacte, sans pipe (pas de faux négatif SIGPIPE sous pipefail)
  case $'\n'"$2"$'\n' in *$'\n'"$1"$'\n'*) return 0 ;; esac
  return 1
}
in_scope() {   # chemins qu'OpenTV a le droit de retirer (même périmètre que uninstall.sh)
  case "$1" in
    /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) return 0 ;;
    /etc/systemd/logind.conf.d/opentv-kiosk.conf|/etc/chromium/policies/managed/opentv-kiosk.json) return 0 ;;
    /etc/chromium-browser/policies/managed/opentv-kiosk.json) return 0 ;;
    /etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- fichiers livrés (source|destination|mode)
FILES=()
add() { FILES+=("$1|$2|$3"); }
for f in "$HERE"/opentv_pi/*.py; do add "$f" "/opt/opentv/lib/opentv_pi/$(basename "$f")" 644; done
for b in opentv-pi opentv-mcp opentv-net-helper opentv-kiosk-browser opentv-player opentv-player-keys; do add "$HERE/bin/$b" "/opt/opentv/bin/$b" 755; done
# démarrage produit : écran OpenTV sur tty1 + contrôle en lecture seule après redémarrage
add "$HERE/system/opentv-splash" /opt/opentv/bin/opentv-splash 755
add "$HERE/system/opentv-boot-check" /opt/opentv/bin/opentv-boot-check 755
for u in tv.html tv.js keys.js tv.css setup.css setup.js remote.css remote.js fallback.html; do add "$HERE/ui/$u" "/opt/opentv/share/ui/$u" 644; done
add "$REPO/native/app/qrcode.js" /opt/opentv/share/ui/qrcode.js 644                  # réutilisé tel quel
add "$REPO/scripts/nanopi/opentv_foryou.py" /opt/opentv/lib/foryou/opentv_foryou.py 644  # moteur « Pour toi »
add "$REPO/scripts/nanopi/opentv_bridge.py" /opt/opentv/lib/foryou/opentv_bridge.py 644  # load_catalogue_meta
for u in opentv.service opentv-kiosk.service opentv-net-helper.service opentv-net-helper.path \
         opentv-ap-guard.service opentv-ap-guard.timer opentv-cec.service opentv-cec.timer \
         opentv-cec-announce.service opentv-splash.service opentv-player.service opentv-player-keys.service; do
  add "$HERE/systemd/$u" "/etc/systemd/system/$u" 644
done
# aucune invite de connexion sur tty1 tant que opentv-kiosk.service est activé (condition seule, rien de masqué)
add "$HERE/system/getty-tty1-opentv-kiosk.conf" /etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf 644
add "$HERE/system/sysusers-opentv.conf" /etc/sysusers.d/opentv.conf 644
add "$HERE/system/tmpfiles-opentv.conf" /etc/tmpfiles.d/opentv.conf 644
add "$HERE/system/pam-opentv-kiosk" /etc/pam.d/opentv-kiosk 644
# kiosque durci, fichiers dédiés et réversibles : touches Power/Reboot/Suspend ignorées par logind (la boîte ne
# s'éteint pas sur la touche Power d'une télécommande), politique Chromium gérée (DevTools, impression,
# téléchargements et navigation hors de 127.0.0.1 bloqués) aux deux emplacements possibles de Raspberry Pi OS
add "$HERE/system/logind-opentv-kiosk.conf" /etc/systemd/logind.conf.d/opentv-kiosk.conf 644
add "$HERE/system/chromium-policy-opentv-kiosk.json" /etc/chromium/policies/managed/opentv-kiosk.json 644
add "$HERE/system/chromium-policy-opentv-kiosk.json" /etc/chromium-browser/policies/managed/opentv-kiosk.json 644
# « Regarder » : politique Chromium du LECTEUR, montée par opentv-player.service seulement (jamais dans /etc)
add "$HERE/system/chromium-policy-opentv-player.json" /opt/opentv/share/chromium-player-policy/opentv-player.json 644
add "$HERE/uninstall.sh" /opt/opentv/uninstall.sh 755
add "$HERE/README.md" /opt/opentv/README.md 644
add "$HERE/CEC_COMPATIBILITY.md" /opt/opentv/CEC_COMPATIBILITY.md 644
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
# cmdline.txt n'est jamais écrit dans le dossier /boot/firmware de la partition racine (partition de démarrage absente)
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ] && command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q /boot/firmware; then
  die "/boot/firmware n'est pas monté : installation refusée, aucun fichier modifié (sudo mount /boot/firmware)"
fi

# ---------------------------------------------------------------- copie avec sauvegardes et retour arrière
TS="$(date -u +%Y%m%dT%H%M%SZ)"
BK="/var/lib/opentv-install/backup-$TS-$$"   # unique : deux exécutions dans la même seconde ne se mélangent pas
MANIFEST="/opt/opentv/MANIFEST.sha256"
CMDF="/boot/firmware/cmdline.txt"
VIDEO_ARG="video=HDMI-A-1:1920x1080@60"
VIDEO_REC="/var/lib/opentv-install/cmdline-video.added"
# démarrage silencieux : affichage CONSOLE seulement (le journal persistant, dmesg et journalctl -k gardent tout ;
# console=tty1, l'invite du mode de secours et SSH sont conservés). Aucune option udev/systemd.log_level (journal).
BOOT_ARGS=(quiet loglevel=3 logo.nologo vt.global_cursor_default=0 systemd.show_status=false)
BOOT_REC="/var/lib/opentv-install/cmdline-boot.added"
OLD_LIST=""
if [ -f "$(dest "$MANIFEST")" ]; then OLD_LIST="$(awk '{print $2}' "$(dest "$MANIFEST")")"; fi
# dossiers système qu'OpenTV peut avoir à créer (hors /opt/opentv) : seuls ceux créés ici sont notés, et retirés
# (s'ils sont vides) par uninstall.sh — un dossier livré par la distribution n'est jamais supprimé
EXTRA_DIRS="/etc/systemd/logind.conf.d
/etc/chromium
/etc/chromium/policies
/etc/chromium/policies/managed
/etc/chromium-browser
/etc/chromium-browser/policies
/etc/chromium-browser/policies/managed
/etc/systemd/system/getty@tty1.service.d"
DIRS_REC="/var/lib/opentv-install/created-dirs.list"
NEW_DIRS=()
while IFS= read -r d; do
  [ -n "$d" ] || continue
  if [ ! -e "$(dest "$d")" ] && [ ! -L "$(dest "$d")" ]; then NEW_DIRS+=("$d"); fi
done <<EOF
$EXTRA_DIRS
EOF
DIRS_REC_CHANGED=0
DIRS_REC_OLD=""
OPT_EXISTED=0   # /opt/opentv absent avant cette exécution ⇒ ses dossiers vides sont retirés par le retour arrière
if [ -e "$(dest /opt/opentv)" ] || [ -L "$(dest /opt/opentv)" ]; then OPT_EXISTED=1; fi
NEW=()
SAVED=()
REMOVED=()
STARTED=0
CUR=""
CMDLINE_CHANGED=0
VIDEO_REC_NEW=0
BOOT_REC_CHANGED=0
BOOT_REC_OLD=""

rollback() {
  set +e
  trap - ERR
  [ "$DRY" = 1 ] && exit 1
  if [ "$STARTED" = 1 ]; then
    echo "opentv-install: échec — retour à l'état précédent" >&2
    local d
    [ -n "$CUR" ] && rm -f -- "$(dest "$CUR").opentv-tmp"
    rm -f -- "$(dest "$MANIFEST").tmp" "$(dest "$CMDF").opentv-tmp" "$(dest "$VIDEO_REC").opentv-tmp" \
      "$(dest "$BOOT_REC").opentv-tmp"
    for d in ${NEW[@]+"${NEW[@]}"}; do rm -f -- "$(dest "$d")"; done
    for d in ${SAVED[@]+"${SAVED[@]}"} ${REMOVED[@]+"${REMOVED[@]}"}; do
      mkdir -p -- "$(dirname "$(dest "$d")")"
      cp -p -- "$(dest "$BK$d")" "$(dest "$d")" || echo "opentv-install: restauration impossible : $d" >&2
    done
    if [ "$CMDLINE_CHANGED" = 1 ]; then   # même écriture durable qu'à l'aller : jamais de cmdline.txt tronqué
      if ! { cp -p -- "$(dest "$BK$CMDF")" "$(dest "$CMDF").opentv-tmp" &&
             replace_durably "$(dest "$CMDF").opentv-tmp" "$(dest "$CMDF")"; }; then
        rm -f -- "$(dest "$CMDF").opentv-tmp"
        echo "opentv-install: restauration impossible : $CMDF (sauvegarde : $BK$CMDF)" >&2
      fi
    fi
    # marqueurs notés AVANT cmdline.txt : retirés/rétablis (aucun marqueur périmé après un échec)
    [ "$VIDEO_REC_NEW" = 1 ] && rm -f -- "$(dest "$VIDEO_REC")"
    if [ "$BOOT_REC_CHANGED" = 1 ]; then
      if [ -n "$BOOT_REC_OLD" ]; then put_durably "$(dest "$BOOT_REC")" "$BOOT_REC_OLD"; else rm -f -- "$(dest "$BOOT_REC")"; fi
      rm -f -- "$(dest "$BOOT_REC").opentv-tmp"
    fi
    if [ "$DIRS_REC_CHANGED" = 1 ]; then
      if [ -n "$DIRS_REC_OLD" ]; then printf '%s\n' "$DIRS_REC_OLD" > "$(dest "$DIRS_REC")"; else rm -f -- "$(dest "$DIRS_REC")"; fi
    fi
    if [ "$OPT_EXISTED" = 0 ] && [ -d "$(dest /opt/opentv)" ] && [ ! -L "$(dest /opt/opentv)" ]; then
      find "$(dest /opt/opentv)" -depth -type d -empty -exec rmdir -- {} \; 2>/dev/null
    fi
    local i
    i=${#NEW_DIRS[@]}
    while [ "$i" -gt 0 ]; do   # du plus profond au plus haut ; vides seulement
      i=$((i - 1))
      rmdir -- "$(dest "${NEW_DIRS[$i]}")" 2>/dev/null || true
    done
  fi
  exit 1
}
trap rollback ERR
fail() { echo "opentv-install: $*" >&2; rollback; }   # erreur détectée : même retour arrière qu'un échec de commande

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

# ---------------------------------------------------------------- cmdline.txt : 1080p + démarrage silencieux (réversible)
# Une seule ligne modifiée (la première, seule lue par le firmware), une seule écriture atomique, une seule sauvegarde.
# Un jeton dont la clé est déjà présente (ex. loglevel=7 choisi localement) est respecté et jamais noté. Fin de ligne
# (LF, CRLF ou absente) et lignes suivantes conservées octet pour octet.
has_arg() {   # has_arg <ligne> <jeton> : même jeton, ou même clé (« clé=… » ou « clé » seule), quelle que soit la valeur
  local k="${2%%=*}"
  case " $1 " in *" $2 "*|*" $k "*|*" $k="*) return 0 ;; esac
  return 1
}
local_verbosity() {   # debug / loglevel=… déjà présents : quiet ou loglevel=3 ajoutés en fin de ligne les écraseraient
  local w v=""
  set -f
  for w in $1; do case "$w" in debug|loglevel=*) v="${v:+$v }$w" ;; esac; done
  set +f
  printf '%s' "$v"
}
C="$(dest "$CMDF")"
ADD_ARGS=()
if [ ! -f "$C" ]; then
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then fail "$CMDF introuvable : Raspberry Pi OS Bookworm requis"; fi
  say "cmdline.txt absent ($ROOT) : 1080p et démarrage silencieux non appliqués"
else
  CMD_FIRST=""
  CMD_CR=""
  IFS= read -r CMD_FIRST < "$C" || true
  case "$CMD_FIRST" in *$'\r') CMD_CR=$'\r'; CMD_FIRST="${CMD_FIRST%$'\r'}" ;; esac
  case " $CMD_FIRST " in *" root="*) ;; *) fail "$CMDF : première ligne sans root=… (fichier inattendu), non modifié" ;; esac
  case " $CMD_FIRST " in
    *" video=HDMI-A-1:"*) say "cmdline.txt : video=HDMI-A-1 déjà présent, inchangé" ;;
    *) ADD_ARGS+=("$VIDEO_ARG") ;;
  esac
  VERBOSE="$(local_verbosity "$CMD_FIRST")"
  for a in "${BOOT_ARGS[@]}"; do
    if has_arg "$CMD_FIRST" "$a"; then say "cmdline.txt : ${a%%=*} déjà réglé, inchangé"
    elif [ -n "$VERBOSE" ] && { [ "$a" = quiet ] || [ "$a" = loglevel=3 ]; }; then
      say "cmdline.txt : verbosité locale ($VERBOSE) respectée, $a non ajouté (il l'écraserait)"
    else ADD_ARGS+=("$a"); fi
  done
fi
if [ ${#ADD_ARGS[@]} -gt 0 ]; then
  NEW_FIRST="$CMD_FIRST"
  for a in "${ADD_ARGS[@]}"; do NEW_FIRST="${NEW_FIRST:+$NEW_FIRST }$a"; done
  [ ${#NEW_FIRST} -le 1024 ] || fail "$CMDF : ligne trop longue après ajout (${#NEW_FIRST} > 1024), non modifié"
  if [ "$DRY" = 1 ]; then
    say "[dry-run] ajouter ${ADD_ARGS[*]} à $CMDF (sauvegarde $BK$CMDF ; jetons notés, retirés par uninstall.sh)"
  else
    STARTED=1
    backup "$CMDF"
    durable_sync "$(dest "$BK$CMDF")"
    # 1) jetons notés et synchronisés AVANT toute modification : une coupure ne laisse jamais un jeton OpenTV non noté
    #    (un marqueur sans jeton est sans effet : uninstall.sh ne retire que des jetons exacts présents)
    mkdir -p -- "$(dirname "$(dest "$BOOT_REC")")"
    if [ -f "$(dest "$BOOT_REC")" ]; then BOOT_REC_OLD="$(cat "$(dest "$BOOT_REC")")"; fi
    BOOT_REC_NEXT="$BOOT_REC_OLD"
    for a in "${ADD_ARGS[@]}"; do
      if [ "$a" = "$VIDEO_ARG" ]; then
        if [ ! -f "$(dest "$VIDEO_REC")" ]; then
          VIDEO_REC_NEW=1   # noté avant l'écriture : un échec en cours d'écriture est aussi annulé
          put_durably "$(dest "$VIDEO_REC")" "$VIDEO_ARG"
        fi
      elif ! listed "$a" "$BOOT_REC_NEXT"; then
        BOOT_REC_NEXT="${BOOT_REC_NEXT:+$BOOT_REC_NEXT$'\n'}$a"
      fi
    done
    if [ "$BOOT_REC_NEXT" != "$BOOT_REC_OLD" ]; then
      BOOT_REC_CHANGED=1
      put_durably "$(dest "$BOOT_REC")" "$BOOT_REC_NEXT"
    fi
    # 2) nouvelle ligne dans un fichier temporaire du même dossier, synchronisé, renommé, puis cible + dossier synchronisés
    CMD_NL=""
    [ "$(wc -l < "$C" | tr -d ' ')" -gt 0 ] && CMD_NL=$'\n'
    {
      printf '%s%s%s' "$NEW_FIRST" "$CMD_CR" "$CMD_NL"
      tail -n +2 "$C"
    } > "$C.opentv-tmp"
    chmod "$(stat -c %a "$C" 2>/dev/null || stat -f %Lp "$C")" "$C.opentv-tmp"
    durable_sync "$C.opentv-tmp"
    CMDLINE_CHANGED=1
    mv -f -- "$C.opentv-tmp" "$C"
    durable_sync "$C" "$(dirname "$C")"
    say "cmdline.txt : ${ADD_ARGS[*]} ajouté(s) — effectif au prochain redémarrage (sudo reboot)"
  fi
fi

# dossiers système créés par cette installation : notés pour une désinstallation exacte
if [ ${#NEW_DIRS[@]} -gt 0 ]; then
  if [ "$DRY" = 1 ]; then
    say "[dry-run] dossiers créés (notés dans $DIRS_REC) : ${NEW_DIRS[*]}"
  else
    STARTED=1
    mkdir -p -- "$(dirname "$(dest "$DIRS_REC")")"
    if [ -f "$(dest "$DIRS_REC")" ]; then DIRS_REC_OLD="$(cat "$(dest "$DIRS_REC")")"; fi
    DIRS_REC_CHANGED=1
    for d in "${NEW_DIRS[@]}"; do
      if ! listed "$d" "$DIRS_REC_OLD"; then printf '%s\n' "$d" >> "$(dest "$DIRS_REC")"; fi
    done
  fi
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
  sync   # fichiers livrés, marqueurs et manifeste sur le support avant les étapes système
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
# ancienne appartenance opentv-kiosk ∈ input (versions précédentes) retirée : seul opentv-kiosk.service rend input à
# Cage ; le contrôleur du lecteur et son Chromium (même compte) ne doivent jamais l'hériter. Idempotent.
drop_kiosk_input() {
  local line
  line="$(getent group input 2>/dev/null)" || return 0
  case ",${line##*:}," in
    *,opentv-kiosk,*) gpasswd -d opentv-kiosk input >/dev/null ;;
  esac
}
post systemd-sysusers /etc/sysusers.d/opentv.conf
post drop_kiosk_input
post systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf
post chown root:opentv "$ENVF"
post systemctl daemon-reload
if [ "$ENABLE" = 1 ]; then
  # opentv-ap-guard.timer : garde root du Wi-Fi temporaire, indépendante du service Python
  # opentv-cec.timer : déclaration Playback « OpenTV » + remappage OK + UNE annonce de source active par démarrage
  # (fenêtre bornée, TV allumée seulement ; jamais de réveil ni de réannonce). Réactivation : opentv-pi cec --activate
  # opentv-splash.service : écran OpenTV sur tty1 au démarrage (sysinit), jamais redémarré ici (Cage possède tty1)
  # opentv-player.service + opentv-player-keys.service : contrôleur du lecteur « Regarder » et aide des touches.
  # Activés mais SANS capacité de lecture : « Regarder » exige la source approuvée (/etc/opentv/source.env) ET le
  # témoin de promotion terrain /etc/opentv/playback-promoted — ni l'un ni l'autre n'est jamais livré, écrit ni
  # écrasé par l'installation (opentv-pi playback promote --field-validated, après la validation HDMI réelle).
  post systemctl enable opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service \
    opentv-cec.timer opentv-splash.service opentv-player-keys.service opentv-player.service
  post systemctl restart opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service \
    opentv-cec.timer opentv-player-keys.service opentv-player.service
fi
if ! host python3 -I /opt/opentv/bin/opentv-pi health; then
  say "santé : à vérifier (sudo /opt/opentv/bin/opentv-pi health ; journalctl -u opentv -u opentv-kiosk)"
fi
if [ "$CMDLINE_CHANGED" = 1 ]; then say "cmdline.txt : redémarrer la boîte pour appliquer ${ADD_ARGS[*]}"; fi
if [ -e "$(dest /etc/opentv/playback-promoted)" ]; then
  say "« Regarder » : témoin de promotion présent ; lié à la livraison installée entière (manifeste) : cette installation le rend caduc s'il a changé un fichier livré — nouvelle validation terrain puis sudo /opt/opentv/bin/opentv-pi playback promote --field-validated"
else
  say "« Regarder » : NON promu (aucun bouton) — après la validation HDMI réelle : sudo /opt/opentv/bin/opentv-pi playback promote --field-validated"
fi
say "démarrage : écran OpenTV sur tty1, sans terminal ni invite (effectif au prochain redémarrage ; contrôle : sudo /opt/opentv/bin/opentv-boot-check)"
say "touche Power des télécommandes ignorée par la boîte (/etc/systemd/logind.conf.d/opentv-kiosk.conf) : effectif au prochain redémarrage (sudo reboot)"
say "terminé. Retour arrière : sudo /opt/opentv/uninstall.sh   (ajouter --purge pour effacer l'état)"
