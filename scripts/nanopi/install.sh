#!/usr/bin/env bash
# Install the OpenTV autostart user service for the current (non-root) user. Never touches muse-display.
set -euo pipefail
[ "$(id -u)" != 0 ] || { echo "refuse: run as the NanoPi user, not root" >&2; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
APPDIR="$HOME/opentv-autostart"; UNITDIR="$HOME/.config/systemd/user"; CONF="$HOME/.config/opentv-autostart"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'
python3 -I -m py_compile "$SRC/opentv_autostart.py"
mkdir -p "$APPDIR" "$UNITDIR" "$CONF"
[ -f "$APPDIR/opentv_autostart.py" ] && cp -p "$APPDIR/opentv_autostart.py" "$APPDIR/opentv_autostart.py.prev"
install -m 0644 "$SRC/opentv_autostart.py" "$APPDIR/opentv_autostart.py"
install -m 0644 "$SRC/opentv-autostart.service" "$UNITDIR/opentv-autostart.service"
[ -f "$CONF/env" ] || printf 'TV_HOST=192.168.2.14\nLAUNCH_ON_START=0\nDRY_RUN=1\nCEC_CMD=cec-client -s -d 1\nOFF_DEBOUNCE_S=15\n' > "$CONF/env"
if command -v systemd-analyze >/dev/null; then systemd-analyze --user verify "$UNITDIR/opentv-autostart.service"; fi
systemctl --user daemon-reload
systemctl --user enable --now opentv-autostart.service
systemctl --user is-active opentv-autostart.service
loginctl show-user "$(id -un)" -p Linger
