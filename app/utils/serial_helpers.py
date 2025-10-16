import re, io, csv, os, serial
from serial.tools import list_ports

def normalize_uid(uid: str) -> str:
    if not uid: return ""
    return uid.replace(":", "").replace("-", "").strip().upper()

def find_serial_port(hint: str = "") -> str | None:
    ports = list(list_ports.comports())
    if not ports:
        return None
    hint_low = hint.lower()
    for p in ports:
        cand = f"{p.device} {p.description} {p.manufacturer}".lower()
        if hint_low and hint_low in cand:
            return p.device
    for p in ports:
        d = p.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyacm" in d or "ttyusb" in d:
            return p.device
    return ports[0].device

def read_uid_once(baudrate: int, hint: str, timeout: float) -> str | None:
    port = find_serial_port(hint)
    if not port:
        return None
    try:
        with serial.Serial(port, baudrate, timeout=timeout) as ser:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                return None
            tokens = re.findall(r'[0-9A-Fa-f:\-]{6,}', line)
            candidate = max(tokens, key=len) if tokens else line
            return normalize_uid(candidate)
    except Exception:
        return None