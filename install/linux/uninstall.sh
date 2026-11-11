#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
systemctl disable --now victus-fan.service 2>/dev/null || true
rm -f /etc/systemd/system/victus-fan.service /usr/local/bin/victus-fan
systemctl daemon-reload
rm -rf /opt/victus-fan
echo "Entfernt. Konfiguration bleibt in /etc/victus-fan."
