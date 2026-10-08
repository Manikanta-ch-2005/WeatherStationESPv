import machine
from machine import ADC, Pin, Timer, UART, SoftI2C, WDT
import time
import network
import urequests as requests
import json
import gc
import socket
import os
from ota import OTAUpdater

# =============================================================================
# 1. Configuration Management
# =============================================================================

CONFIG_FILE = "config.json"
OTA_CHECK_INTERVAL_MS = 60000

# Fixed hardware pins
HARDWARE_CFG = {
    "sht45": {
        "sda": 21,
        "scl": 22,
        "freq": 50000,
        "timeout": 50000,
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
    }
}

# These are only
# Put the real values in config.json on the ESP32.
DEFAULT_CONFIG = {
    "wifi_ssid": "YOUR_WIFI_SSID",
    "wifi_pass": "YOUR_WIFI_PASSWORD",
    "supabase_url": "https://YOUR_PROJECT.supabase.co",
    "supabase_key": "YOUR_SUPABASE_KEY",
    "supabase_table": "WEATHER_LIVE_NODE1",

    # Private GitHub repository API endpoint
    "ota_repo_api": "https://api.github.com/repos/OWNER/REPOSITORY/contents/",
    "ota_branch": "main",
    "ota_token": "YOUR_GITHUB_TOKEN",
    "loop_delay_s": 1,
    "max_buffer": 100
}


def load_config():
    config = DEFAULT_CONFIG.copy()

    try:
        with open(CONFIG_FILE, "r") as f:
            saved_config = json.load(f)

        for key, value in saved_config.items():
            config[key] = value

    except OSError:
        pass

    except ValueError as exc:
        print("Config JSON error:", exc)

    save_config(config)
    return config


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f)
    except Exception as exc:
        print("Config save failed:", exc)


