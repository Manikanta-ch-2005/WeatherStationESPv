import network
import socket
import json
import time
import machine
import os
import ntptime

CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {
    "ssid": "",
    "pass": "",
    "github_repo": "https://raw.githubusercontent.com/Manikanta-ch-2005/WeatherStationESPv/main"
}

def load_settings():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        with open(CONFIG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f)
        return DEFAULT_CONFIG

def save_settings(ssid, password):
    cfg = load_settings()
    cfg["ssid"] = ssid.strip()
    cfg["pass"] = password.strip()
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f)
    try:
        os.sync()
    except AttributeError:
        pass

def parse_post(request_str):
    try:
        body = request_str.split("\r\n\r\n", 1)[1]
        params = {}
        for pair in body.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                v = v.replace("+", " ")
                parts = v.split("%")
                res = parts[0]
                for p in parts[1:]:
                    res += chr(int(p[:2], 16)) + p[2:]
                params[k] = res
        return params
    except Exception:
        return {}

def start_ap_portal():
    # Kill station mode completely
    sta = network.WLAN(network.STA_IF)
    try:
        sta.disconnect()
        sta.active(False)
    except Exception:
        pass
    time.sleep_ms(200)

    # Launch isolated AP
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.ifconfig(('192.168.4.1', '255.255.255.0', '192.168.4.1', '8.8.8.8'))
    ap.config(essid="WeatherStation-AP", channel=6, authmode=network.AUTH_OPEN)

    print("\n" + "=" * 55)
    print("[SETUP MODE] AP Active. Connect to: WeatherStation-AP")
    print("Portal Address: http://192.168.4.1")
    print("=" * 55 + "\n")

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(1)

    html = """<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Setup</title><style>body{font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:20px;margin:0}
input{width:100%;padding:10px;margin:8px 0 16px 0;box-sizing:border-box}button{width:100%;padding:12px;background:#0284c7;color:#fff;border:none;font-weight:bold}</style></head>
<body><h2>Station Wi-Fi Setup</h2><form method="POST"><label>SSID:</label><input name="ssid" required>
<label>Password:</label><input type="password" name="pass"><button type="submit">Save & Connect</button></form></body></html>"""

    while True:
        try:
            conn, addr = s.accept()
            req = conn.recv(1024).decode('utf-8')
            if "POST" in req:
                params = parse_post(req)
                if "ssid" in params and params["ssid"]:
                    save_settings(params["ssid"], params.get("pass", ""))
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<h3>Credentials Saved. Connecting...</h3>")
                    conn.close()
                    s.close()
                    time.sleep(1)
                    machine.reset()
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(html)}\r\n\r\n{html}".encode())
            conn.close()
        except Exception:
            pass

# 1. Load configuration
cfg = load_settings()
ssid = cfg.get("ssid", "").strip()
password = cfg.get("pass", "").strip()

# Make sure AP mode is shut down by default
ap = network.WLAN(network.AP_IF)
ap.active(False)

if not ssid:
    print("[BOOT] No saved credentials found. Switching to AP mode...")
    start_ap_portal()

# 2. Connect to Station Wi-Fi
sta = network.WLAN(network.STA_IF)
sta.active(True)
sta.connect(ssid, password)

print(f"[BOOT] Connecting to Wi-Fi: {ssid}...")
timeout = 15
while not sta.isconnected() and timeout > 0:
    time.sleep(1)
    timeout -= 1

if not sta.isconnected():
    print("[BOOT] Wi-Fi connection timed out. Falling back to AP mode...")
    start_ap_portal()
else:
    ip = sta.ifconfig()[0]
    print("=" * 55)
    print(f"Connected to {ssid} successfully!")
    print(f"Station Dashboard URL: http://{ip}")
    print("=" * 55)
    
    # 3. Synchronize RTC clock for HTTPS certificates
    try:
        ntptime.settime()
        print("[BOOT] Clock synced via NTP.")
    except Exception as e:
        print(f"[BOOT] NTP sync skipped: {e}")