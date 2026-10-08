# This file is executed on every boot (including wake-boot from deepsleep)
#import esp
#esp.osdebug(None)
#import webrepl
#webrepl.start()

import json
from ota import OTAUpdater

try:
	with open("config.json", "r") as config_file:
		config = json.load(config_file)
	OTAUpdater(config).check_for_updates()
except Exception as exc:
	print("[OTA] Boot check failed:", exc)
