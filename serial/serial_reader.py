# serial_reader_tcp.py
import socket, time, requests

HOST = "host.docker.internal"
PORT = 2001
INGEST_URL = "http://web:5000/api/ingest"   # or http://localhost:5001/api/ingest if not using compose net

def lines_from_socket(sock):
    buf = b""
    sock.settimeout(5.0)
    while True:
        try:
            chunk = sock.recv(1024)
            if not chunk:
                # peer closed; yield any trailing partial line then stop
                if buf:
                    yield buf.decode(errors="ignore").strip()
                return
            buf += chunk
            while True:
                nl = buf.find(b"\n")
                cr = buf.find(b"\r")
                idxs = [i for i in (nl, cr) if i != -1]
                if not idxs:
                    break
                i = min(idxs)
                line = buf[:i].decode(errors="ignore").strip()
                buf = buf[i+1:]
                if line:
                    yield line
        except socket.timeout:
            # no data this interval; just loop
            continue

def main():
    while True:
        try:
            print(f"Connecting to {HOST}:{PORT} ...")
            with socket.create_connection((HOST, PORT), timeout=5.0) as s:
                print("Connected.")
                for line in lines_from_socket(s):
                    # Expect: DEV|PAYLOAD|RSSI|SNR (e.g., 1|hello299|-72.0|9.3)
                    parts = line.split("|", 3)
                    if len(parts) >= 2:
                        dev_id  = parts[0]
                        payload = parts[1]
                        rssi = float(parts[2]) if len(parts) > 2 and parts[2] else None
                        snr  = float(parts[3]) if len(parts) > 3 and parts[3] else None
                        print(f"< {dev_id}|{payload}|{rssi}|{snr}")
                        try:
                            requests.post(
                                INGEST_URL,
                                json={"dev_id": dev_id, "payload": payload, "rssi": rssi, "snr": snr},
                                timeout=2,
                            )
                        except Exception as e:
                            print(f"POST failed: {e}")
                    else:
                        print(f"Ignored: {line}")
        except Exception as e:
            print(f"TCP connect error: {e}")
            time.sleep(1)

if __name__ == "__main__":
    main()