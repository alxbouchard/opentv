#!/usr/bin/env bash
# Retire seulement le service opentv-bridge (idempotent). Le stop tue navigateur + compositeur (KillMode=control-group),
# ce qui rend le maître DRM : le tableau de bord fbdev redevient visible. Contrôle lecture seule ensuite.
set -uo pipefail
UNIT=opentv-bridge.service
systemctl --user disable --now "$UNIT" 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/$UNIT"
systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT" 2>/dev/null || true
if systemctl --user is-active --quiet "$UNIT"; then echo "rollback FAILED: $UNIT still active" >&2; exit 1; fi
if pgrep -u "$(id -u)" -f 'opentv-bridge-[0-9]+|--user-data-dir=.*opentv-bridge-' >/dev/null; then
  echo "rollback WARNING: leftover browser/compositor processes" >&2; exit 1
fi
echo "rollback ok: $UNIT removed (code in ~/opentv-bridge, env and state kept)"
systemctl --user is-active muse-display.service
