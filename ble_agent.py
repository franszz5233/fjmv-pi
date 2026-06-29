#!/usr/bin/env python3
"""Agente Bluetooth (BLE) de la Raspberry Pi — usa jmhome como PUENTE (relé HTTP).

La Pi es la ANTENA Bluetooth. Sale a internet, hace 'poll' de comandos tipados
que el admin pidió en jmhome (Bluetooth), los ejecuta con su radio BLE (bleak)
y devuelve JSON. jmhome no necesita alcanzar a la Pi.

Comandos:
  scan  → BleakScanner.discover(seconds): lista dispositivos (mac, nombre, rssi,
          uuids/manufacturer) + identificación heurística (TTLock / Tuya).
  probe → conecta y enumera servicios+características (GATT) de una cerradura.
  test  → conecta, identifica protocolo, se suscribe a notificaciones, escucha
          unos segundos y (opcional) escribe un frame hex. Prueba de comunicación.
  raw   → escritura BLE de bajo nivel a una característica concreta (experimentar).

Config por variables de entorno (ver el .service):
  JMHOME    = https://jmhome.fly.dev
  PI_ID     = pi-fjmv
  PI_NAME   = Raspberry FJMV
  BLE_TOKEN = fjmv-ble-5233     (cae a SNIFF_TOKEN si no está)

Requiere: bleak (pip), BlueZ + bluetooth encendido (rfkill unblock bluetooth).
"""
import os
import socket
import asyncio
import subprocess
import time

import requests

try:
    from bleak import BleakScanner, BleakClient
    BLE_OK = True
    BLE_ERR = ''
except Exception as _e:  # noqa: BLE001
    BLE_OK = False
    BLE_ERR = str(_e)

BASE = os.environ.get("JMHOME", "https://jmhome.fly.dev").rstrip("/")
PI_ID = os.environ.get("PI_ID", "pi-fjmv")
PI_NAME = os.environ.get("PI_NAME", "Raspberry FJMV")
TOKEN = os.environ.get("BLE_TOKEN", os.environ.get("SNIFF_TOKEN", "fjmv-ble-5233"))
HDR = {"X-Ble-Token": TOKEN}

# ---- Firmas conocidas de cerraduras --------------------------------------
TTLOCK_SVC = "00001910-0000-1000-8000-00805f9b34fb"   # servicio 0x1910
TUYA_SVC = "0000fd50-0000-1000-8000-00805f9b34fb"      # servicio 0xFD50
TUYA_SVC_ALT = "0000a201-0000-1000-8000-00805f9b34fb"  # publicidad 0xA201


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def _adapters():
    """Lista los adaptadores BT (hciconfig/hcitool) — solo informativo."""
    try:
        out = subprocess.run("hciconfig 2>/dev/null | grep -oE '^hci[0-9]+'",
                             shell=True, capture_output=True, text=True, timeout=5)
        return [x for x in (out.stdout or "").split() if x]
    except Exception:
        return []


def _guess_kind(name, uuids, mfg_ids):
    """Heurística de qué cerradura es, por nombre/uuids/fabricante."""
    name = (name or "").upper()
    u = {str(x).lower() for x in (uuids or [])}
    if TTLOCK_SVC in u or name.startswith(("S2", "TT", "LOCK", "M2", "SN")):
        if "1910" in " ".join(u) or TTLOCK_SVC in u:
            return "ttlock"
    if TUYA_SVC in u or TUYA_SVC_ALT in u or "fd50" in " ".join(u) or "a201" in " ".join(u):
        return "tuya"
    # Tuya suele anunciar el company id 0x07D0 (2000) o nombres tipo "TY".
    if 0x07D0 in (mfg_ids or []) or name.startswith(("TY", "TUYA", "BLE")):
        return "tuya"
    if TTLOCK_SVC in u:
        return "ttlock"
    return "?"


def hello():
    try:
        requests.post(f"{BASE}/api/ble/hello", json={
            "pi": PI_ID, "name": PI_NAME, "ip": lan_ip(),
            "ble_ok": BLE_OK, "adapters": _adapters(),
            "info": {"host": socket.gethostname(), "ble_err": "" if BLE_OK else BLE_ERR[:120]},
        }, headers=HDR, timeout=8)
    except Exception:
        pass


