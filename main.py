import machine
from machine import ADC, Pin, Timer, UART, SoftI2C
import time
import network
import urequests as requests
import json
import gc


# ==============================================================================
# 1. System Configuration
# ==============================================================================

CONFIG = {
    "wifi": {
        "ssid": "vivo Y100A",
        "pass": "personal.s",
        "timeout": 15
    },

    "supabase": {
        "url": "https://sgkdpliqlhgiqsabxzxe.supabase.co",
        "key": "sb_publishable_9gBUGozvaGyoxkM9rW2tFg_pyF6esLE",
        "table": "WEATHER_LIVE_NODE1"
    },

    "ota": {
        "repo_api": "https://api.github.com/repos/Manikanta-ch-2005/WeatherStationESPv/contents/",
        "branch": "main",
        "github_token": "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",
        "check_interval_s": 3600
    },

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
    },

    "loop_delay_s": 1
}


# ==============================================================================
# 2. GitHub OTA Driver
# ==============================================================================

class GitHubOTA:

    def __init__(self, repo_api, branch, github_token, check_interval_s):

        self.repo_api = (
            repo_api if repo_api.endswith('/') else repo_api + '/'
        )

        self.branch = branch
        self.github_token = github_token

        self.check_interval_ms = check_interval_s * 1000

        self.last_check = 0

        self.local_version = self._read_local_version()

    def _read_local_version(self):

        try:

            with open("version.txt", "r") as f:
                return f.read().strip()

        except OSError:

            return "0"

    def _fetch_file(self, filename):

        gc.collect()

        url = (
            self.repo_api
            +
            filename
            +
            "?ref="
            +
            self.branch
        )

        headers = {
            "User-Agent": "ESP32-MicroPython-OTA",
            "Authorization": "Bearer " + self.github_token,
            "Accept": "application/vnd.github.raw+json"
        }

        response = requests.get(
            url,
            headers=headers
        )

        if response.status_code != 200:

            status = response.status_code

            try:
                message = response.text
            except Exception:
                message = ""

            response.close()

            raise Exception(
                "GitHub HTTP "
                + str(status)
                + ": "
                + message
            )

        return response

    def check(self):

        now = time.ticks_ms()

        if (
            self.last_check != 0
            and
            time.ticks_diff(
                now,
                self.last_check
            )
            <
            self.check_interval_ms
        ):

            return

        self.last_check = now

        print("\n[OTA] Checking GitHub for updates...")

        try:

            # ------------------------------------------------------------------
            # 1. Read remote version.txt
            # ------------------------------------------------------------------

            response = self._fetch_file("version.txt")

            remote_version = response.text.strip()

            response.close()

            print(
                "[OTA] Local version:",
                self.local_version
            )

            print(
                "[OTA] Remote version:",
                remote_version
            )

            # ------------------------------------------------------------------
            # 2. Compare versions
            # ------------------------------------------------------------------

            if (
                remote_version != self.local_version
                and
                remote_version != ""
            ):

                print(
                    "[OTA] New version found:",
                    remote_version
                )

                print(
                    "[OTA] Downloading main.py..."
                )

                # --------------------------------------------------------------
                # 3. Download new main.py
                # --------------------------------------------------------------

                response = self._fetch_file("main.py")

                new_main = response.text

                response.close()

                if (
                    new_main is None
                    or
                    len(new_main) == 0
                ):

                    print(
                        "[OTA] Downloaded main.py is empty."
                    )

                    return

                # --------------------------------------------------------------
                # 4. Save new main.py
                # --------------------------------------------------------------

                with open(
                    "main.py",
                    "w"
                ) as f:

                    f.write(new_main)

                del new_main

                gc.collect()

                # --------------------------------------------------------------
                # 5. Update version.txt
                # --------------------------------------------------------------

                with open(
                    "version.txt",
                    "w"
                ) as f:

                    f.write(remote_version)

                self.local_version = remote_version

                print(
                    "[OTA] Update applied successfully."
                )

                print(
                    "[OTA] Rebooting ESP32..."
                )

                time.sleep(2)

                machine.reset()

            else:

                print(
                    "[OTA] No update needed."
                )

                print(
                    "[OTA] Running version:",
                    self.local_version
                )

                print()

        except Exception as e:

            print(
                "[OTA] Check failed:",
                e
            )

            print()


# ==============================================================================
# 3. Cloud & Connectivity Driver
# ==============================================================================

