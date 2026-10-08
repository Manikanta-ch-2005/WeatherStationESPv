import gc
import machine
import network
import os
import time
import urequests


class OTAUpdater:
    MIN_HEAP = 80000

    def __init__(self, config):
        self.config = config
        self.repo_api = config.get("ota_repo_api", "").rstrip("/")
        self.branch = config.get("ota_branch", "main")
        self.token = config.get("ota_token", "")

    def _headers(self):
        headers = {
            "User-Agent": "ESP32-MicroPython-OTA",
            "Accept": "application/vnd.github.raw+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Connection": "close"
        }
        if self.token and self.token != "YOUR_GITHUB_TOKEN":
            headers["Authorization"] = "token " + self.token
        return headers

    def _url(self, filename):
        return "{}/{}?ref={}".format(
            self.repo_api,
            filename,
            self.branch
        )

    def _remove(self, filename):
        try:
            os.remove(filename)
        except OSError:
            pass

    def _fetch_version(self):
        response = urequests.get(
            self._url("version.txt"),
            headers=self._headers()
        )
        try:
            if response.status_code != 200:
                raise OSError("GitHub version HTTP {}".format(response.status_code))
            return response.text.strip()
        finally:
            response.close()

    def _download_main(self, wdt=None):
        response = urequests.get(
            self._url("main.py"),
            headers=self._headers()
        )
        total = 0
        try:
            if response.status_code != 200:
                raise OSError("GitHub main.py HTTP {}".format(response.status_code))
            with open("main.new.py", "wb") as firmware_file:
                while True:
                    if wdt is not None:
                        wdt.feed()
                    chunk = response.raw.read(512)
                    if not chunk:
                        break
                    firmware_file.write(chunk)
                    total += len(chunk)
                    del chunk
        finally:
            response.close()
            gc.collect()
        return total

    def _install(self, remote_version):
        self._remove("main.bak.py")
        try:
            os.rename("main.py", "main.bak.py")
        except OSError:
            pass

        try:
            os.rename("main.new.py", "main.py")
        except Exception:
            try:
                os.rename("main.bak.py", "main.py")
            except OSError:
                pass
            raise

        with open("version.txt", "w") as version_file:
            version_file.write(remote_version)

        self._remove("main.bak.py")

    def check_for_updates(self, wdt=None):
        if "api.github.com/repos/" not in self.repo_api or "/contents" not in self.repo_api:
            print("[OTA] Invalid GitHub repository API URL; skipping.")
            return

        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        if not wlan.isconnected():
            ssid = self.config.get("wifi_ssid", "")
            password = self.config.get("wifi_pass", "")
            if not ssid:
                print("[OTA] Wi-Fi SSID missing; skipping.")
                return
            wlan.connect(ssid, password)
            timeout = 15
            while not wlan.isconnected() and timeout > 0:
                time.sleep(1)
                timeout -= 1

        if not wlan.isconnected():
            print("[OTA] Wi-Fi unavailable; skipping.")
            wlan.active(False)
            return

        gc.collect()
        free_heap = gc.mem_free()
        if free_heap < self.MIN_HEAP:
            print("[OTA] Low heap ({} bytes); skipping.".format(free_heap))
            return

        try:
            with open("version.txt", "r") as version_file:
                local_version = version_file.read().strip()
        except OSError:
            local_version = "0"

        print("[OTA] Checking GitHub ({} bytes free)...".format(free_heap))
        try:
            if wdt is not None:
                wdt.feed()
            remote_version = self._fetch_version()
            if wdt is not None:
                wdt.feed()
            if not remote_version or remote_version == local_version:
                print("[OTA] Firmware is up to date.")
                return

            gc.collect()
            self._remove("main.new.py")
            downloaded = self._download_main(wdt)
            if downloaded <= 0:
                raise OSError("Downloaded main.py is empty")

            self._install(remote_version)
            print("[OTA] Installed version {}; rebooting.".format(remote_version))
            time.sleep(1)
            machine.reset()
        except Exception as exc:
            self._remove("main.new.py")
            gc.collect()
            print("[OTA] Check failed:", exc)