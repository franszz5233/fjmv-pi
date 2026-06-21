"""
Pipeline de visión que corre EN LA PC (no en la nube):

  RTSP  ->  MediaPipe PoseLandmarker (multi-persona)  ->  esqueleto dibujado
                       |
                       +-> cajas por persona -> ¿2 personas juntas?
                                                     |
                                                     +-> género (Caffe) + captura + evento

Sirve el frame ya anotado como JPEG (igual que CameraStream) y mantiene una
lista de eventos + capturas que la web muestra en el panel izquierdo.

Degrada con gracia: si falta un modelo, sigue funcionando sin esa parte.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import os
import re
import socket
import threading
import time
from collections import deque

import cv2
import numpy as np

from .camera import CameraStream


def _rtsp_auth_ok(ip, user, pw, path, port=554, timeout=3):
    """¿La IP responde RTSP 200 con estas credenciales? (verifica que es NUESTRA cámara)."""
    try:
        url = f"rtsp://{ip}:{port}{path}"
        s = socket.create_connection((ip, port), timeout=timeout)
        s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode())
        d = s.recv(2048).decode(errors="ignore")
        m = re.search(r'realm="([^"]+)".*?nonce="([^"]+)"', d, re.S)
        if not m:
            s.close()
            return "200" in d
        realm, nonce = m.group(1), m.group(2)
        h = lambda x: hashlib.md5(x.encode()).hexdigest()  # noqa: E731
        resp = h(f"{h(f'{user}:{realm}:{pw}')}:{nonce}:{h(f'DESCRIBE:{url}')}")
        auth = (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", '
                f'uri="{url}", response="{resp}"')
        s.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAuthorization: {auth}\r\n"
                  f"Accept: application/sdp\r\n\r\n".encode())
        r = s.recv(2048).decode(errors="ignore")
        s.close()
        return "200" in r.split("\r\n")[0]
    except Exception:
        return False


def discover_camera(template_url):
    """Escanea la /24 y devuelve la URL RTSP de NUESTRA cámara (misma clave) en su IP actual."""
    m = re.match(r"rtsp://([^:]+):([^@]+)@([\d.]+):(\d+)(/.*)", template_url)
    if not m:
        return None
    import urllib.parse
    user, pw_enc, oldip, port, path = m.groups()
    pw = urllib.parse.unquote(pw_enc)   # clave real para el digest ($ viene como %24)
    base = ".".join(oldip.split(".")[:3])

    def open554(ip):
        try:
            s = socket.create_connection((ip, 554), timeout=1.5)
            s.close()
            return ip
        except Exception:
            return None

    cands = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=40) as ex:
        for r in ex.map(open554, [f"{base}.{i}" for i in range(1, 255)]):
            if r:
                cands.append(r)
    # priorizar la IP anterior si reaparece
    cands.sort(key=lambda x: x != oldip)
    for ip in cands:
        if _rtsp_auth_ok(ip, user, pw, path, int(port)):
            print(f"  [IA] cámara encontrada en {ip}")
            return f"rtsp://{user}:{pw_enc}@{ip}:{port}{path}"   # encoded para cv2/ffmpeg
    return None

MODELS = os.path.join(os.path.dirname(__file__), "models")
CAPTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "captures")
os.makedirs(CAPTURES, exist_ok=True)

# topología de los 33 puntos de MediaPipe Pose (mismas conexiones que solutions.pose)
POSE_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10), (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21),
    (17, 19), (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
    (27, 29), (28, 30), (29, 31), (30, 32), (27, 31), (28, 32),
]

GENDER_MEAN = (78.4263377603, 87.7689143744, 114.895847746)
GENDER_LABELS = ("Hombre", "Mujer")


class AIStream(CameraStream):
    """Lee RTSP, anota con IA y publica frame + eventos."""

    def __init__(self, cam_id, name, url, *, proc_width=640, num_poses=4,
                 gender=False, quality=58, together_cooldown=8.0,
                 cloud=None, clip_secs=30, notify_interval=30.0, max_rep=5,
                 min_faces=2, face_conf=0.4, process_fps=7,
                 cap_interval=2.0, window_secs=1.5, cooldown=60.0, win_sample=0.35):
        super().__init__(cam_id, name, url, quality=quality, max_w=proc_width)
        self.proc_width = proc_width      # resolución del análisis/vivo
        self.process_fps = process_fps    # procesa ~N fps (el resto se descarta -> sin lag)
        self.min_faces = min_faces        # avisa cuando hay >= min_faces rostros
        self.face_conf = face_conf
        self.cap_interval = cap_interval  # foto a la galería cada Ns
        self.win_sample = win_sample      # muestreo en memoria para el collage (rápido)
        self.window_secs = window_secs    # actividad mínima para el 1er collage
        self.cooldown = cooldown          # espera entre avisos de WhatsApp (configurable)

        self.events: deque = deque(maxlen=40)
        self.people = 0
        self.together = False             # = actividad (>= min_faces rostros)
        self._ev_lock = threading.Lock()
        self._face_net = None
        self._profile = None

        # detección desacoplada del vivo (anti-lag)
        self.live_fps = 12                # vivo fluido
        self.live_width = 480             # vivo más chico -> sube más rápido
        self.live_quality = 58            # color a 480px ~18-20KB/frame: ligero para WiFi
        self.detect_dt = 0.2              # detección ~5 fps en su propio hilo
        self._latest = None
        self._latest_lock = threading.Lock()
        self._last_faces = []
        self._det_started = False
        self._hb_started = False
        self._cam_mac = ""

        # ---- nube ----
        self.cloud = None
        if cloud and cloud.get("url"):
            try:
                from .cloud import CloudPusher
                self.cloud = CloudPusher(
                    cloud["url"], cloud.get("token", ""),
                    cloud.get("camera_id", "cam1"), cloud.get("name", name))
                print(f"  [IA] cloud cam_id={self.cloud.cam_id!r}  (config={cloud.get('camera_id')!r})")
            except Exception as e:  # noqa: BLE001
                print("  [IA] nube no disponible:", e)

        # ---- estado de actividad / capturas / notificación por ventanas ----
        self._active = False
        self._burst = None
        self._last_face_ts = 0.0
        self._cap_last = 0.0
        self._win_last = 0.0
        self._win: list = []              # frames color muestreados (rolling) para el collage
        self._activity_start = 0.0
        self._wa_last = 0.0

        self._apply_lite()

    # --------------------------------------------------- modo ligero (Zero 2 W)
    def _ram_mb(self):
        try:
            with open("/proc/meminfo") as f:
                for ln in f:
                    if ln.startswith("MemTotal"):
                        return int(ln.split()[1]) // 1024
        except Exception:
            pass
        return 9999

    def _apply_lite(self):
        """Auto en equipos con poca RAM (Raspberry Pi Zero 2 W = 512MB):
        baja resolución/fps y usa el SUBSTREAM de la cámara (subtype=1 ~ H.264,
        mucho menos CPU para decodificar). Forzar con FJMV_LITE=1/0."""
        env = os.environ.get("FJMV_LITE", "")
        lite = (env == "1") or (env != "0" and self._ram_mb() < 700)
        if not lite:
            return
        self.proc_width = min(self.proc_width, 416)
        self.live_fps = 6
        self.live_width = 384
        self.live_quality = 55
        self.detect_dt = 0.5              # detección ~2 fps (suficiente para contar rostros)
        # cámara: pasar al substream (más liviano). discover_camera conserva el subtype.
        if self.url and "subtype=0" in self.url:
            self.url = self.url.replace("subtype=0", "subtype=1")
        print(f"  [IA] MODO LIGERO ON (RAM~{self._ram_mb()}MB): proc={self.proc_width} "
              f"live={self.live_width}@{self.live_fps}fps substream")

    # ----------------------------------------------------------- modelos
    def _init_models(self):
        # Detector de ROSTROS (OpenCV DNN, res10 SSD)
        proto = os.path.join(MODELS, "face_deploy.prototxt")
        model = os.path.join(MODELS, "res10_face.caffemodel")
        try:
            self._face_net = cv2.dnn.readNetFromCaffe(proto, model)
            print("  [IA] detector de rostros OK")
        except Exception as e:  # noqa: BLE001
            print("  [IA] detector de rostros NO cargó:", e)
        # cascada de PERFIL (rostros de lado) — viene con OpenCV, sin descargas
        try:
            self._profile = cv2.CascadeClassifier(
                cv2.data.haarcascades + "haarcascade_profileface.xml")
            if self._profile.empty():
                self._profile = None
        except Exception:
            self._profile = None

    # ----------------------------------------------------------- helpers
    def _collage(self, frames):
        """Collage 2x2 con las 4 fotos MÁS RECIENTES (color). Si hay menos de 4,
        repite la última para NO dejar celdas en negro."""
        sel = list(frames)[-4:]
        if not sel:
            return None
        while len(sel) < 4:
            sel.append(sel[-1])
        cw, ch = 480, 270
        c = [cv2.resize(f, (cw, ch)) for f in sel]
        grid = np.vstack([np.hstack([c[0], c[1]]), np.hstack([c[2], c[3]])])
        ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return buf.tobytes()

    @staticmethod
    def _iou(a, b):
        ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
        ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
        iw = max(0, ix2 - ix1); ih = max(0, iy2 - iy1)
        inter = iw * ih
        if inter == 0:
            return 0.0
        area_a = (a[2] - a[0]) * (a[3] - a[1])
        area_b = (b[2] - b[0]) * (b[3] - b[1])
        return inter / float(area_a + area_b - inter)

    def _dedup(self, boxes):
        """Quita solapados (mismo rostro detectado 2 veces) -> conteo correcto."""
        out = []
        for b in sorted(boxes, key=lambda z: (z[2] - z[0]) * (z[3] - z[1]), reverse=True):
            if all(self._iou(b, o) < 0.35 for o in out):
                out.append(b)
        return out

    def _detect_faces(self, bgr):
        """Rostros (x1,y1,x2,y2): frontales (DNN) + de perfil (cascada), sin duplicados."""
        if self._face_net is None:
            return []
        h, w = bgr.shape[:2]
        boxes = []
        # frontales (DNN res10, blob 416)
        blob = cv2.dnn.blobFromImage(cv2.resize(bgr, (416, 416)), 1.0,
                                     (416, 416), (104.0, 177.0, 123.0))
        self._face_net.setInput(blob)
        det = self._face_net.forward()
        for i in range(det.shape[2]):
            if float(det[0, 0, i, 2]) < self.face_conf:
                continue
            x1 = int(det[0, 0, i, 3] * w); y1 = int(det[0, 0, i, 4] * h)
            x2 = int(det[0, 0, i, 5] * w); y2 = int(det[0, 0, i, 6] * h)
            x1, y1 = max(0, x1), max(0, y1); x2, y2 = min(w, x2), min(h, y2)
            if x2 > x1 and y2 > y1:
                boxes.append((x1, y1, x2, y2))
        # de PERFIL (ambos lados) con cascada Haar
        if self._profile is not None:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            for (x, y, ww, hh) in self._profile.detectMultiScale(gray, 1.2, 6, minSize=(44, 44)):
                boxes.append((x, y, x + ww, y + hh))
            flip = cv2.flip(gray, 1)
            for (x, y, ww, hh) in self._profile.detectMultiScale(flip, 1.2, 6, minSize=(44, 44)):
                fx = w - (x + ww)
                boxes.append((fx, y, fx + ww, y + hh))
        return self._dedup(boxes)

    def _save_event(self, jpg_bytes):
        """Guarda la miniatura localmente (visor :8090)."""
        ts = time.strftime("%Y%m%d_%H%M%S_") + f"{int(time.time() * 1000) % 1000:03d}"
        name = f"cap_{ts}.jpg"
        with open(os.path.join(CAPTURES, name), "wb") as f:
            f.write(jpg_bytes)
        with self._ev_lock:
            self.events.appendleft({
                "id": name, "time": time.strftime("%H:%M:%S"),
                "people": self.people, "genders": [], "img": f"/captures/{name}",
            })

    def _photo_async(self, gfull, hora, burst):
        """Codifica + guarda en disco + sube la foto, fuera del bucle principal."""
        try:
            jpg = cv2.imencode(".jpg", gfull, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
            self._save_event(jpg)
            if self.cloud:
                self.cloud.push_photo(jpg, hora, burst)
        except Exception as e:  # noqa: BLE001
            print("  [IA] foto async:", e)

    def _collage_async(self, frames, hora, burst):
        """Sube las 4 fotos individuales (para el link /g/<burst>) + el collage (WhatsApp)."""
        try:
            sel = list(frames)[-4:]
            if not self.cloud:
                return
            for fr in sel:                 # 4 fotos individuales -> el link muestra 4
                jpg = cv2.imencode(".jpg", fr, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
                self._save_event(jpg)
                self.cloud.push_photo(jpg, hora, burst)
            self.cloud.push_collage(self._collage(sel), hora, burst)
        except Exception as e:  # noqa: BLE001
            print("  [IA] collage async:", e)

    def snapshot_events(self):
        with self._ev_lock:
            return list(self.events)

    # ----------------------------------------------------------- loop
    def _cam_net(self):
        """Devuelve (ip, mac, ms_latencia) de la cámara. ms = proxy de calidad de enlace."""
        m = re.search(r"@([\d.]+):(\d+)", self.url)
        ip = m.group(1) if m else ""
        port = int(m.group(2)) if m else 554
        if ip and not self._cam_mac:           # MAC vía ARP (una vez)
            try:
                import subprocess
                out = subprocess.run(["arp", "-a", ip], capture_output=True,
                                     text=True, timeout=3).stdout
                mm = re.search(r"([0-9a-fA-F]{2}[-:]){5}[0-9a-fA-F]{2}", out)
                if mm:
                    self._cam_mac = mm.group(0).replace("-", ":").lower()
            except Exception:
                pass
        ms = -1                                 # latencia TCP a 554 (calidad de enlace)
        if ip:
            try:
                t = time.time()
                s = socket.create_connection((ip, port), timeout=1.5)
                s.close()
                ms = int((time.time() - t) * 1000)
            except Exception:
                ms = -1
        return ip, self._cam_mac, ms

    def _heartbeat_loop(self):
        """Latido independiente: aunque la cámara se caiga, sigue avisando a la nube
        (cam_ok, mac, ip, calidad de enlace) -> la app pinta estado/IP/señal."""
        last_net = 0.0
        ip = mac = ""
        ms = -1
        while self.running:
            now = time.time()
            if now - last_net >= 5:
                last_net = now
                ip, mac, ms = self._cam_net()
            if self.cloud:
                try:
                    self.cloud.heartbeat(self.people, self.together, self.connected,
                                         mac, ip, ms)
                except Exception:
                    pass
            time.sleep(1.0)

    def _loop(self):
        self._init_models()
        if self.cloud and not self._hb_started:
            self._hb_started = True
            threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        import os as _os
        _os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

        while self.running:
            if not self.url:
                self._set(self._placeholder("sin URL — configura config.json"))
                time.sleep(1.0); continue
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                cap.release()
                self.connected = False
                # autodescubrimiento: la cámara pudo cambiar de IP (DHCP)
                self._set(self._placeholder("buscando cámara en la red…"))
                nu = discover_camera(self.url)
                if nu and nu != self.url:
                    print(f"  [IA] IP nueva -> {nu.split('@')[-1].split('/')[0]}")
                    self.url = nu
                    continue
                self._set(self._placeholder("sin senal / no conecta"))
                time.sleep(3.0); continue

            self.connected = True
            if not self._det_started:        # detección en su propio hilo (1 sola vez)
                self._det_started = True
                threading.Thread(target=self._detect_loop, daemon=True).start()
            fails = 0
            last_live = 0.0
            live_dt = 1.0 / max(1, self.live_fps)
            while self.running:
                if not cap.grab():           # drena el buffer (barato)
                    fails += 1
                    if fails > 30:
                        break
                    time.sleep(0.02); continue
                fails = 0
                now = time.time()
                if now - last_live < live_dt:
                    continue                 # vivo a live_fps -> fluido
                last_live = now
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue
                with self._latest_lock:
                    self._latest = frame     # comparte el último frame con la detección
                try:
                    self._render_live(frame)
                except Exception as e:  # noqa: BLE001
                    print("  [IA] error live:", type(e).__name__, e)
            cap.release(); self.connected = False
            if self.running:
                self._set(self._placeholder("reconectando…"))
                time.sleep(1.0)

    def _small(self, full):
        H, W = full.shape[:2]
        if W > self.proc_width:
            return cv2.resize(full, (self.proc_width, int(H * self.proc_width / W)))
        return full

    # ---- HILO DE DETECCIÓN (no bloquea el vivo) ----
    def _detect_loop(self):
        while self.running:
            t0 = time.time()
            with self._latest_lock:
                frame = self._latest
            if frame is None or not self.connected:
                time.sleep(0.05); continue
            try:
                self._detect_and_act(frame)
            except Exception as e:  # noqa: BLE001
                print("  [IA] error detect:", type(e).__name__, e)
            dt = time.time() - t0
            if dt < self.detect_dt:
                time.sleep(self.detect_dt - dt)

    def _detect_and_act(self, full):
        small = self._small(full)
        faces = self._detect_faces(small)
        n = len(faces)
        self._last_faces = faces            # cacheado -> el vivo lo dibuja
        self.people = n
        now = time.time()
        active = n >= self.min_faces
        self.together = active

        if active:
            self._last_face_ts = now
            if not self._active:
                self._active = True
                self._burst = time.strftime("%Y%m%d_%H%M%S")
                self._activity_start = now
                self._cap_last = 0.0
                self._win_last = 0.0
                self._win = []
                self._wa_last = 0.0
            # muestreo en memoria para el collage (4 fotos COLOR, todas con >=2 rostros)
            if now - self._win_last >= self.win_sample:
                self._win_last = now
                self._win.append(full.copy())
                self._win = self._win[-6:]
            eff_cd = self.cooldown
            if self.cloud and getattr(self.cloud, "interval", None):
                eff_cd = self.cloud.interval
            ready = (now - self._activity_start) >= self.window_secs
            due = (self._wa_last == 0 and ready) or \
                  (self._wa_last > 0 and now - self._wa_last >= eff_cd)
            if due and len(self._win) and self.cloud:
                self._wa_last = now
                threading.Thread(target=self._collage_async,
                                 args=(list(self._win), time.strftime("%H:%M:%S"), self._burst),
                                 daemon=True).start()
        else:
            if self._active and (now - self._last_face_ts) > 3.0:
                self._active = False
                self._burst = None
                self._win = []

    # ---- HILO PRINCIPAL: vivo fluido (COLOR + cajas en caché) ----
    def _render_live(self, full):
        small = self._small(full)
        VERDE = (20, 255, 57)   # verde fosforescente
        live = small.copy()     # color real; .copy() -> no dibujar sobre el frame de detección
        for (x1, y1, x2, y2) in self._last_faces:
            cv2.rectangle(live, (x1, y1), (x2, y2), VERDE, 2)
        cv2.putText(live, f"Rostros: {self.people}", (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, VERDE, 2, cv2.LINE_AA)
        # vivo LIGERO: más chico + calidad media -> sube rápido (más fluido en remoto)
        lw = self.live_width
        if live.shape[1] > lw:
            live = cv2.resize(live, (lw, int(live.shape[0] * lw / live.shape[1])))
        jpeg = cv2.imencode(".jpg", live, [cv2.IMWRITE_JPEG_QUALITY, self.live_quality])[1].tobytes()
        self._set(jpeg)
        if self.cloud:
            self.cloud.tick(jpeg, self.people, self.together)
