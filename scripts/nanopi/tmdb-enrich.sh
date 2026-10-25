#!/usr/bin/env bash
# Enrichissement TMDb ponctuel du catalogue du pont (jamais planifié). Le secret arrive UNIQUEMENT par
# l'environnement (TMDB_READ_TOKEN / TMDB_API_KEY) ou, à défaut, sur l'entrée standard (une ligne), puis
# est exporté pour le seul processus Python : jamais en argument, jamais sur disque, jamais affiché.
#
# Depuis le Mac (trousseau -> tube -> NanoPi, sans écho) :
#   security find-generic-password -s com.paz.opentv.tmdb.read-access-token-v4 -a paz_alex -w \
#     | ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh --dry-run'      # puis sans --dry-run
#   ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh status'             # sans secret
#   ssh <nanopi> '~/opentv-bridge/tmdb-enrich.sh rollback'           # restaure catalogue.json.tmdb.bak
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "refuse: run as the NanoPi user, not root" >&2; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="${STATE_DIR:-$HOME/.local/state/opentv-bridge}"
CAT="${CATALOGUE_FILE:-$STATE_DIR/catalogue.json}"
PY="${PYTHON:-python3}"
case "${1:-}" in
  status|rollback) exec "$PY" -I "$HERE/opentv_tmdb.py" "$1" --catalogue "$CAT" ;;
esac
if [ -z "${TMDB_READ_TOKEN:-}" ] && [ -z "${TMDB_API_KEY:-}" ]; then
  [ ! -t 0 ] || { echo "refuse: pipe the TMDb v4 read token on stdin (never as an argument)" >&2; exit 2; }
  IFS= read -r TMDB_READ_TOKEN || true
  TMDB_READ_TOKEN="${TMDB_READ_TOKEN%$'\r'}"
  export TMDB_READ_TOKEN
fi
exec "$PY" -I "$HERE/opentv_tmdb.py" enrich --catalogue "$CAT" "$@"
