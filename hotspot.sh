#!/bin/bash
# ============================================================================
# FJMV-PI · Hotspot JM__HOME_6G + sniffing (AUTO en cada arranque)
# Detecta solo: interfaz de INTERNET (default route) y la del AP (la 2da WiFi USB).
# NO toca la interfaz de internet -> no te deja sin conexión.
#   sudo bash hotspot.sh
# ============================================================================
set -e
[ "$EUID" -ne 0 ] && { echo "usa sudo"; exit 1; }
SSID="${HOTSPOT_SSID:-JM__HOME_6G}"
PASS="${HOTSPOT_PASS:-5545894519}"
AP_IP="10.66.0.1"

NET_IF=$(ip route | awk '/default/{print $5; exit}')
AP_IF=""
for w in /sys/class/net/wlan*; do n=$(basename "$w"); [ "$n" != "$NET_IF" ] && AP_IF="$n" && break; done
if [ -z "$AP_IF" ]; then
  echo "[!] No encuentro una 2da interfaz WiFi (¿conectaste la antena USB?)."
  echo "    Aborto para NO dejar la Pi sin internet/acceso."
  exit 1
fi
echo "[*] Internet por: $NET_IF   ·   Hotspot ($SSID) en: $AP_IF"

export DEBIAN_FRONTEND=noninteractive
apt-get install -y hostapd dnsmasq iptables 2>/dev/null || true

# NetworkManager no debe tocar la interfaz del AP
nmcli dev set "$AP_IF" managed no 2>/dev/null || true
ip addr flush dev "$AP_IF" 2>/dev/null || true
ip addr add ${AP_IP}/24 dev "$AP_IF" 2>/dev/null || true
ip link set "$AP_IF" up 2>/dev/null || true

cat > /etc/hostapd/hostapd.conf <<EOF
interface=$AP_IF
driver=nl80211
ssid=$SSID
hw_mode=g
channel=6
auth_algs=1
wpa=2
wpa_passphrase=$PASS
wpa_key_mgmt=WPA-PSK
rsn_pairwise=CCMP
EOF
sed -i 's|#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd 2>/dev/null || true

cat > /etc/dnsmasq.d/fjmv.conf <<EOF
interface=$AP_IF
bind-interfaces
dhcp-range=10.66.0.10,10.66.0.200,12h
EOF

echo 'net.ipv4.ip_forward=1' > /etc/sysctl.d/99-fjmv.conf
sysctl -p /etc/sysctl.d/99-fjmv.conf >/dev/null 2>&1 || true

# NAT reaplicado en cada boot (persistente, sin depender de iptables-save)
cat > /usr/local/sbin/fjmv-nat.sh <<EOF
#!/bin/bash
NET_IF=\$(ip route | awk '/default/{print \$5; exit}')
ip addr add ${AP_IP}/24 dev $AP_IF 2>/dev/null || true
ip link set $AP_IF up 2>/dev/null || true
iptables -t nat -C POSTROUTING -o "\$NET_IF" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "\$NET_IF" -j MASQUERADE
iptables -C FORWARD -i $AP_IF -o "\$NET_IF" -j ACCEPT 2>/dev/null || iptables -A FORWARD -i $AP_IF -o "\$NET_IF" -j ACCEPT
iptables -C FORWARD -i "\$NET_IF" -o $AP_IF -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || iptables -A FORWARD -i "\$NET_IF" -o $AP_IF -m state --state RELATED,ESTABLISHED -j ACCEPT
EOF
chmod +x /usr/local/sbin/fjmv-nat.sh

cat > /etc/systemd/system/fjmv-nat.service <<EOF
[Unit]
Description=FJMV NAT hotspot
After=hostapd.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fjmv-nat.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

systemctl unmask hostapd 2>/dev/null || true
systemctl daemon-reload
systemctl enable hostapd dnsmasq fjmv-nat
systemctl restart dnsmasq || true
systemctl restart hostapd || true
/usr/local/sbin/fjmv-nat.sh
echo "[*] LISTO: hotspot $SSID en $AP_IF (auto en cada boot). Internet por $NET_IF."
echo "    Sniffing ej.:  timeout 15 tcpdump -i $AP_IF -nn -c 50"
