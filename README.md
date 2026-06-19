# FJMV-PI — Vigilancia con cámara + IA (Raspberry Pi → jmhome)

Sistema de vigilancia casero **plug-and-play**. Una Raspberry Pi 4B (Kali) lee
una cámara Dahua/IPC360 por RTSP, detecta rostros con IA, y cuando aparecen
**2+ personas** captura fotos y avisa por **WhatsApp** con un collage + link.
Todo se ve y administra desde la app **jmhome.fly.dev** (Admin → Cam). La Pi
reemplaza a la PC: enciende y trabaja sola, sin pantalla, todo por SSH/relé.

---

## Cómo está armado (3 piezas)

```
 Cámara Dahua RTSP            Raspberry Pi 4B (Kali, /opt/fjmv-pi)         jmhome.fly.dev (relé)            Tú
 ─────────────────           ───────────────────────────────────         ────────────────────            ──────
 H.265, digest auth   ──►   server/  (FastAPI :8090)                                                   App Admin → Cam
 IP por DHCP (.254/.47…)     • lee RTSP, detecta rostros (res10+Haar) ──►  blueprints/camara.py   ──►   • vivo (MJPEG, on-demand)
                             • on 2+ rostros: fotos + collage              (relé en memoria)            • galería de fotos
                             • empuja todo a jmhome (saliente)                                          • WhatsApp al grupo
                             agent.py (sniffing, poll saliente)     ──►    blueprints/sniffing.py  ──►   Admin → Sniffing (terminal)
                             hotspot.sh → WiFi JM__HOME_6G
```

- **La Pi nunca abre puertos entrantes.** Siempre conecta saliente a jmhome
  (igual patrón que los ESP de whofi). jmhome hace de **puente**: el admin ve
  la cámara y maneja la terminal sin túneles externos (no Cloudflare).

---

## Qué hace (funcionalidades)

- **Detección de rostros** (OpenCV res10 SSD + cascada de perfil, dedup IoU).
  Cuenta rostros; "evento" cuando hay **≥2**.
- **Vivo** por MJPEG, **a color**, optimizado (480px) y **on-demand**: arranca
  en STOP, se enciende con ▶ Play para no gastar datos.
- **Fotos** de evidencia (gallería) y **collage 2×2**, a color, solo de tomas
  con **≥2 rostros**. No se borran (volumen persistente en Fly).
- **WhatsApp** (Green-API): al detectar actividad manda el **collage + link** a
  la galería pública, y se **repite** cada intervalo configurable (1/5/10/30/60
  min) mientras siga la actividad.
- **Sniffing**: hotspot `JM__HOME_6G` + terminal remota en la app.

---

## Decisiones / configuración clave (no romper)

| Tema | Decisión | Por qué |
|---|---|---|
| **Destino WhatsApp** | Grupo **`120363425828954803`** (`@g.us`) | Concentrar las alertas en un grupo. Antes iba al personal 5545894519 (migrado automático). Los grupos **solo** van por Green-API. |
| **IP de la cámara** | Autodescubrimiento (no IP fija) | La cámara es DHCP; el bridge escanea el /24 con el digest RTSP y la reencuentra sola. Sirve para esta u otra cámara. |
| **jmhome workers** | gunicorn **1 worker** + 16 hilos | Los relés guardan estado en memoria; 2+ workers parten el estado → vivo intermitente y terminal pierde comandos. |
| **Color** | Vivo/fotos/collage a color | Es MJPEG: a color ~18-20 KB/frame vs ~13 KB gris ≈ igual rendimiento. La detección siempre fue color. |
| **Vivo** | On-demand (Play) | Ahorra datos: la Pi solo transmite si alguien pidió el vivo en los últimos 5 s. |
| **Servicios** | systemd `Restart=always` + enable | Arrancan solos en cada boot (camara, agente, hotspot). |

---

## Instalar (en la Pi, una vez)
```bash
sudo apt -y install git
git clone https://github.com/franszz5233/fjmv-pi.git /opt/fjmv-pi
cd /opt/fjmv-pi
sudo bash setup.sh            # cámara + agente + servicios systemd
sudo bash hotspot.sh          # hotspot JM__HOME_6G + sniffing (requiere antena WiFi USB)
```
La clave de la cámara y los tokens llegan por variables de entorno (no quedan en
GitHub). WiFi cliente: `JM__HOME` / pass `5545894519`. SSH: `kali` / `5545894519`.

## Actualizar la Pi
```bash
cd /opt/fjmv-pi && sudo git reset --hard origin/main \
  && sudo systemctl restart fjmv-camara fjmv-agent
```
(También desde la terminal de la app: Admin → Sniffing.)

## Mantenimiento / fallos comunes
Ver **[MANTENIMIENTO.md](MANTENIMIENTO.md)** (auto-recuperación de IP, checklist
"no veo la cámara", color vs rendimiento). En jmhome:
**`blueprints/CAMARA_SNIFFING.md`** (por qué 1 worker, vivo on-demand).

## Estructura
```
server/      bridge de cámara (ai.py detección+collage, cloud.py push, main.py FastAPI)
agent.py     agente de sniffing (poll a jmhome, ejecuta comandos)
setup.sh     instala todo + servicios systemd
hotspot.sh   hotspot JM__HOME_6G (hostapd+dnsmasq+NAT) en cada boot
sd/          cloud-init (NoCloud) para re-grabar la SD  · ISO en "FJMV/ISO Kali"
```

## Componentes en jmhome (repo aparte)
- `blueprints/camara.py` — relé de cámara (ingesta, vivo MJPEG, galería, WhatsApp al grupo).
- `blueprints/sniffing.py` — relé de terminal a la(s) Pi.
- `Dockerfile` — gunicorn **1 worker** (ver tabla arriba).
