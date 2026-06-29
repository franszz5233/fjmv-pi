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
import sys
import json
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
def _sh(cmd, timeout=8):
    """Corre un comando de shell y devuelve stdout+stderr (para diagnóstico)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, executable="/bin/bash")
        return ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return f"(error: {e})"


async def do_scan(seconds, active=True):
    """Escaneo robusto vía callback de detección (capta TODO anuncio, incluso
    no-conectables, y funciona en cualquier versión de bleak). Reintenta encender
    la radio si hace falta."""
    if not BLE_OK:
        return {"error": f"bleak no disponible: {BLE_ERR[:140]}",
                "hint": "Corre instalar-ble.sh en la Pi (pip install bleak)."}
    # Asegura radio encendida antes de escanear.
    _sh("rfkill unblock bluetooth 2>/dev/null; hciconfig hci0 up 2>/dev/null", timeout=5)
    seen = {}

    def _cb(dev, adv):
        try:
            seen[dev.address] = (dev, adv)
        except Exception:
            pass

    try:
        kw = {"detection_callback": _cb}
        try:
            scanner = BleakScanner(scanning_mode=("active" if active else "passive"), **kw)
        except TypeError:
            scanner = BleakScanner(**kw)   # versiones viejas sin scanning_mode
        await scanner.start()
        await asyncio.sleep(float(seconds))
        await scanner.stop()
    except Exception as e:  # noqa: BLE001
        # Último recurso: API clásica discover().
        try:
            found = await BleakScanner.discover(timeout=float(seconds))
            for dev in found:
                seen[dev.address] = (dev, None)
        except Exception as e2:  # noqa: BLE001
            return {"error": f"scan falló: {str(e)[:120]} / {str(e2)[:120]}",
                    "diag": do_diag(), "devices": []}

    devices = []
    for addr, (dev, adv) in seen.items():
        uuids = list(getattr(adv, "service_uuids", []) or []) if adv else []
        mfg = list((getattr(adv, "manufacturer_data", {}) or {}).keys()) if adv else []
        name = (getattr(adv, "local_name", None) if adv else None) or getattr(dev, "name", None) or ""
        rssi = getattr(adv, "rssi", None) if adv else getattr(dev, "rssi", None)
        devices.append({
            "mac": addr, "name": name, "rssi": rssi,
            "uuids": uuids, "mfg": mfg,
            "kind": _guess_kind(name, uuids, mfg),
        })
    devices.sort(key=lambda d: (d["kind"] == "?", -(d["rssi"] or -999)))
    out = {"devices": devices, "count": len(devices), "seconds": seconds}
    if not devices:
        # Nada encontrado → adjunta diagnóstico para entender por qué.
        out["diag"] = do_diag()
        out["hint"] = ("0 dispositivos. Si la radio está OK, ACTIVA la chapa "
                       "(tócala/pulsa el teclado) durante el escaneo: muchas "
                       "cerraduras solo anuncian BLE al despertar.")
    return out


def do_diag():
    """Diagnóstico del subsistema Bluetooth de la Pi (sin depender de bleak)."""
    d = {
        "bleak_ok": BLE_OK,
        "bleak_err": "" if BLE_OK else BLE_ERR[:200],
        "rfkill": _sh("rfkill list bluetooth 2>/dev/null || rfkill list 2>/dev/null"),
        "hciconfig": _sh("hciconfig -a 2>/dev/null"),
        "adapters": _sh("ls /sys/class/bluetooth 2>/dev/null"),
        "bt_service": _sh("systemctl is-active bluetooth 2>/dev/null"),
        "btmgmt": _sh("bluetoothctl show 2>/dev/null | head -20"),
        "dmesg_bt": _sh("dmesg 2>/dev/null | grep -iE 'blue|hci|brcm|firmware' | tail -12"),
    }
    try:
        import bleak as _b
        d["bleak_version"] = getattr(_b, "__version__", "?")
    except Exception:
        d["bleak_version"] = "(no instalado)"
    return d


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
        if t == "diag":
            return do_diag()
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


def scan_bluetoothctl(seconds):
    """Escaneo de respaldo SIN bleak, usando bluetoothctl (parte de BlueZ).
    Devuelve el mismo formato que do_scan. Útil si bleak no está instalado."""
    _sh("rfkill unblock bluetooth 2>/dev/null; hciconfig hci0 up 2>/dev/null; "
        "systemctl start bluetooth 2>/dev/null", timeout=6)
    # Enciende escaneo, espera, apaga, lista dispositivos vistos.
    _sh(f"bluetoothctl --timeout {int(seconds)} scan on", timeout=int(seconds) + 6)
    listing = _sh("bluetoothctl devices", timeout=8)
    devices = []
    for line in (listing or "").splitlines():
        line = line.strip()
        if not line.startswith("Device "):
            continue
        parts = line.split(" ", 2)
        mac = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        if not mac:
            continue
        devices.append({"mac": mac, "name": name, "rssi": None,
                        "uuids": [], "mfg": [], "kind": _guess_kind(name, [], [])})
    out = {"devices": devices, "count": len(devices), "seconds": seconds,
           "via": "bluetoothctl"}
    if not devices:
        out["diag"] = do_diag()
        out["hint"] = ("0 dispositivos (bluetoothctl). Activa la chapa durante el "
                       "escaneo; muchas cerraduras solo anuncian BLE al despertar.")
    return out


def cli_main(argv):
    """Modo CLI: imprime el resultado como 'BLEJSON:<json>' para que jmhome lo
    lea a través del relé de Sniffing. Uso:
       python3 ble_agent.py scan [segundos]
       python3 ble_agent.py diag
       python3 ble_agent.py probe <mac>
       python3 ble_agent.py test  <mac> [auto|ttlock|tuya] [listen] [write_hex]
       python3 ble_agent.py raw   <mac> <char> <write_hex> [notify_char] [listen]
    """
    cmd = argv[0]
    try:
        if cmd == "diag":
            res = do_diag()
        elif cmd == "scan":
            secs = int(argv[1]) if len(argv) > 1 else 8
            if BLE_OK:
                res = asyncio.run(do_scan(secs))
                # Si bleak no vio nada, intenta el respaldo nativo.
                if not res.get("devices"):
                    fb = scan_bluetoothctl(secs)
                    if fb.get("devices"):
                        res = fb
            else:
                res = scan_bluetoothctl(secs)
        elif cmd == "probe":
            res = asyncio.run(do_probe(argv[1]))
        elif cmd == "test":
            res = asyncio.run(do_test(argv[1], argv[2] if len(argv) > 2 else "auto",
                                      int(argv[3]) if len(argv) > 3 else 5,
                                      argv[4] if len(argv) > 4 else ""))
        elif cmd == "raw":
            res = asyncio.run(do_raw(argv[1], argv[2], argv[3] if len(argv) > 3 else "",
                                     argv[4] if len(argv) > 4 else "",
                                     int(argv[5]) if len(argv) > 5 else 4))
        else:
            res = {"error": f"comando CLI desconocido: {cmd}"}
    except Exception as e:  # noqa: BLE001
        res = {"error": f"excepción CLI: {str(e)[:180]}"}
    print("BLEJSON:" + json.dumps(res))


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
    if len(sys.argv) > 1:
        cli_main(sys.argv[1:])   # modo CLI (lo invoca jmhome por el relé Sniffing)
    else:
        main()                   # modo daemon (relé BLE propio)
