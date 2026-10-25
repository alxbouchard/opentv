#!/usr/bin/env bash
# Installe le service utilisateur opentv-bridge (non root). Ne touche jamais muse-display, musegadget,
# muse-cec-pinmux : lecture seule de leur état avant/après, échec si le tableau de bord a bougé.
# Défaut sûr : DISPLAY_BACKEND=off (API vivante, sessions refusées en 503) tant que le terrain n'a pas validé
# weston + chromium + seatd. Usage : ./install-bridge.sh            (installe, démarre en mode off)
#                                    ./install-bridge.sh --no-start (installe sans démarrer)
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "refuse: run as the NanoPi user, not root" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/opentv-bridge"; UNITDIR="$HOME/.config/systemd/user"; CONF="$UNITDIR/opentv-bridge.env"
UNIT=opentv-bridge.service
dash() { systemctl --user show muse-display.service -p ActiveState -p MainPID -p NRestarts --value | tr '\n' ' '; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
python3 -I -m py_compile "$SRC/opentv_bridge.py"
BEFORE="$(dash)"; echo "muse-display before: $BEFORE"
printf '%s\n' "$BEFORE" | grep -Eq '(^|[[:space:]])active([[:space:]]|$)' || { echo "refuse: muse-display not active; fix the dashboard first" >&2; exit 1; }
mkdir -p "$APPDIR" "$UNITDIR"
for f in opentv_bridge.py opentv_bridge_ui.html; do
  [ -f "$APPDIR/$f" ] && cp -p "$APPDIR/$f" "$APPDIR/$f.prev"
  install -m 0644 "$SRC/$f" "$APPDIR/$f"
done
install -m 0644 "$SRC/opentv-bridge.service" "$UNITDIR/$UNIT"
[ -f "$CONF" ] || printf 'DISPLAY_BACKEND=off\nSOURCE_APPROVED=0\nALLOW_IPS=192.168.2.14\nPORT=8097\nUI_PORT=8098\nSITE_ROOT=https://fakrov.com/\nOUTPUT_MODE=1920x1080\nOUTPUT_NAME=HDMI-A-1\nCATALOGUE_REFRESH_S=0\nROUTING_REFRESH_S=0\nSESSION_CHECK_S=300\nCATALOGUE_IDLE_S=300\nPLAYER_MAX_S=14400\nHEARTBEAT_S=30\n' > "$CONF"
chmod 0600 "$CONF"
if command -v systemd-analyze >/dev/null; then systemd-analyze --user verify "$UNITDIR/$UNIT"; fi
systemctl --user daemon-reload
if [ "${1:-}" = "--no-start" ]; then echo "installed, not started"; exit 0; fi
systemctl --user enable "$UNIT"
systemctl --user restart "$UNIT"
sleep 2
systemctl --user is-active "$UNIT"
AFTER="$(dash)"; echo "muse-display after:  $AFTER"
[ "$BEFORE" = "$AFTER" ] || { echo "ALERT: muse-display state changed ($BEFORE -> $AFTER); running rollback" >&2; bash "$SRC/rollback-bridge.sh" || true; exit 1; }
echo "ok: $UNIT installed (DISPLAY_BACKEND=$(sed -n 's/^DISPLAY_BACKEND=//p' "$CONF"))"
