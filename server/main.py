"""
Servidor web local para ver y controlar la cámara PTZ multi-lente (IPC360).

Uso:
    python -m server.main                  # usa config.json (o placeholders)
    python -m server.main --port 8090

Abre http://localhost:8090  (o por IP de red para verla desde el celular).

Configura tus cámaras en config.json (copia config.example.json). Si no sabes
la URL RTSP, primero descubre la cámara:
    python tools/find_camera.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from .camera import CameraStream, ScreenStream
from . import discover as disco

ROOT = os.path.dirname(os.path.dirname(__file__))
WEB_DIR = os.path.join(ROOT, "web")
CAPTURES_DIR = os.path.join(ROOT, "captures")

app = FastAPI(title="Cam PTZ Viewer")
STREAMS: dict[int, CameraStream] = {}
CFG: dict = {}
_PTZ = None
_PTZ_TRIED = False


def load_config() -> dict:
    for name in ("config.json", "config.example.json"):
        path = os.path.join(ROOT, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    # último recurso: 3 cámaras vacías (placeholder)
    return {"port": 8090, "cameras": [
        {"id": i, "name": f"Lente {i}", "url": ""} for i in (1, 2, 3)]}


def get_ptz():
    """Crea el controlador PTZ la primera vez que se usa (evita colgar el arranque)."""
    global _PTZ, _PTZ_TRIED
    if _PTZ_TRIED:
        return _PTZ
    _PTZ_TRIED = True
    p = CFG.get("ptz")
    if not p or not p.get("host"):
        return None
    try:
        from .ptz import PTZController
        _PTZ = PTZController(p["host"], int(p.get("port", 80)),
                             p.get("user", "admin"), p.get("pass", ""))
    except Exception:
        _PTZ = None
    return _PTZ


@app.get("/")
async def index():
    return FileResponse(os.path.join(WEB_DIR, "index.html"))


@app.get("/api/cameras")
async def cameras():
    return [{"id": c.id, "name": c.name, "connected": c.connected,
             "configured": bool(c.url)} for c in STREAMS.values()]


@app.get("/stream/{cam_id}")
async def stream(cam_id: int):
    cam = STREAMS.get(cam_id)
    if not cam:
        return JSONResponse({"error": "cámara no existe"}, status_code=404)

    async def gen():
        boundary = b"--frame"
        while True:
            f = cam.get()
            if f:
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(f)).encode() + b"\r\n\r\n" + f + b"\r\n")
            await asyncio.sleep(0.05)   # ~20 fps máx

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.post("/api/ptz/{action}")
async def ptz(action: str):
    from .ptz import ACTIONS
    ctrl = get_ptz()
    if ctrl is None or not ctrl.ok:
        return {"ok": False, "error": "PTZ no disponible (¿config ptz? ¿ONVIF?)"}
    if action == "stop":
        return {"ok": ctrl.stop()}
    if action not in ACTIONS:
        return {"ok": False, "error": f"acción '{action}' desconocida"}
    return {"ok": ctrl.move(*ACTIONS[action])}


@app.get("/api/events")
async def events():
    """Eventos de IA (2 personas juntas + género + captura) de todas las cámaras."""
    out = []
    for c in STREAMS.values():
        if hasattr(c, "snapshot_events"):
            out.extend(c.snapshot_events())
    out.sort(key=lambda e: e["id"], reverse=True)
    live = {}
    for c in STREAMS.values():
        if hasattr(c, "people"):
            live = {"people": c.people, "together": c.together}
            break
    return {"live": live, "events": out[:40]}


@app.get("/api/discover")
async def discover():
    hosts = await asyncio.get_running_loop().run_in_executor(None, disco.scan)
    out = []
    for h in hosts:
        item = dict(h)
        if h["likely_camera"]:
            item["rtsp_candidates"] = disco.suggest_rtsp(h["ip"])
        out.append(item)
    return {"hosts": out}


os.makedirs(CAPTURES_DIR, exist_ok=True)
app.mount("/captures", StaticFiles(directory=CAPTURES_DIR), name="captures")
app.mount("/", StaticFiles(directory=WEB_DIR), name="static")


def main():
    global CFG
    CFG = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=CFG.get("port", 8090))
    ap.add_argument("--host", default="0.0.0.0")
    args = ap.parse_args()

    ai_cfg = CFG.get("ai", {})
    for c in CFG.get("cameras", []):
        name = c.get("name", f"Cam {c['id']}")
        use_ai = c.get("ai", ai_cfg.get("enabled", False))
        if c.get("screen"):                       # modo captura de pantalla
            s = ScreenStream(c["id"], name, c["screen"])
        elif use_ai:                              # modo IA (esqueleto + eventos)
            from .ai import AIStream
            s = AIStream(c["id"], name, c.get("url") or "",
                         proc_width=ai_cfg.get("proc_width", 720),
                         num_poses=ai_cfg.get("num_poses", 4),
                         gender=ai_cfg.get("gender", True),
                         cloud=CFG.get("cloud"))
        else:                                     # modo RTSP simple
            s = CameraStream(c["id"], name, c.get("url") or "")
        s.start()
        STREAMS[c["id"]] = s

    print(f"\n  Cam PTZ Viewer -> http://{args.host}:{args.port}")
    print(f"  Cámaras: {len(STREAMS)} · PTZ: {'sí' if CFG.get('ptz') else 'no configurado'}\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
