#!/bin/bash
# OpenTV Raspberry Pi 4 — désinstallation / retour arrière.
#
#   sudo /opt/opentv/uninstall.sh            arrête les services et la garde, ARRÊTE ET SUPPRIME le Wi-Fi temporaire
#                                            OpenTV, libère la CEC (Playback « OpenTV » effacé, touche OK du noyau
#                                            rétablie), retire les fichiers du manifeste (dont le drop-in logind de
#                                            la touche Power et la politique Chromium du kiosque), restaure les
#                                            fichiers d'origine sauvegardés, retire de cmdline.txt exactement les
#                                            jetons ajoutés (video=…, démarrage silencieux), retire l'écran OpenTV et
#                                            la condition getty de tty1, rend tty1
#   ... --purge                              efface aussi l'état (/var/lib/opentv*, /etc/opentv)
#   ... --purge --forget-wifi                oublie aussi le Wi-Fi configuré par OpenTV (opentv-wifi)
#   ... --dry-run | --root <dossier>         plan seul / rootfs de test
set -Eeuo pipefail

ROOT="/"
DRY=0
PURGE=0
FORGET_WIFI=0
die() { echo "opentv-uninstall: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root exige un chemin"; ROOT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --purge) PURGE=1; shift ;;
    --forget-wifi) FORGET_WIFI=1; shift ;;
    -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done
