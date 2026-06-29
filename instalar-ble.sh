#!/bin/bash
# ============================================================================
# FJMV-PI · Instala/actualiza SOLO el agente Bluetooth (BLE) en una Pi que ya
# tiene fjmv-pi corriendo. Idempotente. Correr con sudo desde /opt/fjmv-pi.
#   sudo bash /opt/fjmv-pi/instalar-ble.sh
# ============================================================================
set -e
[ "$EUID" -ne 0 ] && { echo "usa sudo"; exit 1; }
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO_DIR"
echo "==== FJMV-PI · instalar agente BLE · repo=$REPO_DIR ===="

export DEBIAN_FRONTEND=noninteractive
apt-get update -y || true
# BlueZ + utilidades Bluetooth + dbus (bleak los necesita)
apt-get install -y bluez bluetooth rfkill libglib2.0-dev python3-dbus 2>/dev/null || true
pip3 install --break-system-packages bleak 2>/dev/null \
  || pip3 install bleak 2>/dev/null || true

# Encender la radio y el servicio BlueZ
systemctl enable bluetooth 2>/dev/null || true
systemctl start bluetooth 2>/dev/null || true
rfkill unblock bluetooth 2>/dev/null || true
hciconfig hci0 up 2>/dev/null || true

# El relé de jmhome acepta tanto fjmv-ble-5233 como el token de sniffing, así que
# usamos el default y no hay riesgo de desajuste.
BLE_TOKEN="${BLE_TOKEN:-fjmv-ble-5233}"
PI_ID="${PI_ID:-$(grep -h '^Environment=PI_ID=' /etc/systemd/system/fjmv-agent.service 2>/dev/null | head -1 | cut -d= -f3)}"
PI_ID="${PI_ID:-pi-fjmv}"
JMHOME="${JMHOME:-https://jmhome.fly.dev}"

cat > /etc/systemd/system/fjmv-ble.service <<EOF
[Unit]
Description=FJMV agente Bluetooth (BLE) — chapas IoT vía relé jmhome
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target
[Service]
WorkingDirectory=$REPO_DIR
Environment=JMHOME=$JMHOME
Environment=PI_ID=$PI_ID
Environment=PI_NAME=Raspberry FJMV
Environment=BLE_TOKEN=$BLE_TOKEN
ExecStartPre=-/usr/sbin/rfkill unblock bluetooth
ExecStart=/usr/bin/python3 $REPO_DIR/ble_agent.py
Restart=always
RestartSec=8
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fjmv-ble
systemctl restart fjmv-ble
sleep 2
echo "---- estado ----"
systemctl --no-pager -l status fjmv-ble | head -12 || true
echo "==== LISTO. Abre jmhome > Admin > Bluetooth y escanea. ===="
