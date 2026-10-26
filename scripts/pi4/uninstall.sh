#!/bin/bash
# OpenTV Raspberry Pi 4 — désinstallation / retour arrière.
#
#   sudo /opt/opentv/uninstall.sh            arrête les services et la garde, ARRÊTE ET SUPPRIME le Wi-Fi temporaire
#                                            OpenTV, retire les fichiers du manifeste, restaure les fichiers d'origine
#                                            sauvegardés, retire video=… de cmdline.txt, rend tty1
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
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

MANIFEST="$(dest /opt/opentv/MANIFEST.sha256)"
# 1) plus rien ne peut (re)lancer le point d'accès : service, helper et garde arrêtés
host systemctl disable --now opentv-kiosk.service opentv.service opentv-net-helper.path opentv-net-helper.service \
  opentv-ap-guard.timer opentv-ap-guard.service
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
# 3) fichiers

if [ -f "$MANIFEST" ]; then
  while read -r _ p; do
    case "$p" in /opt/opentv/*|/etc/systemd/system/opentv*|/etc/sysusers.d/opentv.conf|/etc/tmpfiles.d/opentv.conf|/etc/pam.d/opentv-kiosk) ;;
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

# 1080p : retire uniquement le paramètre ajouté par install.sh (le reste de cmdline.txt, même modifié depuis, est conservé)
VIDEO_REC="$(dest /var/lib/opentv-install/cmdline-video.added)"
CMDF="$(dest /boot/firmware/cmdline.txt)"
if [ -f "$VIDEO_REC" ]; then
  arg="$(head -n 1 "$VIDEO_REC")"
  if [ "$arg" = "video=HDMI-A-1:1920x1080@60" ] && [ -f "$CMDF" ] && grep -qF -- " $arg" "$CMDF"; then
    if [ "$DRY" = 1 ]; then
      say "[dry-run] retirer $arg de /boot/firmware/cmdline.txt"
    else
      : > "$CMDF.opentv-tmp"
      while IFS= read -r line || [ -n "$line" ]; do printf '%s\n' "${line// "$arg"/}" >> "$CMDF.opentv-tmp"; done < "$CMDF"
      chmod "$(stat -c %a "$CMDF" 2>/dev/null || stat -f %Lp "$CMDF")" "$CMDF.opentv-tmp"
      mv -f -- "$CMDF.opentv-tmp" "$CMDF"
      say "cmdline.txt : $arg retiré (effectif au prochain redémarrage)"
    fi
  fi
  act rm -f -- "$VIDEO_REC"
fi

host systemctl daemon-reload
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
