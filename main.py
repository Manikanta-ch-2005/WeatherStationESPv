import machine
from machine import ADC, Pin, Timer, UART, SoftI2C
import time
import network
import json
import os
import gc

try:
    import socket
except ImportError:
    import usocket as socket

try:
    import ssl
except ImportError:
    import ussl as ssl

CONFIG_FILE = "config.json"
VERSION_FILE = "version.json"

try:
    with open(CONFIG_FILE, "r") as f:
        APP_CONFIG = json.load(f)
except Exception:
    APP_CONFIG = {
        "github_repo": "https://raw.githubusercontent.com/Manikanta-ch-2005/WeatherStationESPv/main"
    }

CONFIG = {
    "supabase": {
        "url": "https://sgkdpliqlhgiqsabxzxe.supabase.co",
        "key": "sb_publishable_9gBUGozvaGyoxkM9rW2tFg_pyF6esLE",
        "table": "WEATHER_LIVE_NODE1"
    },
    "sht45": {
        "sda": 21,
        "scl": 22,
        "freq": 100000,
        "default_addr": 0x44
    },
    "wind_direction": {
        "pin": 33,
        "adc_min": 248,
        "adc_max": 3847
    },
    "wind_speed": {
        "pin": 13,
        "debounce_ms": 5,
        "factor_kmh": 0.315
    },
    "rain_gauge": {
        "uart_id": 2,
        "baudrate": 9600,
        "tx_pin": 17,
        "rx_pin": 16,
        "slave_addr": 0xC0,
        "mm_per_tip": 0.28
    },
    "loop_delay_ms": 50,
    "cloud_sync_interval_s": 15,
    "ota_interval_s": 3600
}

def reset_wifi_credentials():
    try:
        cfg = {}
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        cfg["ssid"] = ""
        cfg["pass"] = ""
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
        try:
            os.sync()
        except AttributeError:
            pass
        return True
    except Exception as e:
        print("[RESET] Wi-Fi wipe error:", e)
        return False

# ==============================================================================
# Hardened HTTPS Client (Blocking Handshake -> Timeout Data Transfer)
# ==============================================================================
def raw_http_request(method, url, headers=None, data=None, timeout=8.0):
    gc.collect()
    if headers is None:
        headers = {}

    proto, _, host_path = url.partition("://")
    is_ssl = (proto == "https")
    host, _, path = host_path.partition("/")
    path = "/" + path
    port = 443 if is_ssl else 80
    if ":" in host:
        host, port = host.split(":")
        port = int(port)

    raw_s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # MUST stay blocking during connection and TLS negotiation to prevent EBUSY (16)
    raw_s.settimeout(None)
    s = raw_s

    try:
        addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
        raw_s.connect(addr)
        if is_ssl:
            s = ssl.wrap_socket(raw_s, server_hostname=host)

        # Set transport timeout only AFTER TLS handshake is complete
        raw_s.settimeout(timeout)

        req = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n"
        for k, v in headers.items():
            req += f"{k}: {v}\r\n"
        if data:
            req += f"Content-Length: {len(data)}\r\n\r\n{data}"
        else:
            req += "\r\n"

        if hasattr(s, "write"):
            s.write(req.encode("utf-8"))
        else:
            s.sendall(req.encode("utf-8"))

        response = b""
        while True:
            try:
                chunk = s.read(512) if hasattr(s, "read") else s.recv(512)
                if not chunk:
                    break
                response += chunk
            except Exception:
                break

        header_data, _, body_data = response.partition(b"\r\n\r\n")
        if not header_data:
            return 0, ""
        first_line = header_data.split(b"\r\n")[0].decode("utf-8")
        parts = first_line.split(" ")
        status_code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return status_code, body_data.decode("utf-8")
    finally:
        try:
            s.close()
        except Exception:
            pass
        try:
            raw_s.close()
        except Exception:
            pass
        gc.collect()

