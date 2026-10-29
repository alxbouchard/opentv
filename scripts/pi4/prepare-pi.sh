#!/bin/bash
# OpenTV Raspberry Pi 4 — préparation d'une Pi DÉJÀ flashée avec Raspberry Pi Imager
# (« Raspberry Pi OS (Legacy, 64-bit) Lite » = Bookworm, SSH activé, pays Wi-Fi réglé). Ne télécharge ni ne flashe rien.
#
#   ./prepare-pi.sh                     préflight matériel puis plan (aucune modification)
#   sudo ./prepare-pi.sh --yes          préflight matériel strict, paquets système, puis OpenTV (install.sh, 1080p compris)
#   options : --no-install (paquets seulement)   --root <dossier> (racine de test : apt jamais exécuté)
#
# Aucun apt n'est lancé avant le préflight matériel (Pi 4 Model B, Raspberry Pi OS Bookworm, arm64, KMS vc4, DRM HDMI).
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="/"
YES=0
INSTALL=1
die() { echo "opentv-prepare: $*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }
has_candidate() {   # has_candidate <paquet> : apt propose une version installable. Sortie capturée, sans pipe
  local pol=""      # (pas de faux négatif SIGPIPE/pipefail d'un « apt-cache | grep -q »)
  pol="$(LC_ALL=C apt-cache policy -- "$1" 2>/dev/null)" || return 1
  case "$pol" in
    *"Candidate: (none)"*) return 1 ;;
    *"Candidate: "?*) return 0 ;;
  esac
  return 1
}
# chargé par « source » (tests) : seulement les fonctions ci-dessus, aucune action
if (return 0 2>/dev/null); then return 0; fi

while [ $# -gt 0 ]; do
  case "$1" in
    --root) [ $# -ge 2 ] || die "--root exige un chemin"; ROOT="$2"; shift 2 ;;
    --yes) YES=1; shift ;;
    --no-install) INSTALL=0; shift ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "option inconnue : $1" ;;
  esac
done
case "$ROOT" in /*) ;; *) die "--root doit être un chemin absolu" ;; esac
if [ "$ROOT" != "/" ]; then ROOT="${ROOT%/}"; fi
LIVE=0
[ "$ROOT" = "/" ] && LIVE=1
if [ "$LIVE" = 1 ] && [ "$YES" = 1 ] && [ "$(id -u)" != 0 ]; then die "--yes exige root (sudo)"; fi
sys() {   # commande système (apt…) : exécutée seulement avec --yes sur la vraie racine
  if [ "$YES" = 0 ]; then say "[plan] $*"
  elif [ "$LIVE" = 1 ]; then "$@"
  else say "[ignoré (racine de test)] $*"; fi
}

# ---------------------------------------------------------------- préflight matériel strict (AVANT tout apt)
command -v python3 >/dev/null 2>&1 || die "python3 introuvable (présent sur Raspberry Pi OS Lite) : préflight impossible, rien installé"
PF=(python3 -I "$HERE/bin/opentv-pi" preflight --stage hardware --root "$ROOT" --text)
if [ "$LIVE" = 0 ]; then PF+=(--assume-arch arm64); fi   # racine de test : dpkg de l'hôte non pertinent
if pf="$("${PF[@]}")"; then
  say "$pf"
else
  say "$pf"
  die "préflight matériel échoué (Raspberry Pi 4 Model B, Raspberry Pi OS Bookworm arm64, KMS vc4, DRM HDMI requis) : apt non lancé, rien modifié"
fi

# ---------------------------------------------------------------- paquets système (dépôts Raspberry Pi OS)
CHROMIUM="chromium"
if [ "$LIVE" = 1 ] && command -v apt-cache >/dev/null 2>&1 && ! has_candidate chromium; then
  CHROMIUM="chromium-browser"
fi
# plymouth : image OpenTV de démarrage (thème livré et activé par install.sh, initramfs régénéré et vérifié) ; le
# paquet seul fournit plymouthd, le module script, le rendu DRM et le crochet initramfs (aucun thème tiers requis)
PKGS="cage wlr-randr $CHROMIUM v4l-utils dnsmasq-base network-manager python3 fonts-noto-core fonts-dejavu-core plymouth"
sys apt-get update
# shellcheck disable=SC2086
sys apt-get install -y --no-install-recommends $PKGS

# ---------------------------------------------------------------- OpenTV (préflight complet, 1080p, manifeste, services)
if [ "$INSTALL" = 1 ]; then
  if [ "$YES" = 0 ]; then
    say "[plan] $HERE/install.sh   (ajoute aussi video=HDMI-A-1:1920x1080@60 à cmdline.txt, sauvegardé et réversible)"
  elif [ "$LIVE" = 1 ]; then
    exec "$HERE/install.sh"
  else
    exec "$HERE/install.sh" --root "$ROOT" --skip-preflight
  fi
fi
say "préparation terminée"
