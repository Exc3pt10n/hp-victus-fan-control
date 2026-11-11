#!/usr/bin/env bash
# Installiert die Lüftersteuerung als systemd-Dienst (Linux, Kernel mit hp-wmi).
set -euo pipefail
[[ $EUID -eq 0 ]] || exec sudo "$0" "$@"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
python3 -m venv /opt/victus-fan/venv
/opt/victus-fan/venv/bin/pip install --quiet --upgrade "$REPO"
install -d /etc/victus-fan
[[ -f /etc/victus-fan/config.toml ]] || install -m 644 "$REPO/config.example.toml" /etc/victus-fan/config.toml
ln -sf /opt/victus-fan/venv/bin/victus-fan /usr/local/bin/victus-fan
/opt/victus-fan/venv/bin/victus-fan diagnose
install -m 644 "$REPO/install/linux/victus-fan.service" /etc/systemd/system/victus-fan.service
systemctl daemon-reload
systemctl enable --now victus-fan.service
echo "Fertig. Status: victus-fan status | Log: journalctl -u victus-fan -f"
