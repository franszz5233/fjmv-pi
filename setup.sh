#!/bin/bash
# ============================================================================
# FJMV-PI · Setup para Raspberry Pi 4B con Kali Linux
#   - Puente de cámara (reemplaza tu PC; reporta a jmhome)
#   - Agente Sniffing (terminal remota vía jmhome, sin túneles externos)
#   - IP fija 192.168.100.253 en Ethernet (eth0)
#   - SSH + hotspot JM__HOME_6G (opcional, etapa 2)
#
# USO (en la Pi, una sola vez):
#   sudo bash setup.sh
# ============================================================================
set -e
[ "$EUID" -ne 0 ] && { echo "Corre con sudo: sudo bash setup.sh"; exit 1; }

# ---- Parámetros (ajusta si hace falta) ----
PI_USER="${SUDO_USER:-kali}"
FIXED_IP="192.168.100.253"
GATEWAY="192.168.100.1"
DNS="8.8.8.8"
JMHOME="https://jmhome.fly.dev"
SSH_PASS="5545894519"
HOTSPOT_SSID="JM__HOME_6G"
HOTSPOT_PASS="5545894519"
REPO_DIR="/opt/fjmv-pi"

echo "================================================================"
echo " FJMV-PI setup  ·  usuario=$PI_USER  ·  IP fija=$FIXED_IP"
echo "================================================================"

# ---- 1) Paquetes ----
echo "[*] Instalando paquetes (puede tardar)…"
apt-get update -y
apt-get install -y python3 python3-pip python3-opencv python3-numpy \
    python3-requests git curl ttyd openssh-server \
    hostapd dnsmasq iptables tcpdump aircrack-ng net-tools wireless-tools
# fastapi/uvicorn por pip (no siempre en apt)
pip3 install --break-system-packages fastapi "uvicorn[standard]" 2>/dev/null \
  || pip3 install fastapi "uvicorn[standard]"

# ---- 2) Código (este repo ya está en $REPO_DIR si clonaste; si no, cópialo) ----
if [ ! -d "$REPO_DIR/server" ]; then
  echo "[*] Copiando código a $REPO_DIR …"
  mkdir -p "$REPO_DIR"
  cp -r "$(dirname "$(readlink -f "$0")")/." "$REPO_DIR/"
fi
cd "$REPO_DIR"

# ---- 3) Modelo de detección de rostros (res10) ----
mkdir -p server/models
mv -f server/face_deploy.prototxt server/models/ 2>/dev/null || true
if [ ! -f server/models/res10_face.caffemodel ]; then
  echo "[*] Descargando modelo de rostros…"
  curl -sL -o server/models/res10_face.caffemodel \
    "https://raw.githubusercontent.com/opencv/opencv_3rdparty/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel"
fi
[ -f server/models/face_deploy.prototxt ] || curl -sL -o server/models/face_deploy.prototxt \
  "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt"

# ---- 4) config.json del puente (cámara + nube) ----
if [ ! -f config.json ]; then
  read -rp "Contraseña RTSP de la cámara (admin): " CAM_PASS
  CAM_PASS_ENC=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$CAM_PASS")
  read -rp "IP de la cámara [192.168.100.254]: " CAM_IP; CAM_IP=${CAM_IP:-192.168.100.254}
  cat > config.json <<EOF
{
  "port": 8090,
  "ai": { "enabled": true },
  "cloud": { "url": "$JMHOME", "token": "fjmv-cam-5233", "camera_id": "cam1", "name": "Entrada" },
  "cameras": [
    { "id": 1, "name": "Cámara AI", "url": "rtsp://admin:${CAM_PASS_ENC}@${CAM_IP}:554/cam/realmonitor?channel=1&subtype=0" }
  ]
}
EOF
fi

# ---- 5) IP fija en Ethernet (NetworkManager) ----
echo "[*] Fijando IP $FIXED_IP en eth0…"
nmcli con mod "Wired connection 1" ipv4.addresses "$FIXED_IP/24" ipv4.gateway "$GATEWAY" \
   ipv4.dns "$DNS" ipv4.method manual 2>/dev/null || \