# ============================ Ejecución de comandos BLE ============================
async def do_scan(seconds):
    if not BLE_OK:
        return {"error": f"bleak no disponible: {BLE_ERR[:140]}"}
    devices = []
    try:
        found = await BleakScanner.discover(timeout=float(seconds), return_adv=True)
    except Exception as e:  # noqa: BLE001
        return {"error": f"scan falló: {str(e)[:160]}"}
    for addr, (dev, adv) in found.items():
        uuids = list(getattr(adv, "service_uuids", []) or [])
        mfg = list((getattr(adv, "manufacturer_data", {}) or {}).keys())
        name = getattr(adv, "local_name", None) or getattr(dev, "name", None) or ""
        devices.append({
            "mac": addr,
            "name": name,
            "rssi": getattr(adv, "rssi", None),
            "uuids": uuids,
            "mfg": mfg,
            "kind": _guess_kind(name, uuids, mfg),
        })
    devices.sort(key=lambda d: (d["kind"] == "?", -(d["rssi"] or -999)))
    return {"devices": devices, "count": len(devices), "seconds": seconds}


def _svc_summary(client):
    """Resumen del GATT: servicios + características con sus propiedades."""
    services = []
    proto = "?"
    write_char = notify_char = None
    for svc in client.services:
        su = str(svc.uuid).lower()
        if su == TTLOCK_SVC:
            proto = "ttlock"
        elif su in (TUYA_SVC, TUYA_SVC_ALT):
            proto = "tuya"
        chars = []
        for ch in svc.characteristics:
            props = list(ch.properties)
            chars.append({"uuid": str(ch.uuid), "props": props})
            if ("write" in props or "write-without-response" in props) and not write_char:
                # Preferir una char del servicio de la cerradura si ya lo detectamos.
                write_char = str(ch.uuid)
            if "notify" in props and not notify_char:
                notify_char = str(ch.uuid)
        services.append({"uuid": str(svc.uuid), "chars": chars})
    return services, proto, write_char, notify_char


async def do_probe(mac):
    if not BLE_OK:
        return {"error": f"bleak no disponible: {BLE_ERR[:140]}"}
    try:
        async with BleakClient(mac, timeout=20.0) as client:
            connected = client.is_connected
            services, proto, wc, nc = _svc_summary(client)
            name = ""
            try:
                # Device Name estándar 0x2A00
                val = await client.read_gatt_char("00002a00-0000-1000-8000-00805f9b34fb")
                name = bytes(val).decode(errors="ignore")
            except Exception:
                pass
            return {
                "mac": mac, "connected": connected, "name": name,
                "protocol": proto, "services": services,
                "write_char": wc, "notify_char": nc,
            }
    except Exception as e:  # noqa: BLE001
        return {"error": f"no se pudo sondear: {str(e)[:180]}", "mac": mac}