case "$ROOT" in /*) ;; *) die "--root doit être un chemin absolu" ;; esac
if [ "$ROOT" != "/" ]; then ROOT="${ROOT%/}"; fi
LIVE=0
[ "$ROOT" = "/" ] && LIVE=1
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then die "exécuter en root (sudo) ou utiliser --dry-run"; fi

dest() { if [ "$ROOT" = "/" ]; then printf '%s' "$1"; else printf '%s%s' "$ROOT" "$1"; fi; }
act() { if [ "$DRY" = 1 ]; then say "[dry-run] $*"; else "$@"; fi; }
host() {
  if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then "$@" || say "(ignoré : $* a échoué)"
  elif [ "$DRY" = 1 ]; then say "[dry-run] $*"
  else say "[racine de test, non exécuté] $*"; fi
}

durable_sync() { sync -- "$@" 2>/dev/null || sync; }   # fsync des chemins nommés ; repli : sync global
replace_durably() {   # temp synchronisé AVANT le renommage, cible + dossier APRÈS (jamais de cmdline.txt vide/tronqué)
  durable_sync "$1" || return 1
  mv -f -- "$1" "$2" || return 1
  durable_sync "$2" "$(dirname "$2")"
}

# 0) cmdline.txt EN PREMIER : retire uniquement les jetons notés par install.sh (liste fermée ; dernière occurrence
#    exacte, délimitée par des espaces). Le reste de la ligne, même modifié depuis, les autres lignes et la fin de ligne
#    sont conservés. Si des jetons sont notés mais que cmdline.txt ne peut pas être traité (partition non montée,
#    fichier absent ou inattendu, écriture impossible) : refus AVANT toute autre modification, marqueurs et
#    /opt/opentv/uninstall.sh conservés pour réessayer. Les marqueurs ne sont effacés qu'après une écriture durable.
VIDEO_REC="$(dest /var/lib/opentv-install/cmdline-video.added)"
BOOT_REC="$(dest /var/lib/opentv-install/cmdline-boot.added)"
CMDF="$(dest /boot/firmware/cmdline.txt)"
drop_arg() {   # drop_arg <ligne> <jeton> : ligne sans la dernière occurrence exacte du jeton
  local l=" $1 " pre post
  case "$l" in *" $2 "*) ;; *) printf '%s' "$1"; return 0 ;; esac
  pre="${l%" $2 "*}"
  post="${l##*" $2 "}"
  l="$pre $post"
  l="${l# }"
  printf '%s' "${l% }"
}
cmdline_blocked() {
  echo "opentv-uninstall: $*" >&2
  echo "opentv-uninstall: désinstallation NON effectuée, rien d'autre n'a été modifié ; jetons OpenTV toujours notés" \
    "dans /var/lib/opentv-install (cmdline-*.added)." >&2
  echo "opentv-uninstall: corriger (ex. sudo mount /boot/firmware) puis relancer : sudo /opt/opentv/uninstall.sh" >&2
  exit 1
}
DROP=()
for rec in "$VIDEO_REC" "$BOOT_REC"; do
  [ -f "$rec" ] || continue
  while IFS= read -r a || [ -n "$a" ]; do
    case "$a" in
      video=HDMI-A-1:1920x1080@60|quiet|loglevel=3|logo.nologo|vt.global_cursor_default=0|systemd.show_status=false)
        DROP+=("$a") ;;
      *) [ -z "$a" ] || say "ignoré (jeton hors liste OpenTV) : $a" ;;
    esac
  done < "$rec"
done
if [ ${#DROP[@]} -gt 0 ]; then
  if [ "$LIVE" = 1 ] && command -v mountpoint >/dev/null 2>&1 && ! mountpoint -q /boot/firmware; then
    cmdline_blocked "/boot/firmware n'est pas monté : impossible de retirer ${DROP[*]} de cmdline.txt"
  fi
  [ -f "$CMDF" ] || cmdline_blocked "/boot/firmware/cmdline.txt introuvable : impossible de retirer ${DROP[*]}"
  first=""
  cr=""
  IFS= read -r first < "$CMDF" || true
  case "$first" in *$'\r') cr=$'\r'; first="${first%$'\r'}" ;; esac
  case " $first " in *" root="*) ;; *) cmdline_blocked "cmdline.txt : première ligne sans root=… (fichier inattendu), non modifié" ;; esac
  new="$first"
  for a in "${DROP[@]}"; do new="$(drop_arg "$new" "$a")"; done
  cmdline_put() {   # nouvelle première ligne ; CR et lignes suivantes conservés ; écriture durable
    local nl=""
    if [ "$(wc -l < "$CMDF" | tr -d ' ')" -gt 0 ]; then nl=$'\n'; fi
    { printf '%s%s%s' "$new" "$cr" "$nl" && tail -n +2 "$CMDF"; } > "$CMDF.opentv-tmp" || return 1
    chmod "$(stat -c %a "$CMDF" 2>/dev/null || stat -f %Lp "$CMDF")" "$CMDF.opentv-tmp" || return 1
    replace_durably "$CMDF.opentv-tmp" "$CMDF"
  }
  if [ "$new" = "$first" ]; then
    say "cmdline.txt : jetons OpenTV déjà absents, inchangé"
  elif [ "$DRY" = 1 ]; then
    say "[dry-run] retirer ${DROP[*]} de /boot/firmware/cmdline.txt"
  elif cmdline_put; then
    say "cmdline.txt : ${DROP[*]} retiré(s) (effectif au prochain redémarrage)"
  else
    rm -f -- "$CMDF.opentv-tmp" 2>/dev/null || true
    cmdline_blocked "écriture durable de cmdline.txt impossible ou non confirmée (support en lecture seule ?)"
  fi
fi
for rec in "$VIDEO_REC" "$BOOT_REC"; do   # traité (ou rien à retirer) : marqueurs effacés seulement maintenant
  if [ -f "$rec" ]; then act rm -f -- "$rec"; fi
done

MANIFEST="$(dest /opt/opentv/MANIFEST.sha256)"
# 1) plus rien ne peut (re)lancer le point d'accès : service, helper et garde arrêtés
host systemctl disable --now opentv-kiosk.service opentv.service opentv-net-helper.path opentv-net-helper.service \
  opentv-ap-guard.timer opentv-ap-guard.service opentv-cec.timer opentv-cec.service opentv-cec-announce.service \
  opentv-splash.service
# 2) Wi-Fi temporaire OpenTV arrêté et supprimé AVANT tout retrait de fichier (normal comme --purge) ; argv fixes
NM="/etc/NetworkManager/system-connections"
AP_ID="opentv-setup-ap"
host nmcli --wait 10 connection down id "$AP_ID"
if [ -e "$(dest "$NM/$AP_ID.nmconnection")" ] || [ -L "$(dest "$NM/$AP_ID.nmconnection")" ]; then
  act rm -f -- "$(dest "$NM/$AP_ID.nmconnection")"
fi
host nmcli connection delete id "$AP_ID"
say "Wi-Fi temporaire OpenTV ($AP_ID) arrêté et supprimé"
# profil candidat d'un wifi-apply interrompu (autoconnect=false, jamais promu) : retiré, l'opentv-wifi actif reste
CAND_ID="opentv-wifi-candidate"
if [ -e "$(dest "$NM/$CAND_ID.nmconnection")" ] || [ -L "$(dest "$NM/$CAND_ID.nmconnection")" ]; then
  act rm -f -- "$(dest "$NM/$CAND_ID.nmconnection")"
fi
host nmcli connection delete id "$CAND_ID"
# 3) CEC : retour arrière borné AVANT le retrait des fichiers (le code est encore installé). N'efface que la
#    configuration Playback « OpenTV » et ne rétablit que la touche Select modifiée par OpenTV ; témoin /run retiré.
if [ -f "$(dest /opt/opentv/bin/opentv-pi)" ]; then
  host python3 -I /opt/opentv/bin/opentv-pi cec-bootstrap --release
fi
if [ -e "$(dest /run/opentv-cec)" ] || [ -L "$(dest /run/opentv-cec)" ]; then act rm -rf -- "$(dest /run/opentv-cec)"; fi
# 4) fichiers

if [ -f "$MANIFEST" ]; then
  while read -r _ p; do
    case "$p" in /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) ;;
      /etc/systemd/logind.conf.d/opentv-kiosk.conf|/etc/chromium/policies/managed/opentv-kiosk.json) ;;
      /etc/chromium-browser/policies/managed/opentv-kiosk.json) ;;
      /etc/systemd/system/getty@tty1.service.d/opentv-kiosk.conf) ;;
      *) [ -z "$p" ] || say "ignoré (hors périmètre OpenTV) : $p"; continue ;; esac
    if [ -e "$(dest "$p")" ]; then act rm -f -- "$(dest "$p")"; fi
  done < "$MANIFEST"
  # restauration des fichiers d'origine (dans l'ordre des installations)
  for list in "$(dest /var/lib/opentv-install)"/backup-*/BACKUP.list; do
    [ -f "$list" ] || continue
    bk="$(dirname "$list")"
    while read -r p; do
      [ -n "$p" ] || continue
      if [ -f "$bk$p" ] && [ ! -e "$(dest "$p")" ]; then
        act mkdir -p "$(dirname "$(dest "$p")")"
        act cp -p -- "$bk$p" "$(dest "$p")"
        say "restauré : $p"
      fi
    done < "$list"
  done
  act rm -f -- "$MANIFEST"
  act rm -rf -- "$(dest /opt/opentv/lib/opentv_pi/__pycache__)" "$(dest /opt/opentv/lib/foryou/__pycache__)"
  for d in /opt/opentv/lib/opentv_pi /opt/opentv/lib/foryou /opt/opentv/lib /opt/opentv/share/ui /opt/opentv/share \
           /opt/opentv/bin /opt/opentv; do
    if [ -d "$(dest "$d")" ]; then
      if [ "$DRY" = 1 ]; then say "[dry-run] rmdir $(dest "$d")"; else rmdir -- "$(dest "$d")" 2>/dev/null || say "conservé (non vide) : $d"; fi
    fi
  done
