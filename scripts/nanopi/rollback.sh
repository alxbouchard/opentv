#!/usr/bin/env bash
# Remove the OpenTV autostart user service. Idempotent. Read-only check of the dashboard afterwards.
set -uo pipefail
UNIT=opentv-autostart.service
systemctl --user disable --now "$UNIT" 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/$UNIT"
systemctl --user daemon-reload
systemctl --user reset-failed "$UNIT" 2>/dev/null || true
if systemctl --user is-active --quiet "$UNIT"; then echo "rollback FAILED: $UNIT still active" >&2; exit 1; fi
echo "rollback ok: $UNIT removed (state/logs kept in ~/.local/state/opentv-autostart)"
# dashboard must be untouched: read-only check, user unit first then system unit
systemctl --user is-active muse-display.service 2>/dev/null || systemctl is-active muse-display.service
echo "TV side (Alex, physical): restore Developer Mode Host PC IP to 192.168.2.13 and reboot the TV."
