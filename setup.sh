#!/bin/bash
# ============================================================================
# FJMV-PI · Setup de la Raspberry (lo corre cloud-init en el 1er arranque).
# SIN secretos en el repo: la cámara/tokens llegan por variables de entorno
# (las pone el user-data del cloud-init, que vive en la SD, no en GitHub).
#   Vars:  CAM_PASS  CAM_IP  JMHOME  CAM_TOKEN  SNIFF_TOKEN  PI_ID  PI_NAME
# WiFi y contraseña de SSH las maneja cloud-init (network-config / chpasswd).
# ============================================================================
set -e
[ "$EUID" -ne 0 ] && { echo "usa sudo"; exit 1; }

CAM_PASS="${CAM_PASS:-}"
CAM_IP="${CAM_IP:-192.168.100.254}"
JMHOME="${JMHOME:-https://jmhome.fly.dev}"
CAM_TOKEN="${CAM_TOKEN:-fjmv-cam-5233}"
SNIFF_TOKEN="${SNIFF_TOKEN:-fjmv-sniff-5233}"
PI_ID="${PI_ID:-pi-fjmv}"
PI_NAME="${PI_NAME:-Raspberry FJMV}"
REPO_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$REPO_DIR"
echo "==== FJMV-PI setup · repo=$REPO_DIR · cam=$CAM_IP ===="

export DEBIAN_FRONTEND=noninteractive
# Arreglo llave GPG de Kali (imágenes viejas traen la llave expirada -> apt falla)
curl -fsSL https://archive.kali.org/archive-keyring.gpg \
  -o /usr/share/keyrings/kali-archive-keyring.gpg 2>/dev/null || true
apt-get update -y
apt-get install -y python3 python3-pip python3-opencv python3-numpy \
    python3-requests git curl net-tools wireless-tools \
    bluez bluetooth rfkill python3-dbus libglib2.0-dev \
    hostapd dnsmasq iptables tcpdump 2>/dev/null || true
pip3 install --break-system-packages fastapi "uvicorn[standard]" bleak 2>/dev/null \
  || pip3 install fastapi "uvicorn[standard]" bleak 2>/dev/null || true

# modelo de rostros
mkdir -p server/models
mv -f server/face_deploy.prototxt server/models/ 2>/dev/null || true
[ -f server/models/res10_face.caffemodel ] || curl -sL -o server/models/res10_face.caffemodel \
  "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
[ -f server/models/face_deploy.prototxt ] || curl -sL -o server/models/face_deploy.prototxt \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt"

# config.json (la clave de la cámara viene por env -> no queda en GitHub)
CAM_PASS_ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$CAM_PASS")
cat > config.json <<EOF
{
  "port": 8090,
  "ai": { "enabled": true },
  "cloud": { "url": "$JMHOME", "token": "$CAM_TOKEN", "camera_id": "cam1", "name": "Entrada" },
  "cameras": [
    { "id": 1, "name": "Cámara AI", "url": "rtsp://admin:${CAM_PASS_ENC}@${CAM_IP}:554/cam/realmonitor?channel=1&subtype=0" }
  ]
}
EOF

# servicios systemd (arrancan solos en cada boot)
cat > /etc/systemd/system/fjmv-camara.service <<EOF
[Unit]
Description=FJMV puente camara
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 -m server.main --port 8090
Restart=always
RestartSec=8
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/fjmv-agent.service <<EOF
[Unit]
Description=FJMV agente sniffing
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$REPO_DIR
Environment=JMHOME=$JMHOME
Environment=PI_ID=$PI_ID
Environment=PI_NAME=$PI_NAME
Environment=SNIFF_TOKEN=$SNIFF_TOKEN
ExecStart=/usr/bin/python3 $REPO_DIR/agent.py
Restart=always
RestartSec=8
[Install]
WantedBy=multi-user.target
EOF
# Agente Bluetooth (BLE) — chapas IoT vía relé jmhome (Bluetooth)
cat > /etc/systemd/system/fjmv-ble.service <<EOF
[Unit]
Description=FJMV agente Bluetooth (BLE)
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target
[Service]
WorkingDirectory=$REPO_DIR
Environment=JMHOME=$JMHOME
Environment=PI_ID=$PI_ID
Environment=PI_NAME=$PI_NAME
Environment=BLE_TOKEN=${BLE_TOKEN:-$SNIFF_TOKEN}
ExecStartPre=-/usr/sbin/rfkill unblock bluetooth
ExecStart=/usr/bin/python3 $REPO_DIR/ble_agent.py
Restart=always
RestartSec=8
[Install]
WantedBy=multi-user.target
EOF

# Encender la radio Bluetooth
systemctl enable bluetooth 2>/dev/null || true
systemctl start bluetooth 2>/dev/null || true
rfkill unblock bluetooth 2>/dev/null || true

# Desactivar ahorro de energía WiFi (evita que el wlan se duerma y corte tras horas)
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<'EOF'
[connection]
wifi.powersave = 2
EOF
cat > /usr/local/sbin/fjmv-wifipower.sh <<'EOF'
#!/bin/sh
for n in /sys/class/net/wlan*; do iw dev "$(basename "$n")" set power_save off 2>/dev/null || true; done
EOF
chmod +x /usr/local/sbin/fjmv-wifipower.sh
cat > /etc/systemd/system/fjmv-wifipower.service <<'EOF'
[Unit]
Description=FJMV desactiva ahorro de energia WiFi
After=NetworkManager.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fjmv-wifipower.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fjmv-camara fjmv-agent fjmv-wifipower fjmv-ble
systemctl restart fjmv-camara fjmv-agent fjmv-ble
/usr/local/sbin/fjmv-wifipower.sh 2>/dev/null || true
echo "==== LISTO. Cámara + agente Sniffing + agente Bluetooth en jmhome ===="