# ==============================================================================
# Stream-Based OTA Engine
# ==============================================================================
class OTAEngine:
    def __init__(self, raw_base_url):
        self.base_url = raw_base_url.replace("/refs/heads", "").rstrip("/")

    def get_local_version(self):
        try:
            with open(VERSION_FILE, "r") as f:
                return json.load(f).get("version", "0.0.0")
        except (OSError, ValueError):
            return "0.0.0"

    def check_and_update(self, files=["main.py"]):
        v_url = f"{self.base_url}/version.json"
        print(f"[OTA] Checking version at {v_url}")

        try:
            status, body = raw_http_request("GET", v_url, headers={"User-Agent": "ESP32"}, timeout=6.0)
            if status != 200:
                print(f"[OTA] Version check failed (HTTP {status})")
                return False
            remote_ver = json.loads(body).get("version")
        except Exception as err:
            print(f"[OTA] Check skipped or timed out: {err}")
            return False

        local_ver = self.get_local_version()
        if remote_ver == local_ver or not remote_ver:
            print(f"[OTA] Firmware is up to date (v{local_ver}).")
            return False

        print(f"[OTA] New version found: v{local_ver} -> v{remote_ver}. Downloading...")
        for fname in files:
            f_url = f"{self.base_url}/{fname}"
            try:
                status, file_content = raw_http_request("GET", f_url, headers={"User-Agent": "ESP32"}, timeout=8.0)
                if status == 200 and len(file_content) > 50:
                    with open(fname + ".tmp", "w") as f:
                        f.write(file_content)
                    try:
                        os.remove(fname)
                    except OSError:
                        pass
                    os.rename(fname + ".tmp", fname)
                    print(f"[OTA] Downloaded {fname}")
                else:
                    print(f"[OTA] Download failed for {fname}")
                    return False
            except Exception as e:
                print(f"[OTA] Error downloading {fname}: {e}")
                return False

        with open(VERSION_FILE, "w") as vf:
            json.dump({"version": remote_ver}, vf)
        try:
            os.sync()
        except AttributeError:
            pass

        print("[OTA] Update applied successfully! Rebooting station...")
        time.sleep(1)
        machine.reset()
        return True