async def do_test(mac, kind, listen, write_hex):
    if not BLE_OK:
        return {"error": f"bleak no disponible: {BLE_ERR[:140]}"}
    notes = []           # bytes recibidos por notificación (hex)
    log = []

    def _cb(_char, data):
        notes.append(bytes(data).hex())

    try:
        log.append(f"Conectando a {mac}…")
        async with BleakClient(mac, timeout=20.0) as client:
            log.append("Conectado. Enumerando GATT…")
            services, proto, wc, nc = _svc_summary(client)
            if kind and kind != "auto":
                proto = kind
            log.append(f"Protocolo detectado: {proto}")
            log.append(f"write_char={wc or '—'}  notify_char={nc or '—'}")
            # Suscribirse a notificaciones para capturar respuesta de la cerradura.
            if nc:
                try:
                    await client.start_notify(nc, _cb)
                    log.append(f"Suscrito a notificaciones {nc}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"No se pudo suscribir: {str(e)[:100]}")
            # Escritura opcional (frame hex provisto por el admin).
            if write_hex and wc:
                try:
                    payload = bytes.fromhex(write_hex.replace(" ", ""))
                    await client.write_gatt_char(wc, payload, response=False)
                    log.append(f"Escrito {len(payload)} bytes a {wc}: {payload.hex()}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"Error al escribir: {str(e)[:120]}")
            elif write_hex and not wc:
                log.append("No hay característica de escritura — no se envió el frame.")
            else:
                log.append("Modo pasivo (sin escribir). Escuchando notificaciones…")
            # Escuchar.
            await asyncio.sleep(float(listen))
            if nc:
                try:
                    await client.stop_notify(nc)
                except Exception:
                    pass
            ok = client.is_connected
            log.append(f"Notificaciones recibidas: {len(notes)}")
            return {
                "mac": mac, "connected": ok, "protocol": proto,
                "write_char": wc, "notify_char": nc,
                "notifications": notes, "log": log,
                "services": services,
                "comm_ok": bool(notes) or ok,
                "nota": ("Enlace BLE establecido. El DESBLOQUEO real requiere las "
                         "llaves de la nube (TTLock open API / Tuya local_key)."),
            }
    except Exception as e:  # noqa: BLE001
        log.append(f"Fallo: {str(e)[:180]}")
        return {"error": f"prueba falló: {str(e)[:180]}", "mac": mac, "log": log}


async def do_raw(mac, char, write_hex, notify_char, listen):
    if not BLE_OK:
        return {"error": f"bleak no disponible: {BLE_ERR[:140]}"}
    notes = []
    log = []

    def _cb(_c, data):
        notes.append(bytes(data).hex())

    try:
        async with BleakClient(mac, timeout=20.0) as client:
            log.append("Conectado.")
            if notify_char:
                try:
                    await client.start_notify(notify_char, _cb)
                    log.append(f"Suscrito a {notify_char}")
                except Exception as e:  # noqa: BLE001
                    log.append(f"No se pudo suscribir: {str(e)[:100]}")
            if write_hex:
                payload = bytes.fromhex(write_hex.replace(" ", ""))
                await client.write_gatt_char(char, payload, response=False)
                log.append(f"Escrito {payload.hex()} a {char}")
            await asyncio.sleep(float(listen))
            if notify_char:
                try:
                    await client.stop_notify(notify_char)
                except Exception:
                    pass
            return {"mac": mac, "connected": client.is_connected,
                    "notifications": notes, "log": log}
    except Exception as e:  # noqa: BLE001
        return {"error": f"raw falló: {str(e)[:180]}", "mac": mac, "log": log}


def run_cmd(c):
    """Despacha un comando tipado y devuelve un dict serializable."""
    t = c.get("type")
    try:
        if t == "scan":
            return asyncio.run(do_scan(int(c.get("seconds", 8))))
        if t == "probe":
            return asyncio.run(do_probe(c.get("mac", "")))
        if t == "test":
            return asyncio.run(do_test(c.get("mac", ""), c.get("kind", "auto"),
                                       int(c.get("listen", 5)), c.get("write_hex", "")))
        if t == "raw":
            return asyncio.run(do_raw(c.get("mac", ""), c.get("char", ""),
                                      c.get("write_hex", ""), c.get("notify_char", ""),
                                      int(c.get("listen", 4))))
        return {"error": f"tipo desconocido: {t}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"excepción: {str(e)[:180]}"}


def main():
    print(f"[ble_agent] PI_ID={PI_ID} -> {BASE}  BLE_OK={BLE_OK}")
    if not BLE_OK:
        print(f"[ble_agent] bleak no disponible: {BLE_ERR}")
    last_hello = 0.0
    while True:
        now = time.time()
        if now - last_hello >= 8:
            last_hello = now
            hello()
        try:
            r = requests.get(f"{BASE}/api/ble/poll",
                             params={"pi": PI_ID, "token": TOKEN, "ip": lan_ip()},
                             timeout=12).json()
            c = r.get("cmd")
            if c and c.get("type"):
                out = run_cmd(c)
                try:
                    requests.post(f"{BASE}/api/ble/result",
                                  json={"id": c["id"], "pi": PI_ID, "output": out},
                                  headers=HDR, timeout=20)
                except Exception:
                    pass
                continue   # vuelve a hacer poll de inmediato
        except Exception:
            pass
        time.sleep(1.2)


if __name__ == "__main__":
    main()
