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
# tant que le kiosque est activé. Image OpenTV de bout en bout : arc-en-ciel du firmware retiré (disable_splash=1 noté
# dans config.txt) et, si Plymouth est installé (prepare-pi.sh), thème « opentv » + splash ; initramfs du noyau v8
# exact régénéré en transaction (sauvegarde SHA-256, vérifié, octets rendus si échec) ; sinon repli texte tty1.
# Mise à jour : fichiers de l'ancien manifeste absents du nouveau retirés (sauvegardés),
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
  sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
# bootcmd : régénération/lecture de l'initramfs. Vraie racine : exécutée. Racine de test : seulement si
# OPENTV_INSTALL_TEST_BOOTCMDS=1 (faux outils du PATH, OPENTV_TEST_ROOT exporté), sinon affichée.
bootcmd() {
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then "$@"
  elif [ "$DRY" = 1 ]; then say "[dry-run] $*"
  elif [ "${OPENTV_INSTALL_TEST_BOOTCMDS:-}" = 1 ]; then OPENTV_TEST_ROOT="$ROOT" "$@"
  else say "[racine de test, non exécuté] $*"; fi
}
bounded() {   # bounded <secondes> <commande…> : jamais d'attente illimitée (GNU timeout, puis KILL 30 s après)
  local s="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then timeout -k 30 "$s" "$@"; else "$@"; fi
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
hexof() { od -An -tx1 -v | tr -d ' \n'; }                # stdin -> hexadécimal (octets exacts, CR compris)
tailhex() { tail -c "$2" -- "$1" | hexof; }               # tailhex <fichier> <n> : n derniers octets
unhex() { local h="$1" o=""; while [ -n "$h" ]; do o="$o\\x${h:0:2}"; h="${h:2}"; done; printf '%b' "$o"; }
# safe_text <fichier> <max> : fichier régulier (pas un lien), au plus <max> octets, sans octet nul. Sinon refus.
safe_text() {
  local n z
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  n="$(wc -c < "$1" | tr -d ' ')" || return 1
  [ "$n" -le "$2" ] || return 1
  z="$(tr -d '\000' < "$1" | wc -c | tr -d ' ')" || return 1
  [ "$z" = "$n" ]
}
file_mode() { stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1"; }
# Plymouth refusé localement : nosplash, plymouth.enable=0|false|no|off, rd.plymouth=0|false|no|off (respecté)
ply_refused() {
  local w v
  set -f
  for w in $1; do
    case "$w" in
      nosplash) set +f; return 0 ;;
      plymouth.enable=*|rd.plymouth=*)
        v="$(printf '%s' "${w#*=}" | tr '[:upper:]' '[:lower:]')"
        case "$v" in 0|n|no|false|off) set +f; return 0 ;; esac ;;
    esac
  done
  set +f
  return 1
}
# transaction initramfs (sauvegarde durable, régénération du noyau exact, vérification, restauration à l'octet)
# shellcheck source-path=SCRIPTDIR source=system/opentv-initramfs.sh
. "$HERE/system/opentv-initramfs.sh"
ir_init
in_scope() {   # chemins qu'OpenTV a le droit de retirer (même périmètre que uninstall.sh)
  case "$1" in
    /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) return 0 ;;
    /etc/systemd/logind.conf.d/opentv-kiosk.conf|/etc/chromium/policies/managed/opentv-kiosk.json) return 0 ;;
    /etc/chromium-browser/policies/managed/opentv-kiosk.json) return 0 ;;
    /etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf) return 0 ;;
    /usr/share/plymouth/themes/opentv/*) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------- fichiers livrés (source|destination|mode)
FILES=()
add() { FILES+=("$1|$2|$3"); }
for f in "$HERE"/opentv_pi/*.py; do add "$f" "/opt/opentv/lib/opentv_pi/$(basename "$f")" 644; done
for b in opentv-pi opentv-mcp opentv-net-helper opentv-kiosk-browser opentv-player opentv-player-keys opentv-muse-bridge; do add "$HERE/bin/$b" "/opt/opentv/bin/$b" 755; done
# pont Muse : sous-ensemble MINIMAL du SDK Linux officiel (transport + appariement), octets identiques au commit épinglé
# (vendor/muse/PIN.json : commit, archive officielle SHA-256, empreinte de chaque fichier ; jamais @main). Ni
# executor.py, ni fileops.py, ni service.py, ni cli.py, ni l'unité musegadget.service d'origine ne sont livrés.
while IFS= read -r f; do
  rel="${f#"$HERE/vendor/muse/"}"
  add "$f" "/opt/opentv/lib/muse/$rel" 644
done < <(find "$HERE/vendor/muse" -type f ! -path '*/__pycache__/*' ! -name '*.pyc' | LC_ALL=C sort)
# dépendance websockets du SDK : roue pure Python épinglée par SHA-256 (= requirements.lock du même commit), déposée
# par prepare-pi.sh (seule étape réseau) dans /var/cache/opentv/muse ; jamais téléchargée ici. Absente : OpenTV est
# installé, le pont Muse reste indisponible (opentv-pi muse status) ; empreinte fausse : installation refusée.
MUSE_WHEEL="websockets-17.1-py3-none-any.whl"
MUSE_WHEEL_SHA256="f221081107b8c48184d99f7019604486376e7ef826037e70aad6b02540732c23"
if [ "$LIVE" = 0 ] && [ -n "${OPENTV_INSTALL_TEST_MUSE_WHEEL_SHA256:-}" ]; then   # racines de test seulement
  MUSE_WHEEL_SHA256="$OPENTV_INSTALL_TEST_MUSE_WHEEL_SHA256"
fi
MUSE_CACHE="/var/cache/opentv/muse/$MUSE_WHEEL"
MUSE_DEP=""
for cand in "$(dest "$MUSE_CACHE")" "$(dest "/opt/opentv/lib/muse/$MUSE_WHEEL")"; do
  if [ -e "$cand" ] || [ -L "$cand" ]; then
    { [ -f "$cand" ] && [ ! -L "$cand" ]; } || die "$cand : fichier inattendu (lien ou non régulier), installation refusée, rien modifié"
    [ "$(sha256 "$cand")" = "$MUSE_WHEEL_SHA256" ] || die "$cand : SHA-256 différent de l'épinglage ($MUSE_WHEEL_SHA256), installation refusée, rien modifié"
    MUSE_DEP="$cand"
    break
  fi
done
if [ -n "$MUSE_DEP" ]; then
  add "$MUSE_DEP" "/opt/opentv/lib/muse/$MUSE_WHEEL" 644
fi
# démarrage produit : écran OpenTV sur tty1 + contrôle en lecture seule après redémarrage
add "$HERE/system/opentv-splash" /opt/opentv/bin/opentv-splash 755
add "$HERE/system/opentv-boot-check" /opt/opentv/bin/opentv-boot-check 755
# image OpenTV Plymouth (thème livré toujours, activé seulement si Plymouth est installé) + passage au kiosque
add "$HERE/system/opentv-plymouth-handoff" /opt/opentv/bin/opentv-plymouth-handoff 755
# transaction initramfs partagée avec /opt/opentv/uninstall.sh (sauvegarde/restauration à l'octet)
add "$HERE/system/opentv-initramfs.sh" /opt/opentv/lib/opentv-initramfs.sh 644
for t in opentv.plymouth opentv.script opentv.png opentv-mask.png; do
  add "$HERE/system/plymouth/$t" "/usr/share/plymouth/themes/opentv/$t" 644
done
for u in tv.html tv.js keys.js canvas.js tv.css setup.css setup.js remote.css remote.js muse.js fallback.html; do add "$HERE/ui/$u" "/opt/opentv/share/ui/$u" 644; done
add "$REPO/native/app/qrcode.js" /opt/opentv/share/ui/qrcode.js 644                  # réutilisé tel quel
add "$REPO/scripts/nanopi/opentv_foryou.py" /opt/opentv/lib/foryou/opentv_foryou.py 644  # moteur « Pour toi »
add "$REPO/scripts/nanopi/opentv_bridge.py" /opt/opentv/lib/foryou/opentv_bridge.py 644  # load_catalogue_meta
add "$REPO/scripts/nanopi/opentv_tmdb.py" /opt/opentv/lib/foryou/opentv_tmdb.py 644      # notes TMDb (tel quel)
for u in opentv.service opentv-kiosk.service opentv-net-helper.service opentv-net-helper.path \
         opentv-ap-guard.service opentv-ap-guard.timer opentv-cec.service opentv-cec.timer \
         opentv-cec-announce.service opentv-cec-return.service opentv-splash.service opentv-player.service \
         opentv-player-keys.service \
         opentv-catalogue-refresh.service opentv-catalogue-refresh.timer opentv-catalogue-ratings.service \
         opentv-catalogue-ratings-retry.service opentv-catalogue-ratings-retry.timer \
         opentv-plymouth-deactivate.service opentv-plymouth-quit.service opentv-plymouth-failsafe.service \
         opentv-plymouth-failsafe.timer opentv-muse.service opentv-muse-pair.service \
         opentv-muse-setup.path opentv-muse-setup.service; do
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
# modèle SANS secret du jeton TMDb ; le vrai /etc/opentv/tmdb/token (root:opentv 0640) n'est jamais livré, écrit,
# manifesté ni retiré par l'installation (sudo /opt/opentv/bin/opentv-pi catalogue ratings-credential). Son dossier
# /etc/opentv/tmdb (root:opentv 0750) est créé par tmpfiles AVANT le (re)démarrage des unités qui le masquent.
add "$HERE/tmdb-token.example" /opt/opentv/tmdb-token.example 644
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
# image OpenTV Plymouth : options ajoutées seulement si Plymouth est utilisable (notées dans BOOT_REC, retirées
# exactement) ; plymouth.ignore-serial-consoles : console=serial0 + console=tty1 n'empêchent plus l'image
PLY_ARGS=(splash plymouth.ignore-serial-consoles)
# arc-en-ciel du firmware : disable_splash=1 ajouté EN FIN de config.txt (octets exacts notés, retirés exactement)
CFGF="/boot/firmware/config.txt"
CFG_REC="/var/lib/opentv-install/config-splash.added"
# thème Plymouth : Theme=opentv dans /etc/plymouth/plymouthd.conf (ancien contenu sauvegardé, thème précédent noté)
PLY_CONF="/etc/plymouth/plymouthd.conf"
PLY_REC="/var/lib/opentv-install/plymouth-theme.added"
PLY_ORIG="/var/lib/opentv-install/plymouthd.conf.orig"
PLY_THEME_DIR="/usr/share/plymouth/themes/opentv"
INITRD="/boot/firmware/initramfs8"   # initramfs chargé par le firmware (Pi 4, noyau 64 bits, auto_initramfs=1)
# initramfs régénéré par OpenTV (thème livré, Theme=opentv ou jetons splash), MÊME si Theme=opentv était déjà choisi :
# noté durablement AVANT la régénération ; uninstall.sh régénère dès que ce marqueur existe (aucune charge OpenTV
# périmée dans initramfs8) et ne l'efface qu'après une régénération vérifiée
INITRD_REC="/var/lib/opentv-install/initramfs-plymouth.rebuilt"
INITRD_REC_LINE="rebuilt=opentv-plymouth"
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
/etc/systemd/system/getty@tty1.service.d
/usr/share/plymouth
/usr/share/plymouth/themes
/usr/share/plymouth/themes/opentv"
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
CFG_CHANGED=0
CFG_REC_CHANGED=0
CFG_REC_OLD=""
PLYCONF_CHANGED=0
PLYCONF_MODE=""
PLY_REC_CHANGED=0
PLY_REC_OLD=""
PLY_ORIG_OLD=0
IR_TXN_OPEN=0       # sauvegarde initramfs + journal en place : tout échec restaure les octets sauvegardés
IR_LIVE_WRITER=0    # régénération encore active après SIGTERM + délai : aucune restauration concurrente
IR_BK="$BK/initramfs"
INITRD_REC_CHANGED=0
INITRD_REC_OLD=""
INITRD_REC_EXISTED=0
# restore_durably <sauvegarde> <cible> : même écriture durable qu'à l'aller (temp + sync + renommage + sync)
restore_durably() {
  cp -p -- "$1" "$2.opentv-tmp" && replace_durably "$2.opentv-tmp" "$2"
}
initramfs_rebuild() {   # noyau v8 exact seulement (jamais -k all) ; SIGTERM borné, jamais SIGKILL de la copie
  ir_rebuild "$IR_KERNEL"
}

# ---------------------------------------------------------------- lectures préalables (aucune écriture)
# cfg_scan <config.txt> : valeur EFFECTIVE (dernière) de disable_splash / auto_initramfs dans les sections qui
# s'appliquent toujours sur la Pi 4 (avant tout filtre, [all], [pi4]) ; valeurs ≠ 1 de disable_splash où qu'elles
# soient (choix local respecté) ; CFG_LAST_ALL=1 si une ligne ajoutée en fin de fichier s'applique sans condition.
cfg_scan() {
  local l name v sec=all
  CFG_LAST_ALL=1; CFG_SPLASH=""; CFG_SPLASH_LOCAL=""; CFG_AUTOINIT=""; CFG_INITRD=0
  while IFS= read -r l || [ -n "$l" ]; do
    l="${l%$'\r'}"
    l="${l#"${l%%[![:space:]]*}"}"
    case "$l" in
      ''|'#'*) ;;
      '['*']'*)
        name="${l#[}"
        name="$(printf '%s' "${name%%]*}" | tr '[:upper:]' '[:lower:]')"
        case "$name" in all) sec=all; CFG_LAST_ALL=1 ;; pi4) sec=pi4; CFG_LAST_ALL=0 ;; *) sec=cond; CFG_LAST_ALL=0 ;; esac ;;
      disable_splash=*|disable_splash[[:space:]]*)
        v="${l#*=}"; v="${v%"${v##*[![:space:]]}"}"; v="${v#"${v%%[![:space:]]*}"}"
        [ "$sec" = cond ] || CFG_SPLASH="$v"
        [ "$v" = 1 ] || CFG_SPLASH_LOCAL="${CFG_SPLASH_LOCAL:+$CFG_SPLASH_LOCAL }disable_splash=$v" ;;
      auto_initramfs=*)
        v="${l#*=}"; v="${v%"${v##*[![:space:]]}"}"
        [ "$sec" = cond ] || CFG_AUTOINIT="$v" ;;
      initramfs[[:space:]]*) [ "$sec" = cond ] || CFG_INITRD=1 ;;
    esac
  done < "$1"
}
CF="$(dest "$CFGF")"
CFG_PRESENT=0
if [ -e "$CF" ] || [ -L "$CF" ]; then
  CFG_PRESENT=1
  safe_text "$CF" 65536 || die "$CFGF : fichier inattendu (lien, non régulier, > 64 Kio ou octet nul), installation refusée, rien modifié"
  cfg_scan "$CF"
elif [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then
  die "$CFGF introuvable : Raspberry Pi OS Bookworm requis, rien modifié"
fi
PC="$(dest "$PLY_CONF")"
if [ -e "$PC" ] || [ -L "$PC" ]; then
  safe_text "$PC" 65536 || die "$PLY_CONF : fichier inattendu (lien, non régulier, > 64 Kio ou octet nul), installation refusée, rien modifié"
fi
IR="$(dest "$INITRD_REC")"
if [ -e "$IR" ] || [ -L "$IR" ]; then
  safe_text "$IR" 4096 || die "$INITRD_REC : marqueur inattendu (lien, non régulier, > 4 Kio ou octet nul), installation refusée, rien modifié"
fi
# ---------------------------------------------------------------- transaction initramfs interrompue : rejouée d'abord
# Un journal initramfs.txn resté (coupure ou arrêt brutal pendant une régénération) : octets d'avant restaurés et
# vérifiés AVANT toute autre action — et AVANT le choix du noyau : un initramfs8 tronqué ne correspond à aucun
# initrd.img-<v> et ferait choisir à tort le v8 le plus récent. L'image OpenTV est ensuite régénérée pour le noyau
# exact de l'initramfs8 rétabli (noyau du journal préféré seulement s'il est validé et identique). Échec : rien modifié.
IR_TXN_PENDING=0
if [ -e "$(dest "$IR_TXN")" ] || [ -L "$(dest "$IR_TXN")" ]; then
  if [ "$DRY" = 1 ]; then
    IR_TXN_PENDING=1   # dry-run : rejeu puis régénération forcée annoncés (rien écrit)
    if ir_regular "$(dest "$IR_TXN")"; then ir_txn_read "$(dest "$IR_TXN")"; fi
    say "[dry-run] transaction initramfs interrompue ($IR_TXN) : octets d'avant restaurés puis vérifiés en premier${IR_TXN_KERNEL:+ (noyau noté : $IR_TXN_KERNEL, revérifié après rétablissement)}"
  elif ir_recover; then
    say "initramfs : transaction interrompue rejouée, $INITRD rétabli octet pour octet"
  else
    die "transaction initramfs interrompue non rétablie ($IR_TXN conservé) : installation refusée, rien d'autre modifié"
  fi
fi

# plymouth_missing : première condition manquante pour une image Plymouth fiable (vide = utilisable). Les outils sont
# cherchés dans la racine visée (fichiers Debian/Raspberry Pi OS) ; aucun paquet n'est installé ici (prepare-pi.sh).
plymouth_missing() {
  local so found=0
  [ -x "$(dest /usr/sbin/plymouthd)" ] || { printf 'plymouthd absent (sudo ./prepare-pi.sh --yes)'; return; }
  [ -x "$(dest /usr/bin/plymouth)" ] || { printf 'client plymouth absent'; return; }
  for so in "$(dest /usr/lib)"/*/plymouth/script.so "$(dest /usr/lib)/plymouth/script.so"; do [ -f "$so" ] && found=1; done
  [ "$found" = 1 ] || { printf 'module script de Plymouth absent'; return; }
  [ -d "$(dest /etc/plymouth)" ] || { printf '/etc/plymouth absent'; return; }
  [ -f "$(dest /usr/share/initramfs-tools/hooks/plymouth)" ] || { printf 'crochet initramfs de Plymouth absent'; return; }
  [ -x "$(dest /usr/sbin/update-initramfs)" ] || { printf 'update-initramfs absent'; return; }
  [ -x "$(dest /usr/bin/lsinitramfs)" ] || { printf 'lsinitramfs absent (vérification impossible)'; return; }
  [ "$CFG_PRESENT" = 1 ] || { printf 'config.txt absent'; return; }
  { [ "$CFG_AUTOINIT" = 1 ] || [ "$CFG_INITRD" = 1 ]; } || { printf 'initramfs non chargé par le firmware (auto_initramfs=1 absent)'; return; }
  [ -f "$(dest "$INITRD")" ] || { printf '%s absent' "$INITRD"; return; }
  ir_pick_kernel || { printf 'aucun noyau v8 complet (/boot/initrd.img-<version>-v8 et modules) pour %s' "$INITRD"; return; }
}
PLY_WHY="$(plymouth_missing)"
PLY_OK=0
if [ -z "$PLY_WHY" ]; then
  PLY_OK=1
  # noyau exact (dans ce shell, APRÈS le rejeu du journal) : initrd.img-<v> identique à initramfs8 (noyau du journal
  # rejoué préféré s'il est validé et identique), sinon le v8 le plus récent
  ir_pick_kernel "$IR_TXN_KERNEL"
  # dry-run avec journal resté : initramfs8 encore tronqué ici, noyau noté (validé) annoncé à la place
  if [ "$DRY" = 1 ] && [ -n "$IR_TXN_KERNEL" ]; then IR_KERNEL="$IR_TXN_KERNEL"; fi
