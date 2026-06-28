#!/bin/bash
# ============================================================================
# FJMV-PI · Modo CLIENTE (sin hotspot) — para Raspberry Pi Zero 2 W.
# Una sola radio WiFi conectada al módem (JM__HOME). Llega a la cámara por la
# misma red y reporta a jmhome. Apaga el hotspot, agrega swap, activa modo
# ligero y reinicia el bridge.
#   sudo bash modo-cliente.sh
# ============================================================================
set -e
[ "$EUID" -ne 0 ] && { echo "usa: sudo bash modo-cliente.sh"; exit 1; }

echo "[*] Apagando hotspot (no hay 2da WiFi en la Zero 2 W)..."
for s in hostapd dnsmasq fjmv-nat; do
  systemctl disable --now "$s" 2>/dev/null || true
done
rm -f /etc/dnsmasq.d/fjmv.conf 2>/dev/null || true

echo "[*] Swap de 1G (red de seguridad para 512MB de RAM)..."
if ! grep -q '/swapfile' /etc/fstab 2>/dev/null; then
  fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
swapon /swapfile 2>/dev/null || true

echo "[*] Desactivando ahorro de energía WiFi (causa de cortes tras horas)..."
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
systemctl enable fjmv-wifipower 2>/dev/null || true
systemctl restart NetworkManager 2>/dev/null || true
sleep 3
/usr/local/sbin/fjmv-wifipower.sh

echo "[*] Anti disco-lleno: journald acotado + recorte de logs gigantes..."
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=40M\nRuntimeMaxUse=20M\n' > /etc/systemd/journald.conf.d/fjmv.conf
systemctl restart systemd-journald 2>/dev/null || true
cat > /usr/local/sbin/fjmv-logguard.sh <<'EOF'
#!/bin/sh
# Si un log crece sin control (p.ej. driver WiFi spameando), recórtalo a 0.
for f in /var/log/syslog /var/log/kern.log /var/log/messages /var/log/daemon.log /var/log/fjmv.log; do
  [ -f "$f" ] || continue
  sz=$(stat -c%s "$f" 2>/dev/null || echo 0)
  [ "$sz" -gt 104857600 ] && : > "$f"   # > 100MB -> truncar
done
EOF
chmod +x /usr/local/sbin/fjmv-logguard.sh
cat > /etc/systemd/system/fjmv-logguard.service <<'EOF'
[Unit]
Description=FJMV recorta logs gigantes (anti disco lleno)
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/fjmv-logguard.sh
EOF
cat > /etc/systemd/system/fjmv-logguard.timer <<'EOF'
[Unit]
Description=FJMV logguard cada 30 min
[Timer]
OnBootSec=10min
OnUnitActiveSec=30min
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now fjmv-logguard.timer 2>/dev/null || true

echo "[*] Forzar modo ligero en el bridge (override del servicio)..."
mkdir -p /etc/systemd/system/fjmv-camara.service.d
printf '[Service]\nEnvironment=FJMV_LITE=1\nEnvironment=PYTHONUNBUFFERED=1\n' \
  > /etc/systemd/system/fjmv-camara.service.d/lite.conf
systemctl daemon-reload

echo "[*] Actualizando código y reiniciando el bridge (modo ligero automático)..."
cd /opt/fjmv-pi && git fetch -q && git reset --hard origin/main >/dev/null
systemctl restart fjmv-camara fjmv-agent
sleep 4
echo "[*] Estado:"
systemctl is-active fjmv-camara fjmv-agent
free -m | awk '/Mem:|Swap:/{print "    "$0}'
echo "[*] LISTO: modo cliente. La Pi va por WiFi del módem; sin hotspot."
echo "    Verifica el MODO LIGERO:  journalctl -u fjmv-camara -n 20 | grep LIGERO"
