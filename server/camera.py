"""
Lectura de cámara RTSP en un hilo de fondo -> último frame JPEG.

OpenCV trae ffmpeg incluido, así que abre RTSP sin binarios externos.
Si la cámara no conecta, entrega un frame "sin señal" para que la web
siga funcionando.
"""

from __future__ import annotations

import threading
import time

import cv2
import numpy as np


class CameraStream:
    def __init__(self, cam_id: int, name: str, url: str | None,
                 quality: int = 70, max_w: int = 960):
        self.id = cam_id
        self.name = name
        self.url = url
        self.quality = quality
        self.max_w = max_w
        self.connected = False
        self.running = False
        self._frame: bytes | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- API
    def start(self):
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.running = False

    def get(self) -> bytes | None:
        with self._lock:
            return self._frame

    # ------------------------------------------------------------ interno
    def _set(self, data: bytes):
        with self._lock:
            self._frame = data

    def _placeholder(self, text: str) -> bytes:
        img = np.full((360, 640, 3), (14, 16, 24), np.uint8)
        cv2.putText(img, f"#{self.id}  {self.name}", (20, 44),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 200, 120), 2)
        cv2.putText(img, text, (20, 200),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.62, (90, 110, 230), 2)
        ok, buf = cv2.imencode(".jpg", img)
        return buf.tobytes()

    def _encode(self, frame) -> bytes | None:
        h, w = frame.shape[:2]
        if w > self.max_w:
            frame = cv2.resize(frame, (self.max_w, int(h * self.max_w / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        return buf.tobytes() if ok else None

    def _loop(self):
        while self.running:
            if not self.url:
                self._set(self._placeholder("sin URL — configura config.json"))
                time.sleep(1.0)
                continue

            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            if not cap.isOpened():
                self.connected = False
                self._set(self._placeholder("sin senal / no conecta"))
                cap.release()
                time.sleep(2.0)
                continue

            self.connected = True
            fails = 0
            while self.running:
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 30:
                        break
                    time.sleep(0.02)
                    continue
                fails = 0
                data = self._encode(frame)
                if data:
                    self._set(data)
            cap.release()
            self.connected = False
            if self.running:
                self._set(self._placeholder("reconectando…"))
                time.sleep(1.0)


class ScreenStream(CameraStream):
    """
    Captura una región de la pantalla y la sirve como si fuera una cámara.

    Sirve para "puentear" el cliente oficial (IPC360 desktop o la app en un
    emulador): abres ahí la cámara, y esta clase captura esa zona y la entrega
    en nuestro visor local. region = {"monitor": N}  o  {"x","y","w","h"}.
    """

    def __init__(self, cam_id, name, region: dict, quality=72, fps=15):
        super().__init__(cam_id, name, url="capture", quality=quality)
        self.region = region or {"monitor": 1}
        self.fps = fps

    def _loop(self):
        import mss
        dt = 1.0 / self.fps
        try:
            with mss.mss() as sct:
                if "monitor" in self.region:
                    idx = int(self.region["monitor"])
                    mon = sct.monitors[idx] if idx < len(sct.monitors) else sct.monitors[1]
                else:
                    mon = {"left": self.region.get("x", 0), "top": self.region.get("y", 0),
                           "width": self.region.get("w", 960), "height": self.region.get("h", 540)}
                self.connected = True
                while self.running:
                    raw = sct.grab(mon)
                    frame = np.frombuffer(raw.rgb, np.uint8).reshape(raw.height, raw.width, 3)
                    frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    data = self._encode(frame)
                    if data:
                        self._set(data)
                    time.sleep(dt)
        except Exception as e:  # noqa: BLE001
            self.connected = False
            self._set(self._placeholder(f"captura falló: {type(e).__name__}"))