# ==============================================================================
# Cloud Client (Supabase Telemetry)
# ==============================================================================
class CloudClient:
    def __init__(self, db_cfg):
        self.db_url = f"{db_cfg['url']}/rest/v1/{db_cfg['table']}"
        self.headers = {
            "apikey": db_cfg["key"],
            "Authorization": f"Bearer {db_cfg['key']}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }
        self.wlan = network.WLAN(network.STA_IF)

    def is_connected(self):
        return self.wlan.isconnected()

    def push(self, direction, speed, rain, temp, hum):
        if not self.is_connected():
            return False

        payload = json.dumps({
            "wind_direction": direction,
            "wind_speed": speed,
            "rain_gauge": rain,
            "temperature": temp,
            "humidity": hum
        })
        try:
            status, resp = raw_http_request("POST", self.db_url, headers=self.headers, data=payload, timeout=6.0)
            if status in [200, 201, 204]:
                print(f" -> Supabase Push: Success (HTTP {status})")
                return True
            else:
                print(f" -> Supabase HTTP Reject: {status} - {resp[:100]}")
                return False
        except Exception as e:
            print(f" -> Supabase Push Error: {e}")
            return False

# ==============================================================================
# Sensor Drivers
# ==============================================================================
class SHT45Sensor:
    def __init__(self, sda_pin, scl_pin, freq=100000, default_addr=0x44):
        self.i2c = SoftI2C(scl=Pin(scl_pin), sda=Pin(sda_pin), freq=freq)
        self.addr = default_addr

        devices = [hex(x) for x in self.i2c.scan()]
        print(f"[SHT45] Bus scan on SDA={sda_pin}, SCL={scl_pin}: {devices}")
        if '0x44' in devices:
            self.addr = 0x44
        elif '0x45' in devices:
            self.addr = 0x45

    def _crc8(self, data):
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = ((crc << 1) ^ 0x31) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
        return crc

    def read(self):
        try:
            self.i2c.writeto(self.addr, b'\xFD')
            time.sleep_ms(20)
            data = self.i2c.readfrom(self.addr, 6)
            if len(data) != 6 or self._crc8(data[0:2]) != data[2] or self._crc8(data[3:5]) != data[5]:
                return None, None
            temp = -45.0 + (175.0 * ((data[0] << 8) | data[1]) / 65535.0)
            hum = max(0.0, min(100.0, -6.0 + (125.0 * ((data[3] << 8) | data[4]) / 65535.0)))
            return round(temp, 2), round(hum, 2)
        except Exception:
            return None, None

class WindDirectionSensor:
    CAL_POINTS = [
        (4.00, 0.0), (5.50, 45.0), (7.15, 90.0), (9.30, 135.0),
        (11.50, 180.0), (13.60, 225.0), (16.00, 270.0), (18.40, 315.0), (20.00, 360.0),
    ]
    CENTER_DEG = [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0]
    CENTER_NAMES = ["North", "Northeast", "East", "Southeast", "South", "Southwest", "West", "Northwest"]

    def __init__(self, pin_num, adc_min=248, adc_max=3847):
        self.adc = ADC(Pin(pin_num))
        self.adc.atten(ADC.ATTN_11DB)
        self.adc.width(ADC.WIDTH_12BIT)
        self.adc_min = adc_min
        self.adc_max = adc_max

    def _interpolate_degree(self, ma):
        ma = max(self.CAL_POINTS[0][0], min(ma, self.CAL_POINTS[-1][0]))
        for i in range(len(self.CAL_POINTS) - 1):
            ma_a, deg_a = self.CAL_POINTS[i]
            ma_b, deg_b = self.CAL_POINTS[i + 1]
            if ma_a <= ma <= ma_b:
                return (deg_a + ((ma - ma_a) / (ma_b - ma_a)) * (deg_b - deg_a)) % 360.0
        return 0.0

    def read_direction(self):
        raw = sum(self.adc.read() for _ in range(5)) // 5
        clamped = max(self.adc_min, min(raw, self.adc_max))
        ma = 4.0 + ((clamped - self.adc_min) / (self.adc_max - self.adc_min)) * 16.0
        deg = self._interpolate_degree(ma)
        best_i, best_diff = 0, 360.0
        for i, center in enumerate(self.CENTER_DEG):
            diff = abs((deg - center + 180.0) % 360.0 - 180.0)
            if diff < best_diff:
                best_diff, best_i = diff, i
        return self.CENTER_NAMES[best_i]

class WindSpeedSensor:
    def __init__(self, pin_num, factor_kmh=0.315, debounce_ms=5):
        self.pin = Pin(pin_num, Pin.IN, Pin.PULL_UP)
        self.factor_kmh = factor_kmh
        self.debounce_ms = debounce_ms
        self.pulse_count = 0
        self.last_pulse = time.ticks_ms()
        self.speed = 0.0
        self.pin.irq(trigger=Pin.IRQ_FALLING, handler=self._pulse_isr)
        self.timer = Timer(0)
        self.timer.init(period=1000, mode=Timer.PERIODIC, callback=self._calc_speed)

    def _pulse_isr(self, pin):
        now = time.ticks_ms()
        if time.ticks_diff(now, self.last_pulse) > self.debounce_ms:
            self.pulse_count += 1
            self.last_pulse = now

    def _calc_speed(self, t):
        state = machine.disable_irq()
        p = self.pulse_count
        self.pulse_count = 0
        machine.enable_irq(state)
        self.speed = p * self.factor_kmh

    def read_speed(self):
        return round(self.speed, 2)

    def deinit(self):
        self.timer.deinit()

class RainGaugeSensor:
    def __init__(self, uart_id=2, baudrate=9600, tx_pin=17, rx_pin=16, slave_addr=0xC0, mm_per_tip=0.28):
        self.uart = UART(uart_id, baudrate=baudrate, tx=tx_pin, rx=rx_pin, timeout=100)
        self.slave_addr = slave_addr
        self.mm_per_tip = mm_per_tip
        self.last_tip = None

    def _modbus_crc(self, data):
        crc = 0xFFFF
        for pos in data:
            crc ^= pos
            for _ in range(8):
                crc = ((crc >> 1) ^ 0xA001) if (crc & 1) else (crc >> 1)
        return crc

    def read_raw_tips(self):
        req = bytearray([self.slave_addr, 0x04, 0x00, 0x0A, 0x00, 0x02])
        crc = self._modbus_crc(req)
        req.append(crc & 0xFF)
        req.append((crc >> 8) & 0xFF)
        while self.uart.any():
            self.uart.read()
        self.uart.write(req)
        time.sleep_ms(60)
        if self.uart.any():
            resp = self.uart.read(9)
            if resp and len(resp) == 9:
                if (resp[-2] | (resp[-1] << 8)) == self._modbus_crc(resp[:-2]):
                    return (resp[5] << 24) | (resp[6] << 16) | (resp[3] << 8) | resp[4]
        return None

    def init_baseline(self):
        for _ in range(3):
            self.last_tip = self.read_raw_tips()
            if self.last_tip is not None:
                return True
            time.sleep_ms(100)
        return False

    def read_interval_rainfall(self):
        tips = self.read_raw_tips()
        rain = 0.0
        if tips is not None and self.last_tip is not None:
            if tips < self.last_tip:
                self.last_tip = tips
            rain = (tips - self.last_tip) * self.mm_per_tip
            self.last_tip = tips
        return round(rain, 2)

# ==============================================================================
# Local Dashboard Server (Station IP Port 80)
# ==============================================================================
class EmbeddedWebServer:
    def __init__(self, ota_ref):
        self.ota = ota_ref
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(('', 80))
        self.server.listen(3)
        self.server.setblocking(False)

    def handle_clients(self, telemetry):
        try:
            conn, addr = self.server.accept()
        except OSError:
            return

        try:
            conn.settimeout(1.5)
            raw = conn.recv(1024)
            if not raw:
                conn.close()
                return

            req = raw.decode("utf-8")
            print(f"[HTTP INCOMING] {req.split()[0]} {req.split()[1]}")

            if "GET /favicon.ico" in req:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
                conn.close()
                return

            # Action: Trigger OTA Update
            if "GET /update" in req:
                body = (
                    "<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    "<title>OTA Update</title></head><body style=\"background:#090d16;color:#e2e8f0;font-family:sans-serif;padding:30px;text-align:center\">"
                    "<h2>Checking GitHub for updates...</h2>"
                    "<p>If an update is found, the station will download it and restart automatically.</p>"
                    "<p><a href=\"/\" style=\"color:#38bdf8;text-decoration:none;font-weight:bold\">Return to Dashboard</a></p></body></html>"
                )
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                conn.sendall((header + body).encode("utf-8"))
                time.sleep_ms(300)
                conn.close()
                print("[WEB] Triggering OTA update from dashboard...")
                self.ota.check_and_update(files=["main.py"])
                return

            # Action: Wipe Wi-Fi & Launch AP
            if "GET /reset-wifi" in req:
                body = (
                    "<!DOCTYPE html><html><head><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                    "<title>Wi-Fi Reset</title></head><body style=\"background:#090d16;color:#e2e8f0;font-family:sans-serif;padding:30px;text-align:center\">"
                    "<h2>Wi-Fi Credentials Cleared!</h2>"
                    "<p>Rebooting into Access Point setup mode...</p>"
                    "<p>Connect your phone or PC to <b>WeatherStation-AP</b> at <b>http://192.168.4.1</b></p></body></html>"
                )
                header = f"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                conn.sendall((header + body).encode("utf-8"))
                time.sleep_ms(500)
                conn.close()
                print("[WEB] Wiping Wi-Fi credentials and resetting station...")
                reset_wifi_credentials()
                time.sleep(1)
                machine.reset()
                return

            # Route: Dashboard
            local_v = self.ota.get_local_version()
            html_body = f"""<!DOCTYPE html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>Station Live Node</title>
<style>
  body{{font-family:Arial,sans-serif;background:#090d16;color:#e2e8f0;padding:20px;margin:0}}
  .container{{max-width:480px;margin:auto}}
  h1{{color:#38bdf8;font-size:22px;margin-bottom:4px}}
  .badge{{background:#1e293b;color:#94a3b8;font-size:12px;padding:4px 8px;border-radius:4px;display:inline-block;margin-bottom:15px}}
  .grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
  .card{{background:#161f30;padding:16px;border-radius:8px;border:1px solid #23314d}}
  .val{{font-size:22px;font-weight:bold;color:#f8fafc;margin-top:6px}}
  .lbl{{font-size:12px;color:#94a3b8;text-transform:uppercase}}
  .actions{{margin-top:20px;display:flex;flex-direction:column;gap:10px}}
  .btn{{display:block;width:100%;padding:12px;text-align:center;text-decoration:none;border-radius:6px;font-weight:bold;box-sizing:border-box}}
  .btn-primary{{background:#0284c7;color:#fff}}
  .btn-danger{{background:#dc2626;color:#fff}}
</style></head><body><div class="container">
  <h1>Weather Station Node</h1>
  <span class="badge">Firmware: v{local_v}</span>
  <div class="grid">
    <div class="card"><div class="lbl">Temperature</div><div class="val">{telemetry.get('temp')} &deg;C</div></div>
    <div class="card"><div class="lbl">Humidity</div><div class="val">{telemetry.get('hum')} %</div></div>
    <div class="card"><div class="lbl">Wind Direction</div><div class="val">{telemetry.get('dir')}</div></div>
    <div class="card"><div class="lbl">Wind Speed</div><div class="val">{telemetry.get('spd')} km/h</div></div>
    <div class="card" style="grid-column: span 2"><div class="lbl">Rainfall (Interval)</div><div class="val">{telemetry.get('rain')} mm</div></div>
  </div>
  <div class="actions">
    <a href="/update" class="btn btn-primary" onclick="return confirm('Check GitHub for firmware updates now?')">Check & Apply GitHub OTA</a>
    <a href="/reset-wifi" class="btn btn-danger" onclick="return confirm('Disconnect Wi-Fi and enter AP Mode?')">Reset Wi-Fi & Enter AP Mode</a>
  </div>
</div></body></html>"""

            body_bytes = html_body.encode("utf-8")
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body_bytes)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("utf-8")

            conn.sendall(header + body_bytes)
            conn.close()

        except Exception:
            try:
                conn.close()
            except Exception:
                pass