def url_decode(value):
    value = value.replace("+", " ")
    result = ""
    i = 0

    while i < len(value):
        if value[i] == "%" and i + 2 < len(value):
            try:
                result += chr(int(value[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass

        result += value[i]
        i += 1

    return result


def html_escape(value):
    value = str(value)
    value = value.replace("&", "&amp;")
    value = value.replace("<", "&lt;")
    value = value.replace(">", "&gt;")
    value = value.replace('"', "&quot;")
    return value


def read_firmware_version():
    try:
        with open("version.txt", "r") as version_file:
            return version_file.read().strip()
    except OSError:
        return "0"


# =============================================================================
# 2. Cloud & Connectivity Driver (Wi-Fi + AP fallback)
# =============================================================================

class CloudClient:

    # HTTPS/TLS on ESP32 needs a sizeable temporary heap.  Never start a
    # second HTTPS operation while the device is already low on RAM.
    MIN_HTTPS_HEAP = 55000
    RETRY_COOLDOWN_MS = 15000

    def __init__(self, cfg):
        self.cfg = cfg
        self.wlan_sta = network.WLAN(network.STA_IF)
        self.wlan_ap = network.WLAN(network.AP_IF)
        self.last_upload_ms = 0
        self.last_upload_error_ms = 0
        self.upload_failures = 0

    def _free_heap(self):
        gc.collect()
        try:
            return gc.mem_free()
        except Exception:
            return 0

    def _can_use_https(self):
        free_heap = self._free_heap()
        if free_heap < self.MIN_HTTPS_HEAP:
            print("[NET] Low RAM: {} bytes free; skipping HTTPS.".format(free_heap))
            return False
        return True

    def _get_headers(self):
        key = self.cfg["supabase_key"]

        return {
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
            "Connection": "close"
        }

    def connect_wifi(self):
        self.wlan_ap.active(False)
        self.wlan_sta.active(True)

        if not self.wlan_sta.isconnected():
            print("Connecting to Wi-Fi:", self.cfg["wifi_ssid"])

            try:
                self.wlan_sta.disconnect()
            except Exception:
                pass

            self.wlan_sta.connect(
                self.cfg["wifi_ssid"],
                self.cfg["wifi_pass"]
            )

            timeout = 15

            while (
                not self.wlan_sta.isconnected()
                and timeout > 0
            ):
                time.sleep(1)
                timeout -= 1

        if self.wlan_sta.isconnected():
            print(
                "Wi-Fi Connected. IP:",
                self.wlan_sta.ifconfig()[0]
            )
            return True

        print(
            "Wi-Fi Failed. Starting Access Point mode..."
        )

        self.wlan_sta.active(False)
        self.wlan_ap.active(True)

        try:
            self.wlan_ap.config(
                essid="WeatherStation-Setup",
                password="adminpassword",
                authmode=3
            )
        except TypeError:
            self.wlan_ap.config(
                essid="WeatherStation-Setup",
                password="adminpassword"
            )

        print(
            "Connect to 'WeatherStation-Setup' "
            "(Pass: adminpassword)."
        )
        print(
            "Portal IP: 192.168.4.1"
        )

        return False

    def is_connected(self):
        return self.wlan_sta.isconnected()

    def push_payload(self, payload, force=False):
        if not self.is_connected():
            return False

        now = time.ticks_ms()
        if (not force and self.last_upload_error_ms and
                time.ticks_diff(now, self.last_upload_error_ms) < self.RETRY_COOLDOWN_MS):
            return False

        if not self._can_use_https():
            self.last_upload_error_ms = now
            return False

        url = "{}/rest/v1/{}".format(
            self.cfg["supabase_url"].rstrip("/"),
            self.cfg["supabase_table"]
        )

        response = None
        body = None

        try:
            gc.collect()
            body = json.dumps(payload)
            response = requests.post(
                url,
                headers=self._get_headers(),
                data=body
            )

            ok = response.status_code in (200, 201, 204)
            if ok:
                self.upload_failures = 0
                self.last_upload_ms = now
                self.last_upload_error_ms = 0
            else:
                self.upload_failures += 1
                self.last_upload_error_ms = now
                print("Supabase HTTP status:", response.status_code)
            return ok

        except Exception as exc:
            self.upload_failures += 1
            self.last_upload_error_ms = now
            print("Supabase request failed:", exc)
            return False

        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass
            body = None
            gc.collect()


# =============================================================================
# 3. GitHub OTA Driver - Private Repository + RAM Optimized
# =============================================================================

# =============================================================================
# 3. Web Interface & Dashboard
# =============================================================================

class WebInterface:

    def __init__(self, station):
        self.station = station
        self.server = socket.socket(
            socket.AF_INET,
            socket.SOCK_STREAM
        )
        self.server.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1
        )
        self.server.bind(("", 80))
        self.server.listen(2)
        self.server.setblocking(False)

    def _html_base(self, title, content):
        return """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{}</title>
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#f4f4f4;color:#333;}}
.card{{background:#fff;padding:20px;border-radius:10px;box-shadow:0 0 10px rgba(0,0,0,0.1);margin-bottom:20px;}}
.btn{{background:#007bff;color:white;padding:10px 15px;border:none;border-radius:5px;cursor:pointer;text-decoration:none;display:inline-block;margin-right:5px;}}
.btn-danger{{background:#dc3545;}}
input{{width:100%;padding:8px;margin:5px 0 15px;border:1px solid #ccc;border-radius:4px;box-sizing:border-box;}}
</style>
</head>
<body>
<h2>{}</h2>
{}
</body>
</html>""".format(
            html_escape(title),
            html_escape(title),
            content
        )

    def render_dashboard(self):
        station = self.station

        if station.cloud.is_connected():
            ip = station.cloud.wlan_sta.ifconfig()[0]
        else:
            ip = "192.168.4.1 (AP Mode)"

        data = station.current_data

        content = """
<div class="card">
<p><b>Temperature:</b> {} &deg;C</p>
<p><b>Humidity:</b> {} %</p>
<p><b>Wind Speed:</b> {} km/h</p>
<p><b>Wind Direction:</b> {}</p>
<p><b>Rain:</b> {} mm</p>
</div>

<div class="card">
<p><b>IP Address:</b> {}</p>
<p><b>Offline Buffer:</b> {} records</p>
<p><b>Last Status:</b> {}</p>
<p><b>Firmware Version:</b> {}</p>
<br>
<a href="/settings" class="btn">Change Settings</a>
<a href="/reboot" class="btn btn-danger">Reboot Device</a>
</div>

<script>
setTimeout(function(){{ location.reload(); }}, 5000);
</script>
""".format(
            html_escape(data.get("temperature", "--")),
            html_escape(data.get("humidity", "--")),
            html_escape(data.get("wind_speed", "--")),
            html_escape(data.get("wind_direction", "--")),
            html_escape(data.get("rain_gauge", "--")),
            html_escape(ip),
            len(station.offline_buffer),
            html_escape(station.last_error),
            html_escape(read_firmware_version())
        )

        return self._html_base(
            "Live Dashboard",
            content
        )

    def render_settings(self):
        cfg = self.station.cfg

        fields = [
            ("wifi_ssid", "Wi-Fi SSID", "text"),
            ("wifi_pass", "Wi-Fi Password", "password"),
            ("supabase_url", "Supabase URL", "text"),
            ("supabase_key", "Supabase Key", "password"),
            ("supabase_table", "Supabase Table", "text"),
            ("ota_repo_api", "GitHub Repository API", "text"),
            ("ota_branch", "OTA Branch", "text"),
            ("ota_token", "GitHub Token", "password"),
            ("loop_delay_s", "Loop Delay (s)", "number"),
            ("max_buffer", "Maximum Buffer Records", "number")
        ]

        form = '<div class="card"><form action="/save" method="POST">'

        for key, label, input_type in fields:
            value = html_escape(cfg.get(key, ""))
            form += (
                '<label>{}</label>'
                '<input type="{}" name="{}" value="{}">'
            ).format(
                html_escape(label),
                input_type,
                key,
                value
            )

        form += (
            '<button type="submit" class="btn">Save & Reboot</button>'
            '<a href="/" class="btn">Cancel</a>'
            '</form></div>'
        )

        return self._html_base(
            "System Settings",
            form
        )

    def _send_response(self, conn, body, status="200 OK"):
        header = (
            "HTTP/1.1 {}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).format(status)

        conn.send(
            (header + body).encode("utf-8")
        )

    def process_requests(self):
        conn = None

        try:
            conn, addr = self.server.accept()
            conn.settimeout(1.0)

            request = conn.recv(4096)

            if not request:
                return

            request_text = request.decode(
                "utf-8",
                "ignore"
            )

            first_line = request_text.split(
                "\r\n",
                1
            )[0].split()

            if len(first_line) < 2:
                return

            method = first_line[0]
            full_path = first_line[1]
            path = full_path.split("?", 1)[0]

            # -------------------------------------------------------------
            # GET /
            # -------------------------------------------------------------
            if method == "GET" and path == "/":
                self._send_response(
                    conn,
                    self.render_dashboard()
                )

            # -------------------------------------------------------------
            # GET /settings
            # -------------------------------------------------------------
            elif method == "GET" and path == "/settings":
                self._send_response(
                    conn,
                    self.render_settings()
                )

            # -------------------------------------------------------------
            # GET /reboot
            # -------------------------------------------------------------
            elif method == "GET" and path == "/reboot":
                self._send_response(
                    conn,
                    self._html_base(
                        "Reboot",
                        "<p>Rebooting...</p>"
                    )
                )

                conn.close()
                conn = None

                time.sleep_ms(200)
                machine.reset()

            # -------------------------------------------------------------
            # POST /save
            # -------------------------------------------------------------
            elif method == "POST" and path == "/save":

                body = ""

                if "\r\n\r\n" in request_text:
                    body = request_text.split(
                        "\r\n\r\n",
                        1
                    )[1]

                # Read extra body data if Content-Length says more exists.
                content_length = 0

                for line in request_text.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(
                                line.split(":", 1)[1].strip()
                            )
                        except ValueError:
                            content_length = 0
                        break

                body_bytes = len(body.encode("utf-8"))

                while body_bytes < content_length:
                    try:
                        part = conn.recv(512)
                    except Exception:
                        part = b""

                    if not part:
                        break

                    part_text = part.decode(
                        "utf-8",
                        "ignore"
                    )

                    body += part_text
                    body_bytes = len(
                        body.encode("utf-8")
                    )

                # Update only known config keys.
                for pair in body.split("&"):
                    if "=" not in pair:
                        continue

                    key, value = pair.split("=", 1)

                    if key in self.station.cfg:
                        self.station.cfg[key] = url_decode(value)

                save_config(self.station.cfg)

                self._send_response(
                    conn,
                    self._html_base(
                        "Saved",
                        "<p>Settings saved. Rebooting...</p>"
                    )
                )

                conn.close()
                conn = None

                time.sleep_ms(300)
                machine.reset()

            # -------------------------------------------------------------
            # Other paths
            # -------------------------------------------------------------
            else:
                self._send_response(
                    conn,
                    self._html_base(
                        "404",
                        "<h3>Page Not Found</h3>"
                        "<a href='/'>Home</a>"
                    ),
                    "404 Not Found"
                )

        except OSError:
            # Normal when the non-blocking socket has no request.
            pass

        except Exception as exc:
            self.station.last_error = (
                "Web Error: " + str(exc)
            )

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


# =============================================================================
# 5. SHT45 Temperature & Humidity Sensor
# =============================================================================

class SHT45Sensor:

    def __init__(
        self,
        sda_pin,
        scl_pin,
        freq,
        timeout,
        default_addr
    ):
        self.i2c = SoftI2C(
            scl=Pin(scl_pin),
            sda=Pin(sda_pin),
            freq=freq,
            timeout=timeout
        )
        self.addr = default_addr
        self.last_error = "Not tested"

        try:
            devices = self.i2c.scan()
            print("SHT45 I2C devices:", [hex(x) for x in devices])
            if 0x44 in devices:
                self.addr = 0x44
            elif 0x45 in devices:
                self.addr = 0x45
            else:
                self.last_error = "SHT45 not found on I2C"
        except Exception as exc:
            self.last_error = "I2C scan: " + str(exc)

    def _crc8(self, data):
        crc = 0xFF

        for byte in data:
            crc ^= byte

            for _ in range(8):
                if crc & 0x80:
                    crc = (
                        ((crc << 1) ^ 0x31)
                        & 0xFF
                    )
                else:
                    crc = (
                        (crc << 1)
                        & 0xFF
                    )

        return crc

    def read(self):
        try:
            try:
                self.i2c.writeto(
                    self.addr,
                    b"\xFD"
                )

            except OSError:
                devices = self.i2c.scan()

                if 0x44 in devices:
                    self.addr = 0x44
                elif 0x45 in devices:
                    self.addr = 0x45
                else:
                    return None, None

                self.i2c.writeto(
                    self.addr,
                    b"\xFD"
                )

            time.sleep_ms(30)

            data = self.i2c.readfrom(
                self.addr,
                6
            )

            if len(data) != 6:
                return None, None

            if self._crc8(data[0:2]) != data[2]:
                return None, None

            if self._crc8(data[3:5]) != data[5]:
                return None, None

            t_ticks = (
                (data[0] << 8)
                | data[1]
            )

            rh_ticks = (
                (data[3] << 8)
                | data[4]
            )

            temperature = (
                -45.0
                + (
                    175.0
                    * t_ticks
                    / 65535.0
                )
            )

            humidity = (
                -6.0
                + (
                    125.0
                    * rh_ticks
                    / 65535.0
                )
            )

            humidity = max(
                0.0,
                min(100.0, humidity)
            )

            return (
                round(temperature, 2),
                round(humidity, 2)
            )

        except Exception as exc:
            self.last_error = str(exc)
            return None, None


# =============================================================================
# 6. Wind Direction Sensor
# =============================================================================

class WindDirectionSensor:

    CAL_POINTS = [
        (4.00, 0.0),
        (5.50, 45.0),
        (7.15, 90.0),
        (9.30, 135.0),
        (11.50, 180.0),
        (13.60, 225.0),
        (16.00, 270.0),
        (18.40, 315.0),
        (20.00, 360.0)
    ]

    CENTER_DEG = [
        0.0,
        45.0,
        90.0,
        135.0,
        180.0,
        225.0,
        270.0,
        315.0
    ]

    CENTER_NAMES = [
        "North",
        "Northeast",
        "East",
        "Southeast",
        "South",
        "Southwest",
        "West",
        "Northwest"
    ]

    def __init__(
        self,
        pin_num,
        adc_min,
        adc_max
    ):
        self.adc = ADC(
            Pin(pin_num)
        )

        self.adc.atten(
            ADC.ATTN_11DB
        )

        self.adc.width(
            ADC.WIDTH_12BIT
        )

        self.adc_min = adc_min
        self.adc_max = adc_max

    def _interpolate_degree(self, current_ma):
        ma = max(
            self.CAL_POINTS[0][0],
            min(
                current_ma,
                self.CAL_POINTS[-1][0]
            )
        )

        for i in range(
            len(self.CAL_POINTS) - 1
        ):
            ma_a, deg_a = self.CAL_POINTS[i]
            ma_b, deg_b = self.CAL_POINTS[i + 1]

            if ma_a <= ma <= ma_b:
                if ma_b == ma_a:
                    return deg_a

                fraction = (
                    (ma - ma_a)
                    / (ma_b - ma_a)
                )

                return (
                    deg_a
                    + fraction
                    * (deg_b - deg_a)
                ) % 360.0

        return 0.0

    def _degree_to_cardinal(self, degree):
        best_index = 0
        best_diff = 360.0

        for i, center in enumerate(
            self.CENTER_DEG
        ):
            diff = abs(
                (
                    degree
                    - center
                    + 180.0
                ) % 360.0
                - 180.0
            )

            if diff < best_diff:
                best_diff = diff
                best_index = i

        return self.CENTER_NAMES[best_index]

    def read_direction(self):
        raw_value = (
            sum(
                self.adc.read()
                for _ in range(5)
            )
            // 5
        )

        span = (
            self.adc_max
            - self.adc_min
        )

        if span <= 0:
            return "Unknown"

        clamped = max(
            self.adc_min,
            min(raw_value, self.adc_max)
        )

        current_ma = (
            4.0
            + (
                (clamped - self.adc_min)
                / span
            )
            * 16.0
        )

        degree = self._interpolate_degree(
            current_ma
        )

        return self._degree_to_cardinal(
            degree
        )


# =============================================================================
# 7. Wind Speed Anemometer
# =============================================================================

class WindSpeedSensor:

    def __init__(
        self,
        pin_num,
        factor_kmh,
        debounce_ms
    ):
        self.pin = Pin(
            pin_num,
            Pin.IN,
            Pin.PULL_UP
        )

        self.factor_kmh = factor_kmh
        self.debounce_ms = debounce_ms
        self.pulse_count = 0
        self.last_pulse_time = time.ticks_ms()
        self.current_speed_kmh = 0.0

        self.pin.irq(
            trigger=Pin.IRQ_FALLING,
            handler=self._pulse_isr
        )

        self.timer = Timer(0)

        self.timer.init(
            period=1000,
            mode=Timer.PERIODIC,
            callback=self._calc_speed
        )

    def _pulse_isr(self, pin):
        now = time.ticks_ms()

        if (
            time.ticks_diff(
                now,
                self.last_pulse_time
            )
            >
            self.debounce_ms
        ):
            self.pulse_count += 1
            self.last_pulse_time = now

    def _calc_speed(self, timer):
        state = machine.disable_irq()

        pulses = self.pulse_count
        self.pulse_count = 0

        machine.enable_irq(state)

        self.current_speed_kmh = (
            pulses * self.factor_kmh
        )

    def read_speed(self):
        return round(
            self.current_speed_kmh,
            2
        )

    def deinit(self):
        try:
            self.timer.deinit()
        except Exception:
            pass


# =============================================================================
# 8. Rain Gauge Driver (UART Modbus)
# =============================================================================

class RainGaugeSensor:

    def __init__(
        self,
        uart_id,
        baudrate,
        tx_pin,
        rx_pin,
        slave_addr,
        mm_per_tip
    ):
        self.uart = UART(
            uart_id,
            baudrate=baudrate,
            tx=tx_pin,
            rx=rx_pin,
            timeout=200
        )

        self.slave_addr = slave_addr
        self.mm_per_tip = mm_per_tip
        self.last_tip_count = None

    def _modbus_crc(self, data):
        crc = 0xFFFF

        for value in data:
            crc ^= value

            for _ in range(8):
                if crc & 1:
                    crc = (
                        (crc >> 1)
                        ^ 0xA001
                    )
                else:
                    crc >>= 1

        return crc

    def read_raw_tips(self):
        request = bytearray([
            self.slave_addr,
            0x04,
            0x00,
            0x0A,
            0x00,
            0x02
        ])

        crc = self._modbus_crc(request)

        request.append(
            crc & 0xFF
        )

        request.append(
            (crc >> 8) & 0xFF
        )

        while self.uart.any():
            self.uart.read()

        self.uart.write(request)

        time.sleep_ms(200)

        if self.uart.any():
            response = self.uart.read(9)

            if response and len(response) == 9:
                response_crc = (
                    response[-2]
                    |
                    (response[-1] << 8)
                )

                if response_crc == self._modbus_crc(
                    response[:-2]
                ):
                    return (
                        (response[5] << 24)
                        |
                        (response[6] << 16)
                        |
                        (response[3] << 8)
                        |
                        response[4]
                    )

        return None

    def init_baseline(self):
        for _ in range(5):
            self.last_tip_count = self.read_raw_tips()

            if self.last_tip_count is not None:
                print(
                    "Rain Gauge initial tip count:",
                    self.last_tip_count
                )
                return True

            time.sleep(1)

        print(
            "Warning: Rain Gauge not responding."
        )

        return False

    def read_interval_rainfall(self):
        current_tips = self.read_raw_tips()
        interval_rain_mm = 0.0

        if current_tips is not None:
            if self.last_tip_count is None:
                self.last_tip_count = current_tips

            elif current_tips >= self.last_tip_count:
                interval_rain_mm = (
                    current_tips
                    - self.last_tip_count
                ) * self.mm_per_tip

                self.last_tip_count = current_tips

            else:
                # Counter reset/rollover.
                self.last_tip_count = current_tips

        return round(
            interval_rain_mm,
            2
        )


# =============================================================================
# 9. Main Weather Station
# =============================================================================

class WeatherStation:

    def __init__(self):
        self.cfg = load_config()

        self.offline_buffer = []
        self.last_error = "System OK"
        self.current_data = {}

        self.cloud = CloudClient(
            self.cfg
        )
        self.ota = OTAUpdater(self.cfg)

    def _initialize_hardware(self):
        print(
            "Initializing hardware..."
        )

        sht = HARDWARE_CFG["sht45"]
        self.sht45 = SHT45Sensor(
            sht["sda"],
            sht["scl"],
            sht["freq"],
            sht["timeout"],
            sht["default_addr"]
        )

        wd = HARDWARE_CFG["wind_direction"]
        self.wind_dir = WindDirectionSensor(
            wd["pin"],
            wd["adc_min"],
            wd["adc_max"]
        )

        ws = HARDWARE_CFG["wind_speed"]
        self.wind_spd = WindSpeedSensor(
            ws["pin"],
            ws["factor_kmh"],
            ws["debounce_ms"]
        )

        self.rain = RainGaugeSensor(
            **HARDWARE_CFG["rain_gauge"]
        )

        self.web = WebInterface(
            self
        )

    def _buffer_data(self):
        # Store a copy so future sensor reads do not modify buffered records.
        self.offline_buffer.append(
            self.current_data.copy()
        )

        self.last_error = (
            "Offline - Data buffered"
        )

        max_buffer = max(
            1,
            int(self.cfg["max_buffer"])
        )

        while len(self.offline_buffer) > max_buffer:
            self.offline_buffer.pop(0)

    def _empty_buffer(self, wdt):
        while (
            self.offline_buffer
            and
            self.cloud.is_connected()
        ):
            wdt.feed()

            if self.cloud.push_payload(
                self.offline_buffer[0],
                force=True
            ):
                self.offline_buffer.pop(0)
            else:
                break

    def run(self):
        self.cloud.connect_wifi()
        wdt = WDT(timeout=120000)
        self._initialize_hardware()
        self.rain.init_baseline()
        last_ota_check_ms = time.ticks_ms()

        while True:
            try:
                wdt.feed()

                if (
                    self.cloud.is_connected()
                    and time.ticks_diff(
                        time.ticks_ms(),
                        last_ota_check_ms
                    ) >= OTA_CHECK_INTERVAL_MS
                ):
                    last_ota_check_ms = time.ticks_ms()
                    gc.collect()
                    self.ota.check_for_updates(wdt)
                    wdt.feed()

                # Handle local web dashboard/settings.
                self.web.process_requests()

                # ---------------------------------------------------------
                # Read sensors
                # ---------------------------------------------------------
                temperature, humidity = self.sht45.read()

                if temperature is None or humidity is None:
                    print("SHT45 read failed:", self.sht45.last_error)

                self.current_data = {
                    "wind_direction": self.wind_dir.read_direction(),
                    "wind_speed": self.wind_spd.read_speed(),
                    "rain_gauge": self.rain.read_interval_rainfall(),
                    "temperature": (
                        temperature
                        if temperature is not None
                        else None
                    ),
                    "humidity": (
                        humidity
                        if humidity is not None
                        else None
                    )
                }

                # ---------------------------------------------------------
                # Push to cloud or buffer locally
                # ---------------------------------------------------------
                if self.cloud.is_connected():
                    gc.collect()

                    if self.cloud.push_payload(self.current_data):
                        self.last_error = "System OK"
                        self._empty_buffer(wdt)
                    else:
                        self._buffer_data()

                else:
                    self._buffer_data()

                    # Retry Wi-Fi approximately every 60 seconds.
                    if time.ticks_ms() % 60000 < 1000:
                        self.cloud.connect_wifi()

                # ---------------------------------------------------------
                # Serial output
                # ---------------------------------------------------------
                gc.collect()
                try:
                    free_heap = gc.mem_free()
                except Exception:
                    free_heap = -1

                print(
                    "Data:",
                    self.current_data,
                    "| Buffer:",
                    len(self.offline_buffer),
                    "| Free RAM:",
                    free_heap
                )

                # ---------------------------------------------------------
                # Delay while still servicing web UI and watchdog
                # ---------------------------------------------------------
                start_ticks = time.ticks_ms()

                delay_ms = max(
                    1,
                    int(self.cfg["loop_delay_s"])
                ) * 1000

                while (
                    time.ticks_diff(
                        time.ticks_ms(),
                        start_ticks
                    ) < delay_ms
                ):
                    wdt.feed()
                    self.web.process_requests()
                    time.sleep_ms(100)

            except Exception as exc:
                self.last_error = (
                    "Loop Exception: "
                    + str(exc)
                )

                print(
                    self.last_error
                )

                time.sleep(1)
                gc.collect()


# =============================================================================
# 10. Program Entry Point
# =============================================================================

if __name__ == "__main__":
    station = WeatherStation()
    station.run()

