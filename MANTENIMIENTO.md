# FJMV-PI · Mantenimiento y auto-recuperación

Bridge de cámara + agente de sniffing que corre en la Raspberry Pi (Kali).
Pensado para ser **plug-and-play**: se autorecupera solo de los fallos comunes.

## Arquitectura (resumen)
- `server/` — bridge de cámara (FastAPI :8090). Lee RTSP, detecta rostros,
  arma collage, y **empuja** todo a `jmhome.fly.dev` (relé). El admin ve la
  cámara desde la app, no se conecta directo a la Pi.
- `agent.py` — agente de sniffing. Hace *poll* saliente a jmhome y ejecuta
  comandos de la terminal Sniffing (la Pi nunca abre puertos entrantes).
- `hotspot.sh` — levanta el hotspot `JM__HOME_6G` (WiFi USB) en cada boot.
- `setup.sh` — instala todo y deja 2 servicios systemd que arrancan solos:
  `fjmv-camara` y `fjmv-agent`.

## Auto-recuperación que YA es automática (no hay que hacer nada)

### 1. La cámara cambia de IP (DHCP) — se reencuentra sola
La cámara es DHCP: hoy es `.254`, mañana `.47`, etc. El bridge **NO** depende
de una IP fija. En `server/ai.py`:
- Si no puede abrir el RTSP de la URL conocida → entra `discover_camera()`,
  que **escanea todo el /24** y prueba el *digest auth* RTSP con las credenciales.
  La primera que responde es la cámara → actualiza la URL y sigue.
- Sirve para **esta cámara o cualquier otra** que use las mismas credenciales.
- Si la cámara estaba conectada y cambia de IP, los `grab()` empiezan a fallar
  → el bucle reabre → falla → dispara el autodescubrimiento. Se recupera en segundos.
- Parámetros del escaneo afinados para la WiFi de la Pi: `timeout=1.5, max_workers=40`
  (80 hilos saturaban la WiFi y no encontraba nada).

Diagnóstico: `journalctl -u fjmv-camara -f` muestra `[IA] IP nueva -> X.X.X.X`.

### 2. Los servicios se caen — arrancan solos
`fjmv-camara` y `fjmv-agent` son systemd con `Restart=always`. Y están
`enable`-ados → arrancan en cada boot. El hotspot también (`hostapd`,
`dnsmasq`, `fjmv-nat` enabled).

### 3. Sin internet / sin señal
El bridge pinta un placeholder ("buscando cámara…", "sin senal") y reintenta
en bucle sin morir.

## Actualizar el código de la Pi
Desde la **terminal Sniffing** de la app (Admin → Sniffing), o por SSH:
```bash
cd /opt/fjmv-pi && sudo git fetch -q && sudo git reset --hard origin/main \
  && sudo systemctl restart fjmv-camara fjmv-agent
```
(El repo en la Pi vive en `/opt/fjmv-pi`.)

## Color vs rendimiento
El vivo va por **MJPEG** (JPEG por frame). A color, 480px, calidad 58 ≈ 18-20 KB
por frame: prácticamente igual que gris (~13 KB) — **no baja el rendimiento**.
La **detección siempre fue a color** (el modelo res10 recibe BGR). Por eso el
vivo, las fotos y el collage están a color. Si algún día hay que aligerar en
una red muy lenta: bajar `live_quality` o `live_width` en `server/ai.py`.

## Checklist rápido si "no veo la cámara"
1. ¿Servicio vivo?  `systemctl is-active fjmv-camara` → `active`.
2. ¿Conectó a la cámara?  `journalctl -u fjmv-camara -n 30` → sin "sin senal".
3. ¿Reporta a jmhome?  en la app la tarjeta de la cámara muestra IP/MAC frescas.
4. **El vivo es on-demand**: hay que presionar **▶ Play** en la tarjeta
   (arranca en STOP a propósito, para no gastar datos). Tarda 1-2 s.
5. Si la app muestra la cámara intermitente, revisar que jmhome corra con
   **1 worker** (ver nota en el proyecto jmhome) — es requisito del relé.