# ==============================================================================
# Main Telemetry Loop
# ==============================================================================
class WeatherStation:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ota = OTAEngine(APP_CONFIG["github_repo"])
        self.cloud = CloudClient(cfg["supabase"])
        self.sht45 = SHT45Sensor(sda_pin=cfg["sht45"]["sda"], scl_pin=cfg["sht45"]["scl"])
        self.wind_dir = WindDirectionSensor(
            pin_num=cfg["wind_direction"]["pin"],
            adc_min=cfg["wind_direction"]["adc_min"],
            adc_max=cfg["wind_direction"]["adc_max"]
        )
        self.wind_spd = WindSpeedSensor(pin_num=cfg["wind_speed"]["pin"], factor_kmh=cfg["wind_speed"]["factor_kmh"])
        self.rain = RainGaugeSensor(
            uart_id=cfg["rain_gauge"]["uart_id"],
            baudrate=cfg["rain_gauge"]["baudrate"],
            tx_pin=cfg["rain_gauge"]["tx_pin"],
            rx_pin=cfg["rain_gauge"]["rx_pin"]
        )

        self.web = EmbeddedWebServer(self.ota)
        self.last_ota_check = time.time()
        self.last_cloud_push = 0
        self.latest_data = {"dir": "-", "spd": 0.0, "rain": 0.0, "temp": "-", "hum": "-"}

    def run(self):
        self.rain.init_baseline()
        print("Station running. Telemetry & Web Server active.\n" + "-" * 50)

        try:
            while True:
                now = time.time()

                # 1. Background hourly OTA check
                if now - self.last_ota_check > self.cfg["ota_interval_s"]:
                    self.ota.check_and_update(files=["main.py"])
                    self.last_ota_check = now

                # 2. Read Sensors
                direction = self.wind_dir.read_direction()
                speed_kmh = self.wind_spd.read_speed()
                temp, hum = self.sht45.read()
                rain_mm = self.rain.read_interval_rainfall()

                self.latest_data = {
                    "dir": direction,
                    "spd": speed_kmh,
                    "rain": rain_mm,
                    "temp": temp,
                    "hum": hum
                }

                # 3. Supabase push every 15s
                if now - self.last_cloud_push >= self.cfg["cloud_sync_interval_s"]:
                    print(f"Dir: {direction:10} | Spd: {speed_kmh:5.2f} km/h | Rain: {rain_mm:5.2f} mm | Temp: {str(temp):>5}°C | Hum: {str(hum):>5}%")
                    self.cloud.push(direction, speed_kmh, rain_mm, temp, hum)
                    self.last_cloud_push = now

                # 4. Service local dashboard
                self.web.handle_clients(self.latest_data)
                time.sleep_ms(self.cfg["loop_delay_ms"])

        except KeyboardInterrupt:
            self.wind_spd.deinit()

if __name__ == "__main__":
    station = WeatherStation(CONFIG)
    station.run()