fi

rollback() {
  set +e
  trap - ERR
  [ "$DRY" = 1 ] && exit 1
  if [ "$STARTED" = 1 ]; then
    echo "opentv-install: échec — retour à l'état précédent" >&2
    local d
    [ -n "$CUR" ] && rm -f -- "$(dest "$CUR").opentv-tmp"
    rm -f -- "$(dest "$MANIFEST").tmp" "$(dest "$CMDF").opentv-tmp" "$(dest "$VIDEO_REC").opentv-tmp" \
      "$(dest "$BOOT_REC").opentv-tmp" "$(dest "$CFGF").opentv-tmp" "$(dest "$CFG_REC").opentv-tmp" \
      "$(dest "$PLY_CONF").opentv-tmp" "$(dest "$PLY_REC").opentv-tmp" "$(dest "$INITRD_REC").opentv-tmp"
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
    if [ "$CFG_CHANGED" = 1 ] && ! restore_durably "$(dest "$BK$CFGF")" "$(dest "$CFGF")"; then
      rm -f -- "$(dest "$CFGF").opentv-tmp"
      echo "opentv-install: restauration impossible : $CFGF (sauvegarde : $BK$CFGF)" >&2
    fi
    if [ "$PLYCONF_CHANGED" = 1 ]; then
      if [ "$PLYCONF_MODE" = create ]; then
        rm -f -- "$(dest "$PLY_CONF")" && durable_sync "$(dirname "$(dest "$PLY_CONF")")"
      elif ! restore_durably "$(dest "$BK$PLY_CONF")" "$(dest "$PLY_CONF")"; then
        rm -f -- "$(dest "$PLY_CONF").opentv-tmp"
        echo "opentv-install: restauration impossible : $PLY_CONF (sauvegarde : $BK$PLY_CONF)" >&2
      fi
    fi
    # marqueurs notés AVANT cmdline.txt : retirés/rétablis (aucun marqueur périmé après un échec)
    [ "$VIDEO_REC_NEW" = 1 ] && rm -f -- "$(dest "$VIDEO_REC")"
    if [ "$BOOT_REC_CHANGED" = 1 ]; then
      if [ -n "$BOOT_REC_OLD" ]; then put_durably "$(dest "$BOOT_REC")" "$BOOT_REC_OLD"; else rm -f -- "$(dest "$BOOT_REC")"; fi
      rm -f -- "$(dest "$BOOT_REC").opentv-tmp"
    fi
    if [ "$CFG_REC_CHANGED" = 1 ]; then
      if [ -n "$CFG_REC_OLD" ]; then put_durably "$(dest "$CFG_REC")" "$CFG_REC_OLD"; else rm -f -- "$(dest "$CFG_REC")"; fi
    fi
    if [ "$PLY_REC_CHANGED" = 1 ]; then
      if [ -n "$PLY_REC_OLD" ]; then put_durably "$(dest "$PLY_REC")" "$PLY_REC_OLD"; else rm -f -- "$(dest "$PLY_REC")"; fi
      if [ "$PLY_ORIG_OLD" = 1 ]; then restore_durably "$(dest "$BK$PLY_ORIG")" "$(dest "$PLY_ORIG")"
      else rm -f -- "$(dest "$PLY_ORIG")"; fi
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
    # initramfs : la copie du crochet z50-raspi-firmware est EN PLACE (non atomique) — octets d'avant la régénération
    # restaurés depuis la sauvegarde durable (temp → fsync → renommage → fsync du dossier), empreintes relues ;
    # marqueur de régénération et journal rendus à leur état précédent SEULEMENT après cette vérification. Sinon
    # (processus encore actif, sauvegarde ou écriture impossible) : journal et marqueur conservés, rejoués par le
    # prochain install.sh / uninstall.sh avant toute autre action.
    if [ "$IR_TXN_OPEN" = 1 ]; then
      if [ "$IR_LIVE_WRITER" = 1 ]; then
        echo "opentv-install: régénération encore active : initramfs NON restauré maintenant (jamais en concurrence" \
          "avec la copie) ; journal $IR_TXN et marqueur conservés — relancer sudo ./install.sh ou" \
          "sudo /opt/opentv/uninstall.sh pour le rétablir" >&2
      elif ir_restore "$IR_BK" && ir_txn_end; then
        echo "opentv-install: initramfs rétabli octet pour octet (SHA-256 vérifiés)" >&2
        if [ "$INITRD_REC_CHANGED" = 1 ]; then
          if [ "$INITRD_REC_EXISTED" = 1 ]; then put_durably "$(dest "$INITRD_REC")" "$INITRD_REC_OLD"
          else rm -f -- "$(dest "$INITRD_REC")"; durable_sync "$(dirname "$(dest "$INITRD_REC")")"; fi
        fi
      else
        echo "opentv-install: initramfs NON rétabli : journal $IR_TXN et marqueur $INITRD_REC conservés (sauvegarde" \
          "$IR_BK) ; relancer sudo ./install.sh ou sudo /opt/opentv/uninstall.sh pour le rétablir" >&2
      fi
    fi
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
PLY_CMDLINE_ADDED=0   # splash / plymouth.ignore-serial-consoles ajoutés : initramfs régénéré et vérifié aussi
if [ ! -f "$C" ]; then
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then fail "$CMDF introuvable : Raspberry Pi OS Bookworm requis"; fi
  say "cmdline.txt absent ($ROOT) : 1080p et démarrage silencieux non appliqués"
  if [ "$PLY_OK" = 1 ]; then PLY_OK=0; PLY_WHY="cmdline.txt absent"; fi
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
  # Plymouth refusé localement (nosplash, plymouth.enable=0/false/no/off…) : respecté, l'écran texte reste le repli
  if ply_refused "$CMD_FIRST" && [ "$PLY_OK" = 1 ]; then
    PLY_OK=0
    PLY_WHY="désactivé localement dans cmdline.txt (nosplash/plymouth.enable=0|false|no)"
  fi
  if [ "$PLY_OK" = 1 ]; then
    for a in "${PLY_ARGS[@]}"; do
      if has_arg "$CMD_FIRST" "$a"; then say "cmdline.txt : ${a%%=*} déjà présent, inchangé"
      else ADD_ARGS+=("$a"); PLY_CMDLINE_ADDED=1; fi
    done
  fi
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

# ---------------------------------------------------------------- config.txt : arc-en-ciel du firmware retiré
# Un seul ajout EN FIN de fichier (« disable_splash=1 », précédé de « [all] » si la dernière section est
# conditionnelle), octets exacts notés AVANT l'écriture, écriture atomique synchronisée. Aucune ligne existante
# modifiée ; fin de ligne CRLF reprise ; une valeur locale (disable_splash=0…) est respectée ; jamais de doublon.
if [ "$CFG_PRESENT" = 0 ]; then
  say "config.txt absent ($ROOT) : arc-en-ciel du firmware non désactivé"
elif [ -n "$CFG_SPLASH_LOCAL" ]; then
  say "config.txt : $CFG_SPLASH_LOCAL réglé localement, respecté (arc-en-ciel du firmware conservé)"
elif [ "$CFG_SPLASH" = 1 ]; then
  say "config.txt : disable_splash=1 déjà présent, inchangé"
else
  le=$'\n'
  first=""
  IFS= read -r first < "$CF" || true
  case "$first" in *$'\r') le=$'\r\n' ;; esac
  sfx=""
  if [ -s "$CF" ] && [ "$(tailhex "$CF" 1)" != 0a ]; then sfx="$le"; fi   # dernière ligne sans fin de ligne
  [ "$CFG_LAST_ALL" = 1 ] || sfx="${sfx}[all]${le}"
  sfx="${sfx}disable_splash=1$le"
  if [ "$DRY" = 1 ]; then
    say "[dry-run] ajouter disable_splash=1 en fin de $CFGF (octets notés dans $CFG_REC, retirés par uninstall.sh)"
  else
    STARTED=1
    backup "$CFGF"
    durable_sync "$(dest "$BK$CFGF")"
    mkdir -p -- "$(dirname "$(dest "$CFG_REC")")"
    if [ -f "$(dest "$CFG_REC")" ]; then CFG_REC_OLD="$(cat "$(dest "$CFG_REC")")"; fi
    CFG_REC_CHANGED=1
    put_durably "$(dest "$CFG_REC")" "suffix=$(printf '%s' "$sfx" | hexof)"
    { cat -- "$CF"; printf '%s' "$sfx"; } > "$CF.opentv-tmp"
    chmod "$(file_mode "$CF")" "$CF.opentv-tmp"
    durable_sync "$CF.opentv-tmp"
    CFG_CHANGED=1
    mv -f -- "$CF.opentv-tmp" "$CF"
    durable_sync "$CF" "$(dirname "$CF")"
    say "config.txt : disable_splash=1 ajouté (arc-en-ciel du firmware retiré au prochain redémarrage)"
  fi
