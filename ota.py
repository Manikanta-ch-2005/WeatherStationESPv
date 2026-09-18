import urequests as requests
import json
import machine
import os
import time

VERSION_FILE = "version.json"

class OTAEngine:
    def __init__(self, raw_base_url):
        # Format: "https://raw.githubusercontent.com/<USER>/<REPO>/<BRANCH>"
        self.base_url = raw_base_url.rstrip("/")

    def get_local_version(self):
        try:
            with open(VERSION_FILE, "r") as f:
                return json.load(f).get("version", "0.0.0")
        except (OSError, ValueError):
            return "0.0.0"

    def check_and_update(self, files=["main.py"]):
        try:
            v_url = f"{self.base_url}/version.json"
            print(f"[OTA] Checking for updates at: {v_url}")
            res = requests.get(v_url)
            
            if res.status_code != 200:
                print(f"[OTA] Version check failed with HTTP {res.status_code}")
                res.close()
                return False
            
            remote_ver = res.json().get("version")
            res.close()
            local_ver = self.get_local_version()

            if remote_ver == local_ver or not remote_ver:
                print(f"[OTA] Device is up-to-date (v{local_ver}).")
                return False

            print(f"[OTA] Update found: v{local_ver} -> v{remote_ver}. Downloading files...")
            
            # Download new files to a temporary buffer first to prevent file corruption
            for fname in files:
                f_url = f"{self.base_url}/{fname}"
                print(f"[OTA] Fetching {f_url}...")
                res = requests.get(f_url)
                
                if res.status_code == 200:
                    with open(fname + ".tmp", "w") as f:
                        f.write(res.text)
                    res.close()
                    
                    # Atomic file swap
                    try:
                        os.remove(fname)
                    except OSError:
                        pass
                    os.rename(fname + ".tmp", fname)
                    print(f"[OTA] Replaced {fname} successfully.")
                else:
                    res.close()
                    print(f"[OTA] Error downloading {fname} (HTTP {res.status_code}). Aborting.")
                    return False

            # Update the local version record
            with open(VERSION_FILE, "w") as f:
                json.dump({"version": remote_ver}, f)

            print("[OTA] Update successfully installed! Rebooting ESP32...")
            time.sleep(2)
            machine.reset()
            return True

        except Exception as e:
            print(f"[OTA] Engine exception: {e}")
            return False