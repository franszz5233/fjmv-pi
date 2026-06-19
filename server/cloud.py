"""Empuja a jmhome (nube) por conexión saliente:
  • snapshots en vivo (ligero: escala de grises, baja resolución)
  • fotos de evidencia (buena resolución) cada 2s mientras hay rostros
  • collage (hasta 4 fotos en 1) que dispara el WhatsApp con link a la galería

Todo en hilos daemon para no frenar el bucle de captura.
"""
from __future__ import annotations

import threading
import time

import requests


class CloudPusher:
    def __init__(self, base_url, token, cam_id="cam1", name="Cámara",
                 snap_interval=0.1):
        self.base = (base_url or "").rstrip("/")
        self.token = token or ""
        self.cam_id = cam_id
        self.name = name
        self.snap_interval = snap_interval
        self._last_snap = 0.0
        self._last_status = 0.0
        self.want_live = False    # ¿alguien está viendo el vivo? (lo dice la nube)
        self.interval = None      # intervalo entre avisos WhatsApp (seg), lo dice la nube
        self.enabled = bool(self.base and self.token)

    def _hdr(self, extra=None):
        h = {"X-Cam-Token": self.token}
        if extra:
            h.update(extra)
        return h

    # ---------------- vivo BAJO DEMANDA ----------------
    def tick(self, jpeg, people, active):
        """Sube el frame de vivo SOLO si alguien lo está viendo."""
        if not self.enabled:
            return
        now = time.time()
        if self.want_live and jpeg and now - self._last_snap >= self.snap_interval:
            self._last_snap = now
            threading.Thread(target=self._send_frame, args=(jpeg, people, active),
                             daemon=True).start()

    def heartbeat(self, people, active, cam_ok, mac="", ip="", ms=-1):
        """Latido (hilo del puente): viaja SIEMPRE con cam_ok + MAC + IP + latencia."""
        if not self.enabled:
            return
        try:
            r = requests.post(
                f"{self.base}/api/camara/ingest/snapshot",
                params={"cam": self.cam_id, "name": self.name, "people": people,
                        "together": 1 if active else 0, "status": 1,
                        "camok": 1 if cam_ok else 0, "mac": mac, "ip": ip, "ms": ms},
                headers=self._hdr(), timeout=5)
            j = r.json() or {}
            self.want_live = bool(j.get("live"))
            if j.get("intervalo"):
                self.interval = float(j["intervalo"])
        except Exception:
            pass

    def _send_frame(self, jpeg, people, active):
        try:
            requests.post(
                f"{self.base}/api/camara/ingest/snapshot",
                params={"cam": self.cam_id, "name": self.name, "people": people,
                        "together": 1 if active else 0},
                data=jpeg, headers=self._hdr({"Content-Type": "image/jpeg"}),
                timeout=5)
        except Exception:
            pass

    # ---------------- foto (galería) ----------------
    def push_photo(self, jpeg, hora, burst):
        if not self.enabled:
            return

        def _go():
            try:
                requests.post(
                    f"{self.base}/api/camara/ingest/foto",
                    data={"cam": self.cam_id, "hora": hora, "burst": burst},
                    files={"foto": ("foto.jpg", jpeg, "image/jpeg")},
                    headers=self._hdr(), timeout=15)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()

    # ---------------- collage (dispara WhatsApp) ----------------
    def push_collage(self, jpeg, hora, burst):
        if not self.enabled:
            return

        def _go():
            try:
                requests.post(
                    f"{self.base}/api/camara/ingest/collage",
                    data={"cam": self.cam_id, "hora": hora, "burst": burst},
                    files={"foto": ("collage.jpg", jpeg, "image/jpeg")},
                    headers=self._hdr(), timeout=20)
            except Exception as e:  # noqa: BLE001
                print("  [cloud] collage falló:", e)
        threading.Thread(target=_go, daemon=True).start()