class CloudClient:

    def __init__(self, wifi_cfg, db_cfg):

        self.wifi_ssid = wifi_cfg["ssid"]
        self.wifi_pass = wifi_cfg["pass"]
        self.wifi_timeout = wifi_cfg["timeout"]

        self.db_url = (
            f"{db_cfg['url']}/rest/v1/{db_cfg['table']}"
        )

        self.headers = {

            "apikey": db_cfg["key"],

            "Authorization":
                f"Bearer {db_cfg['key']}",

            "Content-Type":
                "application/json",

            "Prefer":
                "return=minimal"
        }

        self.wlan = network.WLAN(
            network.STA_IF
        )

    def connect_wifi(self):

        self.wlan.active(True)

        if not self.wlan.isconnected():

            print(
                f"Connecting to Wi-Fi: "
                f"{self.wifi_ssid}..."
            )

            self.wlan.connect(
                self.wifi_ssid,
                self.wifi_pass
            )

            timeout = self.wifi_timeout

            while (
                not self.wlan.isconnected()
                and
                timeout > 0
            ):

                time.sleep(1)

                timeout -= 1

        if self.wlan.isconnected():

            print(
                "Wi-Fi Connected. IP:",
                self.wlan.ifconfig()[0]
            )

        else:

            print(
                "Wi-Fi Connection Failed. "
                "Will retry later."
            )

    def is_connected(self):

        return self.wlan.isconnected()

    def push(
        self,
        direction,
        speed,
        rain,
        temp,
        hum
    ):

        if not self.is_connected():

            print(
                "No Wi-Fi. Skipping Cloud Push."
            )

            return False

        payload = {

            "wind_direction":
                direction,

            "wind_speed":
                speed,

            "rain_gauge":
                rain,

            "temperature":
                temp,

            "humidity":
                hum
        }

        try:

            res = requests.post(
                self.db_url,
                headers=self.headers,
                data=json.dumps(payload)
            )

            success = (
                res.status_code
                in
                [200, 201, 204]
            )

            if not success:

                print(
                    f" -> Supabase Error: "
                    f"{res.status_code} - "
                    f"{res.text}"
                )

            else:

                print(
                    " -> Successfully pushed to Supabase"
                )

            res.close()

            return success

        except Exception as e:

            print(
                f" -> HTTP POST Exception: {e}"
            )

            return False


# ==============================================================================
# 4. 7Semi SHT45 Temperature & Humidity Driver
# ==============================================================================

