#!/usr/bin/env python3
"""Agente Sniffing de la Raspberry Pi — usa jmhome como PUENTE (relé HTTP).

- Se registra en jmhome (hello) y manda latido.
- Hace 'poll' de comandos que el admin escribió en la app -> los ejecuta en la Pi
  -> devuelve la salida. (Igual idea que los ESP: comandos + reporte, pero por red.)
- Sale a internet por Ethernet; jmhome no necesita alcanzar a la Pi (la Pi llama).

Config por variables de entorno (ver el .service):
  JMHOME   = https://jmhome.fly.dev
  PI_ID    = pi-fjmv
  PI_NAME  = Raspberry Sniffer
  SNIFF_TOKEN = fjmv-sniff-5233
"""
import os
import socket
import subprocess
import time

import requests

BASE = os.environ.get("JMHOME", "https://jmhome.fly.dev").rstrip("/")
PI_ID = os.environ.get("PI_ID", "pi-fjmv")
PI_NAME = os.environ.get("PI_NAME", "Raspberry Sniffer")
TOKEN = os.environ.get("SNIFF_TOKEN", "fjmv-sniff-5233")
HDR = {"X-Sniff-Token": TOKEN}


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def hello():
    try:
        requests.post(f"{BASE}/api/sniffing/hello", json={
            "pi": PI_ID, "name": PI_NAME, "ip": lan_ip(),
            "info": {"host": socket.gethostname()},
        }, headers=HDR, timeout=8)
    except Exception:
        pass


def run(cmd):
    """Ejecuta un comando de shell y devuelve stdout+stderr (máx 120s)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=120, executable="/bin/bash")
        out = (r.stdout or "") + (r.stderr or "")
        return out.strip() or f"(exit {r.returncode}, sin salida)"
    except subprocess.TimeoutExpired:
        return "(comando excedió 120s — usa algo que termine, ej. 'timeout 10 ...')"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


def main():
    print(f"[agent] PI_ID={PI_ID} -> {BASE}")
    last_hello = 0.0
    while True:
        now = time.time()
        if now - last_hello >= 8:
            last_hello = now
            hello()
        try:
            r = requests.get(f"{BASE}/api/sniffing/poll",
                             params={"pi": PI_ID, "token": TOKEN, "ip": lan_ip()},
                             timeout=12).json()
            c = r.get("cmd")
            if c and c.get("cmd"):
                out = run(c["cmd"])
                try:
                    requests.post(f"{BASE}/api/sniffing/output",
                                  json={"id": c["id"], "output": out[:60000]},
                                  headers=HDR, timeout=15)
                except Exception:
                    pass
                continue   # vuelve a hacer poll de inmediato (terminal fluida)
        except Exception:
            pass
        time.sleep(1.2)


if __name__ == "__main__":
    main()