else
  say "manifeste absent : rien à retirer"
fi

# dossiers système créés par install.sh (liste fermée) : retirés seulement s'ils sont vides, du plus profond au plus haut
DIRS_REC="$(dest /var/lib/opentv-install/created-dirs.list)"
if [ -f "$DIRS_REC" ]; then
  DIRS=()
  while IFS= read -r d; do
    case "$d" in
      /etc/systemd/logind.conf.d|/etc/chromium|/etc/chromium/policies|/etc/chromium/policies/managed) DIRS+=("$d") ;;
      /etc/chromium-browser|/etc/chromium-browser/policies|/etc/chromium-browser/policies/managed) DIRS+=("$d") ;;
      /etc/systemd/system/getty@tty1.service.d) DIRS+=("$d") ;;
      *) [ -z "$d" ] || say "ignoré (hors périmètre OpenTV) : $d" ;;
    esac
  done < "$DIRS_REC"
  i=${#DIRS[@]}
  while [ "$i" -gt 0 ]; do
    i=$((i - 1))
    d="${DIRS[$i]}"
    if [ -d "$(dest "$d")" ] && [ ! -L "$(dest "$d")" ]; then
      if [ "$DRY" = 1 ]; then say "[dry-run] rmdir $(dest "$d")"; else rmdir -- "$(dest "$d")" 2>/dev/null || say "conservé (non vide) : $d"; fi
    fi
  done
  act rm -f -- "$DIRS_REC"
fi

host systemctl daemon-reload
# tty1 lisible tout de suite pour la console rendue (palette par défaut, curseur visible, police console de la
# distribution) ; best effort, chaque accès borné (une console bloquée ne fige jamais la désinstallation) ; le reste
# au redémarrage
if [ "$LIVE" = 1 ] && [ "$DRY" = 0 ]; then
  if [ -c /dev/tty1 ] && command -v timeout >/dev/null 2>&1; then
    printf '\033]R\033[0m\033[?25h' | timeout 2 tee -- /dev/tty1 >/dev/null 2>&1 || true
    if command -v setupcon >/dev/null 2>&1; then
      timeout 10 setupcon --force --font-only </dev/null >/dev/null 2>&1 || true
    elif command -v setfont >/dev/null 2>&1; then
      timeout 3 setfont -C /dev/tty1 </dev/null >/dev/null 2>&1 || true
    fi
  fi
else
  say "[plan] tty1 : palette par défaut, curseur visible, police console (best effort, borné)"
fi
host systemctl start getty@tty1.service

if [ "$PURGE" = 1 ]; then
  for d in /var/lib/opentv /var/lib/opentv-kiosk /etc/opentv /var/lib/opentv-install; do
    if [ -e "$(dest "$d")" ]; then act rm -rf -- "$(dest "$d")"; fi
  done
  if [ "$FORGET_WIFI" = 1 ] && [ -e "$(dest "$NM/opentv-wifi.nmconnection")" ]; then
    act rm -f -- "$(dest "$NM/opentv-wifi.nmconnection")"
  fi
  host nmcli connection reload
  say "comptes système conservés (sans effet) : sudo userdel opentv; sudo userdel opentv-kiosk; sudo groupdel opentv-agent"
fi
say "désinstallation terminée"