class SHT45Sensor:

    def __init__(
        self,
        sda_pin,
        scl_pin,
        freq=50000,
        timeout=50000,
        default_addr=0x44
    ):

        self.i2c = SoftI2C(
            scl=Pin(scl_pin),
            sda=Pin(sda_pin),
            freq=freq,
            timeout=timeout
        )

        self.addr = default_addr

    def _crc8(self, data):

        crc = 0xFF

        for byte in data:

            crc ^= byte

            for _ in range(8):

                if crc & 0x80:

                    crc = (
                        ((crc << 1) ^ 0x31)
                        &
                        0xFF
                    )

                else:

                    crc = (
                        (crc << 1)
                        &
                        0xFF
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

            if (
                self._crc8(data[0:2])
                !=
                data[2]
                or
                self._crc8(data[3:5])
                !=
                data[5]
            ):

                return None, None

            t_ticks = (
                (data[0] << 8)
                |
                data[1]
            )

            rh_ticks = (
                (data[3] << 8)
                |
                data[4]
            )

            temp = (
                -45.0
                +
                (
                    175.0
                    *
                    t_ticks
                    /
                    65535.0
                )
            )

            hum = max(
                0.0,
                min(
                    100.0,
                    -6.0
                    +
                    (
                        125.0
                        *
                        rh_ticks
                        /
                        65535.0
                    )
                )
            )

            return (
                round(temp, 2),
                round(hum, 2)
            )

        except Exception:

            return None, None


# ==============================================================================
# 5. Wind Direction Sensor (4-20mA to ADC)
# ==============================================================================

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
        adc_min=248,
        adc_max=3847
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

    def _interpolate_degree(
        self,
        current_ma
    ):

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

            ma_a, deg_a = (
                self.CAL_POINTS[i]
            )

            ma_b, deg_b = (
                self.CAL_POINTS[i + 1]
            )

            if (
                ma_a
                <=
                ma
                <=
                ma_b
            ):

                if ma_b == ma_a:

                    return deg_a

                frac = (
                    (ma - ma_a)
                    /
                    (ma_b - ma_a)
                )

                return (
                    deg_a
                    +
                    frac
                    *
                    (deg_b - deg_a)
                ) % 360.0

        return 0.0

    def _degree_to_cardinal(
        self,
        degree
    ):

        best_i = 0
        best_diff = 360.0

        for i, center in enumerate(
            self.CENTER_DEG
        ):

            diff = abs(
                (
                    degree
                    -
                    center
                    +
                    180.0
                )
                %
                360.0
                -
                180.0
            )

            if diff < best_diff:

                best_diff = diff
                best_i = i

        return self.CENTER_NAMES[best_i]

    def read_direction(self):

        raw_val = (
            sum(
                self.adc.read()
                for _ in range(5)
            )
            //
            5
        )

        clamped_val = max(
            self.adc_min,
            min(
                raw_val,
                self.adc_max
            )
        )

        current_ma = (
            4.0
            +
            (
                (
                    clamped_val
                    -
                    self.adc_min
                )
                /
                (
                    self.adc_max
                    -
                    self.adc_min
                )
            )
            *
            16.0
        )

        degree = (
            self._interpolate_degree(
                current_ma
            )
        )

        return (
            self._degree_to_cardinal(
                degree
            )
        )


# ==============================================================================
# 6. Wind Speed Anemometer (NPN Pulse Interrupt)
# ==============================================================================

class WindSpeedSensor:

    def __init__(
        self,
        pin_num,
        factor_kmh=0.315,
        debounce_ms=5
    ):

        self.pin = Pin(
            pin_num,
            Pin.IN,
            Pin.PULL_UP
        )

        self.factor_kmh = factor_kmh
        self.debounce_ms = debounce_ms

        self.pulse_count = 0

        self.last_pulse_time = (
            time.ticks_ms()
        )

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
            pulses
            *
            self.factor_kmh
        )

    def read_speed(self):

        return round(
            self.current_speed_kmh,
            2
        )

    def deinit(self):

        self.timer.deinit()


# ==============================================================================
# 7. Rain Gauge Driver (UART Modbus)
# ==============================================================================