fi

# ---------------------------------------------------------------- Plymouth : thème « opentv » (repli : écran texte)
# ply_scan <fichier> : dernière section [Daemon] (PLY_DAEMON), dernière ligne Theme= de cette section (PLY_TLINE,
# valeur PLY_TVAL), nombre de lignes, fin de ligne CRLF. Format GKeyFile : espaces autour de « = » tolérés.
ply_scan() {
  local l n=0 sec=""
  PLY_DAEMON=0; PLY_TLINE=0; PLY_TVAL=""; PLY_LE=$'\n'
  [ -f "$1" ] || return 0
  while IFS= read -r l || [ -n "$l" ]; do
    n=$((n + 1))
    [ "$n" = 1 ] && case "$l" in *$'\r') PLY_LE=$'\r\n' ;; esac
    l="${l%$'\r'}"
    l="${l#"${l%%[![:space:]]*}"}"
    case "$l" in
      '['*) sec="$l"; sec="${sec%"${sec##*[![:space:]]}"}"; [ "$sec" = "[Daemon]" ] && PLY_DAEMON=$n ;;
      Theme=*|Theme[[:space:]]*=*)
        if [ "$sec" = "[Daemon]" ]; then
          PLY_TLINE=$n
          PLY_TVAL="${l#*=}"; PLY_TVAL="${PLY_TVAL#"${PLY_TVAL%%[![:space:]]*}"}"
          PLY_TVAL="${PLY_TVAL%"${PLY_TVAL##*[![:space:]]}"}"
        fi ;;
    esac
  done < "$1"
}
ply_eol_at() {   # ply_eol_at <fichier> <n> : la ligne n se termine par un saut de ligne
  [ "$(head -n "$2" -- "$1" | tail -c 1 | hexof)" = 0a ]
}
THEME_WRITTEN=0
for e in ${NEW[@]+"${NEW[@]}"} ${SAVED[@]+"${SAVED[@]}"}; do
  case "$e" in "$PLY_THEME_DIR"/*) THEME_WRITTEN=1 ;; esac
done
PLY_THEME_NOW=""
if [ "$PLY_OK" = 0 ]; then
  say "Plymouth : non utilisé ($PLY_WHY) — repli : écran OpenTV texte sur tty1 (opentv-splash), démarrage inchangé"
else
  ply_scan "$PC"
  PLY_THEME_NOW="$PLY_TVAL"
  if [ "$PLY_TVAL" = opentv ]; then
    say "Plymouth : thème opentv déjà sélectionné ($PLY_CONF inchangé)"
  elif [ "$DRY" = 1 ]; then
    say "[dry-run] Plymouth : Theme=opentv dans $PLY_CONF (thème précédent « ${PLY_TVAL:-défaut} » noté, restauré par uninstall.sh)"
  else
    STARTED=1
    prev=""
    if [ ! -e "$PC" ]; then
      PLYCONF_MODE=create
      printf '[Daemon]\nTheme=opentv\n' > "$PC.opentv-tmp"
      chmod 644 "$PC.opentv-tmp"
    else
      if [ "$PLY_TLINE" -gt 0 ]; then
        PLYCONF_MODE=replace
        prev="$(head -n "$PLY_TLINE" -- "$PC" | tail -n 1 | hexof)"
        prev="${prev%0a}"
        cr=""
        case "$prev" in *0d) cr=$'\r' ;; esac
        nl=""
        ply_eol_at "$PC" "$PLY_TLINE" && nl=$'\n'
        { head -n $((PLY_TLINE - 1)) -- "$PC"; printf 'Theme=opentv%s%s' "$cr" "$nl"; tail -n +$((PLY_TLINE + 1)) -- "$PC"; } \
          > "$PC.opentv-tmp"
      elif [ "$PLY_DAEMON" -gt 0 ]; then
        PLYCONF_MODE=insert
        sep=""
        ply_eol_at "$PC" "$PLY_DAEMON" || sep="$PLY_LE"
        { head -n "$PLY_DAEMON" -- "$PC"; printf '%sTheme=opentv%s' "$sep" "$PLY_LE"; tail -n +$((PLY_DAEMON + 1)) -- "$PC"; } \
          > "$PC.opentv-tmp"
      else
        PLYCONF_MODE=append
        sep=""
        if [ -s "$PC" ] && [ "$(tailhex "$PC" 1)" != 0a ]; then sep="$PLY_LE"; fi
        { cat -- "$PC"; printf '%s[Daemon]%sTheme=opentv%s' "$sep" "$PLY_LE" "$PLY_LE"; } > "$PC.opentv-tmp"
      fi
      chmod "$(file_mode "$PC")" "$PC.opentv-tmp"
      backup "$PLY_CONF"
    fi
    # thème précédent noté (et contenu d'origine conservé) AVANT le remplacement : jamais d'état non restaurable
    mkdir -p -- "$(dirname "$(dest "$PLY_REC")")"
    if [ -f "$(dest "$PLY_REC")" ]; then PLY_REC_OLD="$(cat "$(dest "$PLY_REC")")"; fi
    if [ -f "$(dest "$PLY_ORIG")" ]; then PLY_ORIG_OLD=1; backup "$PLY_ORIG"; fi
    PLY_REC_CHANGED=1
    if [ "$PLYCONF_MODE" = create ]; then rm -f -- "$(dest "$PLY_ORIG")"
    else restore_durably "$PC" "$(dest "$PLY_ORIG")"; fi   # contenu d'origine exact, écrit durablement
    put_durably "$(dest "$PLY_REC")" "mode=$PLYCONF_MODE
prev=$prev
sha=$(sha256 "$PC.opentv-tmp")"
    durable_sync "$PC.opentv-tmp"
    PLYCONF_CHANGED=1
    mv -f -- "$PC.opentv-tmp" "$PC"
    durable_sync "$PC" "$(dirname "$PC")"
    PLY_THEME_NOW=opentv
    say "Plymouth : Theme=opentv (précédent : « ${PLY_TVAL:-défaut} », restauré par uninstall.sh)"
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

# ---------------------------------------------------------------- pont Muse : compatibilité BlueZ du SDK épinglé
# = install.sh officiel du commit épinglé (configure_bluez + disable_bluez_battery) : [GATT] ExchangeMTU = 256 (app
# Android) et greffon battery de bluetoothd désactivé par le drop-in bluetooth.service.d/zz-opentv-muse.conf (sinon
# BlueZ demande à l'iPhone de se lier et l'appariement iOS peut tomber). Changements POSSÉDÉS (opentv_pi/musebluez.py) :
# notés dans /var/lib/opentv-install/muse-bluez.json, sauvegardés dans $BK (retour arrière de CETTE transaction) et
# retirés exactement par uninstall.sh (--purge compris). Jamais : une valeur ExchangeMTU locale > 256 remplacée (geste
# explicite : opentv-pi muse prepare-bluetooth --replace-mtu), un drop-in étranger écrasé, une commande bluetoothd
# complexe réécrite — l'appariement est alors refusé avec la commande à lancer. Bluetooth n'est redémarré qu'après le
# point de non-retour, sur la vraie racine, et seulement si quelque chose a changé.
BLUEZ_RESTART=0
BLUEZ_ARGS=(muse prepare-bluetooth --installer --root "$ROOT" --backup-dir "$BK")
if [ "$DRY" = 1 ]; then BLUEZ_ARGS+=(--dry-run); fi
if ! command -v python3 >/dev/null 2>&1; then
  say "Muse : python3 introuvable, compatibilité BlueZ non appliquée (appariement refusé : opentv-pi muse status)"
else
  BLUEZ_OUT="$(python3 -I "$HERE/bin/opentv-pi" "${BLUEZ_ARGS[@]}")" ||
    fail "compatibilité BlueZ du pont Muse non appliquée (octets d'avant rendus) : retour à l'état précédent"
  while IFS= read -r l; do
    case "$l" in
      "saved "*) STARTED=1; SAVED+=("${l#saved }") ;;
      "created "*) STARTED=1; NEW+=("${l#created }") ;;
      "dir "*) NEW_DIRS+=("${l#dir }") ;;
      restart) BLUEZ_RESTART=1; say "Muse : compatibilité BlueZ du SDK appliquée (ExchangeMTU = 256, greffon battery désactivé ; notée, retirée par uninstall.sh)" ;;
      "skipped "*) say "ATTENTION Muse : BlueZ non modifié (${l#skipped }) — appariement refusé tant que la compatibilité n'est pas active : sudo /opt/opentv/bin/opentv-pi muse status" ;;
      "plan "*) say "[dry-run] Muse : ${l#plan }" ;;
    esac
  done <<EOF
$BLUEZ_OUT
EOF
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

# ---------------------------------------------------------------- initramfs : image OpenTV dès le noyau
# Seulement après TOUTES les vérifications et une fois le thème complet en place (fichiers + Theme=opentv) ; borné.
# Raspberry Pi OS : update-initramfs renomme atomiquement /boot/initrd.img-<v>, mais le crochet z50-raspi-firmware
# COPIE EN PLACE vers $INITRD (non atomique). Donc, avant tout : copie durable de chaque fichier réécrivable (SHA-256,
# taille, mode) dans $BK/initramfs et journal $IR_TXN ; régénération du seul noyau v8 exact, bornée par SIGTERM
# (jamais SIGKILL) ; vérification lsinitramfs (entrées exigées) ET $INITRD identique à initrd.img-<v> ; tout échec :
# retour arrière complet (thème, config.txt, cmdline.txt) et octets d'origine restaurés puis relus.
THEME_FILES="opentv.plymouth opentv.script opentv.png opentv-mask.png"
initramfs_check() {   # liste écrite dans un fichier (jamais de substitution qui déclencherait un 2e retour arrière)
  local list need
  if [ "$LIVE" = 0 ] && [ "${OPENTV_INSTALL_TEST_BOOTCMDS:-}" != 1 ]; then
    say "[racine de test, non exécuté] lsinitramfs $INITRD"
    return 0
  fi
  list="$(dest "$BK")/initramfs.list"
  mkdir -p -- "$(dest "$BK")" || return 1
  if ! ir_list "$list"; then
    echo "opentv-install: lsinitramfs $INITRD a échoué ou dépassé $IR_LS_S s" >&2
    return 1
  fi
  for need in $THEME_FILES; do
    ir_has_entry "usr/share/plymouth/themes/opentv/$need" "$list" ||
      { echo "opentv-install: $INITRD : thème incomplet ($need absent)" >&2; return 1; }
  done
  grep -qE '(^|/)sbin/plymouthd$' -- "$list" || { echo "opentv-install: $INITRD : plymouthd absent" >&2; return 1; }
  grep -qE '/plymouth/script\.so$' -- "$list" || { echo "opentv-install: $INITRD : module script absent" >&2; return 1; }
  grep -qE '/plymouth/renderers/drm\.so$' -- "$list" || { echo "opentv-install: $INITRD : rendu DRM absent" >&2; return 1; }
  if ! ir_same_as_source "$IR_KERNEL"; then
    echo "opentv-install: $INITRD différent de /boot/initrd.img-$IR_KERNEL (copie du crochet incomplète ou noyau non chargé)" >&2
    return 1
  fi
  if grep -qE '/vc4\.ko(\.[a-z]+)?$' -- "$list"; then
    say "initramfs : thème opentv, plymouthd, module script, rendu DRM et pilote vc4 présents"
  else
    say "initramfs : thème opentv présent ; pilote vc4 absent de l'initramfs (image affichée dès son chargement)"
  fi
}
REBUILD=0
if [ "$PLY_OK" = 1 ]; then
  # transaction interrompue rejouée au début (octets d'avant restaurés) : l'image OpenTV doit être régénérée
  if [ "$PLYCONF_CHANGED" = 1 ] || [ "$THEME_WRITTEN" = 1 ] || [ "$PLY_CMDLINE_ADDED" = 1 ] || [ "$IR_RECOVERED" = 1 ] ||
     { [ "$DRY" = 1 ] && { [ "$PLY_THEME_NOW" != opentv ] || [ "$IR_TXN_PENDING" = 1 ]; }; }; then
    REBUILD=1
  fi
fi
if [ "$REBUILD" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    say "[dry-run] initramfs : sauvegarde durable (SHA-256) de $INITRD, /boot/initrd.img-$IR_KERNEL et /var/lib/initramfs-tools/$IR_KERNEL dans $IR_BK, journal $IR_TXN"
    say "[dry-run] initramfs : update-initramfs -u -k $IR_KERNEL (SIGTERM après $IR_UPDATE_S s, jamais SIGKILL) puis lsinitramfs $INITRD (borné à $IR_LS_S s, thème opentv requis) et $INITRD identique à /boot/initrd.img-$IR_KERNEL"
    say "[dry-run] initramfs : régénération notée dans $INITRD_REC (uninstall.sh régénère, même si Theme=opentv était déjà choisi)"
  else
    for t in $THEME_FILES; do
      [ -f "$(dest "$PLY_THEME_DIR/$t")" ] || fail "thème Plymouth incomplet ($PLY_THEME_DIR/$t) : initramfs non régénéré"
    done
    [ -n "$IR_KERNEL" ] || fail "noyau v8 introuvable : initramfs non régénéré"
    STARTED=1
    # 1) octets réécrivables sauvegardés durablement et relus, PUIS journal : tout échec ou coupure ultérieure est
    #    rétabli octet pour octet (retour arrière, ou prochain install.sh/uninstall.sh)
    ir_backup "$IR_BK" "$IR_KERNEL" || fail "sauvegarde durable de l'initramfs impossible : rien régénéré"
    IR_TXN_OPEN=1
    ir_txn_begin "$IR_BK" "$IR_KERNEL" install || fail "journal $IR_TXN impossible : rien régénéré"
    # 2) noté et synchronisé AVANT la régénération : une coupure ne laisse jamais une charge OpenTV non notée
    if [ -f "$(dest "$INITRD_REC")" ]; then INITRD_REC_EXISTED=1; INITRD_REC_OLD="$(cat "$(dest "$INITRD_REC")")"; fi
    if [ "$INITRD_REC_OLD" != "$INITRD_REC_LINE" ]; then
      mkdir -p -- "$(dirname "$(dest "$INITRD_REC")")"
      INITRD_REC_CHANGED=1
      put_durably "$(dest "$INITRD_REC")" "$INITRD_REC_LINE"
    fi
    say "initramfs : régénération du noyau $IR_KERNEL avec le thème opentv (peut prendre quelques minutes)"
    initramfs_rebuild || {
      [ $? = 125 ] && IR_LIVE_WRITER=1
      fail "update-initramfs -u -k $IR_KERNEL a échoué : thème, config.txt, cmdline.txt et initramfs restaurés"
    }
    initramfs_check || fail "initramfs régénéré mais invalide : thème, config.txt, cmdline.txt et initramfs restaurés"
  fi
elif [ "$PLY_OK" = 1 ]; then
  say "initramfs : inchangé (thème opentv déjà en place)"
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
  # transaction initramfs validée (image vérifiée, manifeste écrit) : journal effacé durablement, sauvegarde gardée
  if [ "$IR_TXN_OPEN" = 1 ]; then
    ir_txn_end || fail "journal $IR_TXN non effacé"
    IR_TXN_OPEN=0
  fi
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
# dossiers d'état, dont /var/lib/opentv/canvas (ambiance OpenTV : opentv:opentv 0700, créé s'il manque, contenu jamais
# lu ni écrit ici) AVANT tout (re)démarrage : chaque unité qui le masque (InaccessiblePaths=) le trouve présent
post systemd-tmpfiles --create /etc/tmpfiles.d/opentv.conf
post chown root:opentv "$ENVF"
# témoin d'une désinstallation interrompue (/run/opentv-uninstalling : bloque rafraîchissement et notes) : retiré
if [ -e "$(dest /run/opentv-uninstalling)" ] || [ -L "$(dest /run/opentv-uninstalling)" ]; then
  act rm -f -- "$(dest /run/opentv-uninstalling)"
fi
post systemctl daemon-reload
# compatibilité BlueZ changée par cette installation : bluetoothd relit main.conf et sa commande (try-restart : rien
# si bluetooth ne tourne pas) ; un échec n'annule rien, l'appariement reste refusé tant qu'elle n'est pas active
if [ "$BLUEZ_RESTART" = 1 ] && ! host systemctl try-restart bluetooth.service; then
  say "ATTENTION Muse : redémarrage de bluetooth impossible — sudo /opt/opentv/bin/opentv-pi muse prepare-bluetooth"
fi
if [ "$ENABLE" = 1 ]; then
  # opentv-ap-guard.timer : garde root du Wi-Fi temporaire, indépendante du service Python
  # opentv-cec.timer : déclaration Playback « OpenTV » + remappage OK + UNE annonce de source active par démarrage
  # (fenêtre bornée, TV allumée seulement ; jamais de réveil ni de réannonce). Réactivation : opentv-pi cec --activate
  # opentv-cec-return.service : moniteur CEC passif ; rejoue cette annonce sûre une fois seulement quand la TV revient
  # d'une application native vers la boîte (séquence exacte émise par la TV ; jamais périodique, jamais de réveil)
  # opentv-splash.service : écran OpenTV sur tty1 au démarrage (sysinit), jamais redémarré ici (Cage possède tty1) ;
  # il n'écrit rien quand Plymouth affiche déjà l'image OpenTV (repli seulement)
  # opentv-player.service + opentv-player-keys.service : contrôleur du lecteur « Regarder » et aide des touches.
  # Activés mais SANS capacité de lecture : « Regarder » exige la source approuvée (/etc/opentv/source.env) ET le
  # témoin de promotion terrain /etc/opentv/playback-promoted — ni l'un ni l'autre n'est jamais livré, écrit ni
  # écrasé par l'installation (opentv-pi playback promote --field-validated, après la validation HDMI réelle).
  # opentv-catalogue-refresh.timer : rafraîchissement quotidien du catalogue (sauté sans /etc/opentv/source.env) ;
  # seule la minuterie est (re)démarrée ici, jamais le passage lui-même (aucune requête pendant l'installation).
  # opentv-catalogue-ratings.service (notes TMDb) n'a pas de section [Install] : tirée par OnSuccess= du
  # rafraîchissement seulement, jamais activée ni lancée ici. opentv-catalogue-ratings-retry.timer : reprise horaire
  # BORNÉE après un échec transitoire seulement (sinon aucun passage ni requête) ; minuterie (re)démarrée, jamais le
  # passage. Le dossier du jeton existe déjà (tmpfiles ci-dessus) : chaque unité redémarrée ici le masque.
  # opentv-plymouth-failsafe.timer : filet absolu (120 s après le noyau) contre un Plymouth laissé indéfiniment ;
  # activé pour le prochain démarrage seulement (jamais démarré ici). Les unités de passage deactivate/quit n'ont pas
  # de section [Install] : tirées par opentv-kiosk.service (Wants=), elles ne s'activent ni ne s'exécutent seules.
  # opentv-muse-setup.path : connexion de Muse depuis la TV (Réglages > Gérer les assistants > Muse). Le helper root
  # ne fait rien sans job du service, et le service n'en dépose qu'après « Autoriser et connecter » confirmé sur la TV
  # (aucun pilotage accordé par l'installation). opentv-muse-setup.service n'a pas de section [Install].
  post systemctl enable opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service \
    opentv-cec.timer opentv-cec-return.service opentv-splash.service opentv-player-keys.service opentv-player.service \
    opentv-catalogue-refresh.timer opentv-catalogue-ratings-retry.timer opentv-plymouth-failsafe.timer \
    opentv-muse-setup.path
  post systemctl restart opentv-net-helper.path opentv-ap-guard.timer opentv.service opentv-kiosk.service \
    opentv-cec.timer opentv-cec-return.service opentv-player-keys.service opentv-player.service opentv-catalogue-refresh.timer \
    opentv-catalogue-ratings-retry.timer opentv-muse-setup.path
fi
# ---------------------------------------------------------------- pont Muse : jamais activé par l'installation
# opentv-muse.service n'est activé que par un appariement humain réussi (sudo opentv-pi muse pair) ; une mise à jour
# ne fait que le redémarrer s'il tournait déjà (try-restart). opentv-muse-pair.service n'a pas de section [Install].
post systemctl try-restart opentv-muse.service
# Connexion de Muse depuis la TV : les jobs (jeton SDK) vivent désormais sur tmpfs (/run/opentv-musejobs, tmpfiles
# ci-dessus). Ancien emplacement sur la carte SD (versions précédentes, avant le redémarrage du service ci-dessus) : un
# job encore en file peut porter un jeton — retiré (fichiers réguliers seulement, jamais un lien suivi), puis les
# dossiers vides supprimés ; le nouveau helper ne lit jamais l'ancien emplacement.
for d in /var/lib/opentv/musejobs /var/lib/opentv/musejobs-tmp; do
  dd="$(dest "$d")"
  if [ -d "$dd" ] && [ ! -L "$dd" ]; then
    act find "$dd" -mindepth 1 -maxdepth 1 -type f -delete || say "ATTENTION Muse : $d non vidé"
    act rmdir -- "$dd" 2>/dev/null || say "ATTENTION Muse : $d conservé (non vide)"
  fi
done
# Service musegadget d'origine (system.run, file.read, file.write, device.health) : masqué tant qu'aucune unité
# étrangère n'existe (lien /dev/null noté, retiré par uninstall.sh) ; s'il est déjà installé, le pont refuse de
# s'apparier (opentv-pi muse status/pair le signalent) et rien d'étranger n'est modifié.
MUSE_MASK="/etc/systemd/system/musegadget.service"
MUSE_MASK_REC="/var/lib/opentv-install/musegadget-masked"
MM="$(dest "$MUSE_MASK")"
if [ -L "$MM" ] && [ "$(readlink "$MM")" = /dev/null ]; then
  say "Muse : service musegadget d'origine déjà masqué"
elif [ -e "$MM" ] || [ -L "$MM" ] || [ -e "$(dest /lib/systemd/system/musegadget.service)" ] ||
     [ -e "$(dest /usr/lib/systemd/system/musegadget.service)" ]; then
  say "ATTENTION Muse : service musegadget d'origine présent (il donne system.run/file.* à Muse) — non modifié ; le pont OpenTV refusera de s'apparier tant qu'il existe"
elif [ "$DRY" = 1 ]; then
  say "[dry-run] masquer musegadget.service d'origine ($MUSE_MASK -> /dev/null, noté dans $MUSE_MASK_REC)"
else
  { mkdir -p -- "$(dirname "$(dest "$MUSE_MASK_REC")")" && put_durably "$(dest "$MUSE_MASK_REC")" "masked=$MUSE_MASK" &&
    mkdir -p -- "$(dirname "$MM")" && ln -s /dev/null "$MM"; } || post_fail "masquage de musegadget.service"
  post systemctl daemon-reload
  say "Muse : service musegadget d'origine masqué (jamais activable à côté d'OpenTV)"
fi
if [ -n "$MUSE_DEP" ]; then
  say "Muse : SDK épinglé et dépendance websockets installés, NON apparié (sur la TV : Réglages > Gérer les assistants > Muse > Autoriser et connecter ; secours administrateur : sudo /opt/opentv/bin/opentv-pi muse pair)"
else
  say "Muse : dépendance websockets absente (sudo ./prepare-pi.sh --yes) — pont Muse indisponible, rien d'apparié"
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
if [ "$PLY_OK" = 1 ]; then
  say "démarrage : image OpenTV (Plymouth, thème opentv) du noyau jusqu'au kiosque, sans arc-en-ciel ni terminal (effectif au prochain redémarrage ; contrôle : sudo /opt/opentv/bin/opentv-boot-check)"
else
  say "démarrage : écran OpenTV sur tty1, sans terminal ni invite (Plymouth non utilisé : $PLY_WHY ; effectif au prochain redémarrage ; contrôle : sudo /opt/opentv/bin/opentv-boot-check)"
fi
say "touche Power des télécommandes ignorée par la boîte (/etc/systemd/logind.conf.d/opentv-kiosk.conf) : effectif au prochain redémarrage (sudo reboot)"
say "terminé. Retour arrière : sudo /opt/opentv/uninstall.sh   (ajouter --purge pour effacer l'état)"