nmcli con add type ethernet ifname eth0 con-name fjmv-eth ipv4.method manual \
   ipv4.addresses "$FIXED_IP/24" ipv4.gateway "$GATEWAY" ipv4.dns "$DNS" 2>/dev/null || true
nmcli con up "Wired connection 1" 2>/dev/null || nmcli con up fjmv-eth 2>/dev/null || true

# ---- 6) SSH + contraseña ----
echo "[*] SSH on, pass de $PI_USER = $SSH_PASS"
systemctl enable ssh && systemctl restart ssh
echo "${PI_USER}:${SSH_PASS}" | chpasswd

# ---- 7) Servicios systemd (puente cámara + agente) ----
echo "[*] Creando servicios…"
cat > /etc/systemd/system/fjmv-camara.service <<EOF
[Unit]
Description=FJMV puente de camara
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$REPO_DIR
ExecStart=/usr/bin/python3 -m server.main --port 8090
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/fjmv-agent.service <<EOF
[Unit]
Description=FJMV agente sniffing (terminal via jmhome)
After=network-online.target
Wants=network-online.target
[Service]
WorkingDirectory=$REPO_DIR
Environment=JMHOME=$JMHOME
Environment=PI_ID=pi-fjmv
Environment=PI_NAME=Raspberry Sniffer
Environment=SNIFF_TOKEN=fjmv-sniff-5233
ExecStart=/usr/bin/python3 $REPO_DIR/agent.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable fjmv-camara fjmv-agent
systemctl restart fjmv-camara fjmv-agent

echo "================================================================"
echo " LISTO (Etapa 1):"
echo "   IP fija:        $FIXED_IP  (Ethernet)"
echo "   SSH:            ssh ${PI_USER}@${FIXED_IP}   (pass ${SSH_PASS})"
echo "   Puente cámara:  corriendo (reporta a jmhome)"
echo "   Agente terminal: corriendo (visible en jmhome > Admin > Sniffing)"
echo ""
echo " Para el HOTSPOT + sniffing (etapa 2):  sudo bash setup.sh hotspot"
echo "================================================================"

# ============================ ETAPA 2: HOTSPOT JM__HOME_6G ============================
if [ "$1" = "hotspot" ]; then
  echo "[*] Configurando hotspot $HOTSPOT_SSID en wlan0 (internet por eth0)…"
  systemctl stop hostapd dnsmasq 2>/dev/null || true
  nmcli radio wifi off 2>/dev/null || true
  rfkill unblock wlan 2>/dev/null || true
  ip addr flush dev wlan0 || true
  ip addr add 10.66.0.1/24 dev wlan0 || true
  ip link set wlan0 up || true

  cat > /etc/hostapd/hostapd.conf <<EOF
interface=wlan0
driver=nl80211
ssid=$HOTSPOT_SSID
hw_mode=g
channel=6
auth_algs=1
wpa=2
wpa_passphrase=$HOTSPOT_PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
  sed -i 's|#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true

  cat > /etc/dnsmasq.d/fjmv.conf <<EOF
interface=wlan0
dhcp-range=10.66.0.10,10.66.0.200,12h
EOF

  echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-fjmv.conf
  sysctl -p /etc/sysctl.d/99-fjmv.conf
  iptables -t nat -A POSTROUTING -o eth0 -j MASQUERADE
  iptables -A FORWARD -i eth0 -o wlan0 -m state --state RELATED,ESTABLISHED -j ACCEPT
  iptables -A FORWARD -i wlan0 -o eth0 -j ACCEPT

  systemctl unmask hostapd 2>/dev/null || true
  systemctl enable hostapd dnsmasq
  systemctl restart dnsmasq hostapd
  echo "[*] Hotspot $HOTSPOT_SSID arriba. Los dispositivos que se conecten pasan por la Pi (sniffing con tcpdump)."
  echo "    Ej. de sniffing desde la app:  timeout 15 tcpdump -i wlan0 -nn -c 50"
fi
