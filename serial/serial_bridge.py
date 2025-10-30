import socket, serial, sys
SER='/dev/cu.usbserial-586E0047471'
BAUD=115200

ser = serial.Serial(SER, BAUD, timeout=0.05)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(('0.0.0.0', 2001)); srv.listen(1)
print('listening on tcp://0.0.0.0:2001 ...')
conn, addr = srv.accept()
print('client connected:', addr)
try:
    conn.settimeout(0.05)
    while True:
        # serial -> tcp
        b = ser.read(1024)
        if b:
            conn.sendall(b)
        # tcp -> serial (optional backchannel)
        try:
            r = conn.recv(1024)
            if r:
                ser.write(r)
        except socket.timeout:
            pass
except KeyboardInterrupt:
    pass
finally:
    try: conn.close()
    except: pass
    ser.close()
    srv.close()
    print('bridge closed')