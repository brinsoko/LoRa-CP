import os, socket, serial, sys

# Configurable via env for quick tweaks without editing the file
SER = os.getenv("SERIAL_DEVICE", "/dev/cu.usbserial-589A0002751")
BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
PRINT_BYTES = os.getenv("PRINT_BYTES", "1") == "1"  # log serial/tcp traffic

def log(msg: str):
    print(msg, flush=True)

def main():
    ser = serial.Serial(SER, BAUD, timeout=0.05)
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 2001))
    srv.listen(1)
    log(f"listening on tcp://0.0.0.0:2001 (serial={SER} baud={BAUD})")
    conn, addr = srv.accept()
    log(f"client connected: {addr}")
    try:
        conn.settimeout(0.05)
        while True:
            # serial -> tcp
            b = ser.read(1024)
            if b:
                if PRINT_BYTES:
                    log(f"serial -> tcp ({len(b)} bytes): {b!r}")
                conn.sendall(b)
            # tcp -> serial (optional backchannel)
            try:
                r = conn.recv(1024)
                if r:
                    if PRINT_BYTES:
                        log(f"tcp -> serial ({len(r)} bytes): {r!r}")
                    ser.write(r)
            except socket.timeout:
                pass
    except KeyboardInterrupt:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        ser.close()
        srv.close()
        log("bridge closed")

if __name__ == "__main__":
    main()
