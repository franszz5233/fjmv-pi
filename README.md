# FJMV-PI — Raspberry Pi 4B (Kali) como puente de cámara + sniffing

Reemplaza la PC: la Pi lee la cámara y reporta a **jmhome.fly.dev**, y expone una
**terminal** en jmhome (Admin → Sniffing) usando jmhome como puente (sin túneles).

## Instalar (en la Pi, una vez)
```bash
sudo apt -y install git
git clone https://github.com/franszz5233/fjmv-pi.git /opt/fjmv-pi
cd /opt/fjmv-pi
sudo bash setup.sh            # etapa 1: cámara + agente + IP fija + SSH
sudo bash setup.sh hotspot    # etapa 2: hotspot JM__HOME_6G + sniffing
```
- SSH: `ssh kali@192.168.100.253` (pass 5545894519)
- En la app: **Admin → Sniffing** → elige la Pi → escribe comandos.
