"""
Descubre cámaras en la WiFi local: escanea la subred buscando puertos típicos
de cámaras IP (RTSP 554, ONVIF 80/8899) y sugiere URLs RTSP comunes de IPC360 /
multi-lente para probar.
"""

from __future__ import annotations

import socket
import threading
from typing import List

CAM_PORTS = [554, 8899, 80, 8000]   # RTSP, ONVIF/propietario, http
# patrones RTSP comunes en cámaras chinas (IPC360/ICSEE/V380/Tuya/Hi3518…)
RTSP_PATTERNS = [
    "rtsp://{u}:{p}@{ip}:554/onvif{n}",
    "rtsp://{u}:{p}@{ip}:554/ch0{n}/0",
    "rtsp://{u}:{p}@{ip}:554/live/ch0{n}_0",
    "rtsp://{u}:{p}@{ip}:554/{n}/av0",
    "rtsp://{u}:{p}@{ip}:554/cam/realmonitor?channel={n}&subtype=0",
    "rtsp://{u}:{p}@{ip}:554/h264/ch{n}/main/av_stream",
    "rtsp://{u}:{p}@{ip}:554/stream{n}",
]


def _local_subnet() -> str:
    """Devuelve el prefijo /24 local, p.ej. '192.168.100.'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "192.168.1.1"
    finally:
        s.close()
    return ip.rsplit(".", 1)[0] + "."


def _check(ip: str, results: list, timeout=0.4):
    open_ports = []
    for port in CAM_PORTS:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                open_ports.append(port)
        except Exception:
            pass
    if open_ports:
        results.append((ip, open_ports))


def scan(subnet: str | None = None) -> List[dict]:
    """Escanea la /24 y devuelve hosts con puertos de cámara abiertos."""
    prefix = subnet or _local_subnet()
    results: list = []
    threads = []
    for i in range(1, 255):
        t = threading.Thread(target=_check, args=(f"{prefix}{i}", results))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    out = []
    for ip, ports in sorted(results):
        out.append({"ip": ip, "ports": ports, "likely_camera": 554 in ports or 8899 in ports})
    return out


def suggest_rtsp(ip: str, user: str = "admin", password: str = "", n_cams: int = 3) -> List[str]:
    """Lista de URLs RTSP candidatas para una cámara de n_cams canales."""
    urls = []
    for pat in RTSP_PATTERNS:
        for n in range(1, n_cams + 1):
            urls.append(pat.format(u=user, p=password, ip=ip, n=n))
    return urls