class RainGaugeSensor:

    def __init__(
        self,
        uart_id=2,
        baudrate=9600,
        tx_pin=17,
        rx_pin=16,
        slave_addr=0xC0,
        mm_per_tip=0.28
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

        for pos in data:

            crc ^= pos

            for _ in range(8):

                if (crc & 1) != 0:

                    crc >>= 1

                    crc ^= 0xA001

                else:

                    crc >>= 1

        return crc

    def read_raw_tips(self):

        req = bytearray(
            [
                self.slave_addr,
                0x04,
                0x00,
                0x0A,
                0x00,
                0x02
            ]
        )

        crc = self._modbus_crc(req)

        req.append(
            crc & 0xFF
        )

        req.append(
            (crc >> 8) & 0xFF
        )

        while self.uart.any():

            self.uart.read()

        self.uart.write(req)

        time.sleep(0.2)

        if self.uart.any():

            resp = self.uart.read(9)

            if resp and len(resp) == 9:

                resp_crc = (
                    resp[-2]
                    |
                    (
                        resp[-1]
                        << 8
                    )
                )

                if (
                    resp_crc
                    ==
                    self._modbus_crc(
                        resp[:-2]
                    )
                ):

                    return (
                        (resp[5] << 24)
                        |
                        (resp[6] << 16)
                        |
                        (resp[3] << 8)
                        |
                        resp[4]
                    )

        return None

    def init_baseline(self):

        for _ in range(5):

            self.last_tip_count = (
                self.read_raw_tips()
            )

            if (
                self.last_tip_count
                is not None
            ):

                print(
                    f"Rain Gauge initial tip count: "
                    f"{self.last_tip_count}"
                )

                return True

            time.sleep(1)

        print(
            "Warning: Rain Gauge not responding."
        )

        return False

    def read_interval_rainfall(self):

        current_tips = (
            self.read_raw_tips()
        )

        interval_rain_mm = 0.0

        if (
            current_tips is not None
            and
            self.last_tip_count is not None
        ):

            if (
                current_tips
                <
                self.last_tip_count
            ):

                self.last_tip_count = (
                    current_tips
                )

            interval_rain_mm = (
                current_tips
                -
                self.last_tip_count
            ) * self.mm_per_tip

            self.last_tip_count = (
                current_tips
            )

        return round(
            interval_rain_mm,
            2
        )


# ==============================================================================
# 8. Main Application Orchestration
# ==============================================================================

class WeatherStation:

    def __init__(self, cfg):

        self.cfg = cfg

        # ------------------------------------------------------------------
        # Cloud
        # ------------------------------------------------------------------

        self.cloud = CloudClient(
            cfg["wifi"],
            cfg["supabase"]
        )

        # ------------------------------------------------------------------
        # OTA
        # ------------------------------------------------------------------

        self.ota = GitHubOTA(
            cfg["ota"]["repo_api"],
            cfg["ota"]["branch"],
            cfg["ota"]["github_token"],
            cfg["ota"]["check_interval_s"]
        )

        # ------------------------------------------------------------------
        # SHT45
        # ------------------------------------------------------------------

        self.sht45 = SHT45Sensor(
            sda_pin=cfg["sht45"]["sda"],
            scl_pin=cfg["sht45"]["scl"],
            freq=cfg["sht45"]["freq"],
            timeout=cfg["sht45"]["timeout"],
            default_addr=cfg["sht45"]["default_addr"]
        )

        # ------------------------------------------------------------------
        # Wind Direction
        # ------------------------------------------------------------------

        self.wind_dir = WindDirectionSensor(
            pin_num=cfg["wind_direction"]["pin"],
            adc_min=cfg["wind_direction"]["adc_min"],
            adc_max=cfg["wind_direction"]["adc_max"]
        )

        # ------------------------------------------------------------------
        # Wind Speed
        # ------------------------------------------------------------------

        self.wind_spd = WindSpeedSensor(
            pin_num=cfg["wind_speed"]["pin"],
            factor_kmh=cfg["wind_speed"]["factor_kmh"],
            debounce_ms=cfg["wind_speed"]["debounce_ms"]
        )

        # ------------------------------------------------------------------
        # Rain Gauge
        # ------------------------------------------------------------------

        self.rain = RainGaugeSensor(
            uart_id=cfg["rain_gauge"]["uart_id"],
            baudrate=cfg["rain_gauge"]["baudrate"],
            tx_pin=cfg["rain_gauge"]["tx_pin"],
            rx_pin=cfg["rain_gauge"]["rx_pin"],
            slave_addr=cfg["rain_gauge"]["slave_addr"],
            mm_per_tip=cfg["rain_gauge"]["mm_per_tip"]
        )

    def setup(self):

        print(
            "Initializing Modular Weather Station..."
        )

        self.cloud.connect_wifi()

        self.rain.init_baseline()

        print(
            "Setup complete. Starting telemetry loop...\n"
            +
            "-" * 60
        )

    def run(self):

        self.setup()

        try:

            while True:

                # ==========================================================
                # 1. Maintain Wi-Fi Connection
                # ==========================================================

                if not self.cloud.is_connected():

                    self.cloud.connect_wifi()

                # ==========================================================
                # 2. Check OTA
                # ==========================================================

                self.ota.check()

                # ==========================================================
                # 3. Read Sensors
                # ==========================================================

                direction = (
                    self.wind_dir.read_direction()
                )

                speed_kmh = (
                    self.wind_spd.read_speed()
                )

                temp, hum = (
                    self.sht45.read()
                )

                rain_mm = (
                    self.rain.read_interval_rainfall()
                )

                # ==========================================================
                # 4. Print Telemetry
                # ==========================================================

                print(
                    f"Dir: {direction:10} | "
                    f"Speed: {speed_kmh:5.2f} km/h | "
                    f"Rain: {rain_mm:5.2f} mm | "
                    f"Temp: {str(temp):>5}°C | "
                    f"Hum: {str(hum):>5}%"
                )

                # ==========================================================
                # 5. Push to Supabase
                # ==========================================================

                self.cloud.push(
                    direction,
                    speed_kmh,
                    rain_mm,
                    temp,
                    hum
                )

                # ==========================================================
                # 6. Delay
                # ==========================================================

                time.sleep(
                    self.cfg["loop_delay_s"]
                )

        except KeyboardInterrupt:

            print(
                "\nStation stopped by user."
            )

            self.wind_spd.deinit()


# ==============================================================================
# 9. Program Entry Point
# ==============================================================================

if __name__ == "__main__":

    station = WeatherStation(
        CONFIG
    )

    station.run()
