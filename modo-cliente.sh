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

echo "[*] Actualizando código y reiniciando el bridge (modo ligero automático)..."
cd /opt/fjmv-pi && git fetch -q && git reset --hard origin/main >/dev/null
systemctl restart fjmv-camara fjmv-agent
sleep 4
echo "[*] Estado:"
systemctl is-active fjmv-camara fjmv-agent
free -m | awk '/Mem:|Swap:/{print "    "$0}'
echo "[*] LISTO: modo cliente. La Pi va por WiFi del módem; sin hotspot."
echo "    Verifica el MODO LIGERO:  journalctl -u fjmv-camara -n 20 | grep LIGERO"
