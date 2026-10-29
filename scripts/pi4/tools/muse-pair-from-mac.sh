#!/bin/bash
# RÉCUPÉRATION ADMINISTRATEUR SEULEMENT. Le parcours normal est sur la TV : Réglages → Gérer les assistants → Muse →
# « Autoriser et connecter » (QR, jeton chiffré par le téléphone, helper root dédié ; README §5 ter). Ce script reste
# pour un administrateur qui a SSH (ex. TV indisponible) ; il n'est proposé nulle part dans l'interface.
# Mac -> boîte OpenTV : autorisation du pilotage par Muse puis appariement du pont, sans jamais exposer le
# jeton SDK Muse. Jamais installé sur la Pi, jamais lancé par un agent : l'administrateur le lance dans son terminal.
#
#   bash scripts/pi4/tools/muse-pair-from-mac.sh <admin>@<hôte-opentv>
#
# - lit `opentv-pi muse status` (aucun secret) ;
# - pilotage pas encore autorisé : montre le plan, puis n'écrit qu'après « oui » tapé par Alex
#   (`muse authorize-control --yes` : la seule ligne « muse » de la politique change) ;
# - jeton SDK absent : ouvre la page des jetons (connexion et création faites par Alex), lit le jeton SANS écho,
#   vérifie seulement sa forme (mgst_ + 43), le passe UNE fois par SSH sur l'entrée standard de
#   `muse pair --token-stdin` (jamais en argument, fichier, historique, journal ni sortie), puis l'efface ;
# - garde la fenêtre Bluetooth attachée et affiche le rapport final de `muse pair` (aucun jeton dedans).
# Exige sudo sans mot de passe pour l'administrateur SSH (sudo -n) : sinon refus clair, rien envoyé.
# Compatible bash 3.2 (macOS).
set -euo pipefail
umask 077

TARGET="${1:-}"
if [ -z "$TARGET" ]; then
  echo "usage : $0 <admin>@<hôte-opentv>" >&2
  exit 2
fi
case "$TARGET" in
  *[!A-Za-z0-9@._-]*|-*) echo "cible SSH invalide" >&2; exit 2 ;;
esac
REMOTE=/opt/opentv/bin/opentv-pi
TOKEN_PAGE="https://gadgets.muse.ai/"
MUSE_TOKEN=""

cleanup() {
  MUSE_TOKEN=""
  unset MUSE_TOKEN
  stty echo 2>/dev/null || true
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM HUP

remote() {   # commande fixe sur la boîte (aucune donnée d'Alex dans la ligne de commande)
  ssh -o ConnectTimeout=10 "$TARGET" "sudo -n $REMOTE muse $*"
}

echo "1/4 · État du pont Muse sur $TARGET…"
if ! status="$(remote status)"; then
  echo "Impossible de lire « muse status » (SSH ou sudo -n refusé) : rien changé." >&2
  exit 1
fi

case "$status" in
  *'"control_authorized": true'*)
    echo "2/4 · Muse est déjà autorisé à piloter OpenTV." ;;
  *)
    echo "2/4 · Muse n'est pas encore autorisé à piloter OpenTV."
    echo "      Il pourra : proposer un film à l'écran, naviguer dans OpenTV, noter tes goûts quand tu le dis,"
    echo "      afficher un court message, et lancer un film du catalogue par le lecteur officiel quand tu le"
    echo "      lui demandes. Retrait à tout moment : Réglages > Gérer les assistants, ou « muse revoke-control »."
    remote authorize-control || true   # plan seul : rien n'est écrit sans --yes
    printf 'Autoriser Muse à piloter OpenTV ? Tape « oui » puis Entrée : '
    IFS= read -r answer
    if [ "$answer" != "oui" ]; then
      echo "Rien changé."
      exit 1
    fi
    remote authorize-control --yes
    ;;
esac

need_token=1
case "$status" in
  *'"sdk_token": "present"'*) need_token=0 ;;
esac

if [ "$need_token" -eq 1 ]; then
  echo "3/4 · Jeton SDK Muse : la page s'ouvre ; connecte-toi et crée le jeton toi-même (Account > SDK tokens)."
  open "$TOKEN_PAGE" >/dev/null 2>&1 || true
  printf 'Colle le jeton (mgst_…, invisible) puis Entrée : '
  IFS= read -r -s MUSE_TOKEN
  echo
  case "$MUSE_TOKEN" in
    mgst_*) ;;
    *) echo "Ce n'est pas un jeton SDK Muse (mgst_…) : rien envoyé." >&2; exit 2 ;;
  esac
  case "$MUSE_TOKEN" in
    *[!A-Za-z0-9_-]*) echo "Caractères inattendus dans le jeton : rien envoyé." >&2; exit 2 ;;
  esac
  if [ "${#MUSE_TOKEN}" -ne 48 ]; then
    echo "Longueur inattendue (48 caractères attendus) : rien envoyé." >&2
    exit 2
  fi
  echo "4/4 · Appariement : garde l'app Muse ouverte (Réglages > Appareils > Mode développeur > +)."
  rc=0
  # printf est une commande interne : le jeton ne passe jamais dans la liste des processus
  printf '%s\n' "$MUSE_TOKEN" | ssh -o ConnectTimeout=10 "$TARGET" "sudo -n $REMOTE muse pair --token-stdin" || rc=$?
  MUSE_TOKEN=""
else
  echo "3/4 · Jeton SDK déjà présent sur la boîte : rien à saisir."
  echo "4/4 · Appariement / réémission de l'identité du pont (garde l'app Muse ouverte si une fenêtre s'ouvre)."
  rc=0
  remote pair </dev/null || rc=$?
fi

if [ "$rc" -ne 0 ]; then
  echo "Appariement non terminé (code $rc) : voir le rapport ci-dessus ; rien d'autre n'a été changé." >&2
  exit "$rc"
fi
echo "Ensuite, dans Muse : « propose-moi un film sur OpenTV » — la fiche « Proposé par Muse » doit s'ouvrir sur la TV."
