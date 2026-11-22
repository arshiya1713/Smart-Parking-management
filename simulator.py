import random
import time
import requests

URL = "http://127.0.0.1:5000/api/sensor"
API_KEY = "sensor-dev-key"   # must match SENSOR_KEY in app.py
SLOTS = [f"P{i}" for i in range(1, 11)]

print("Sensor simulator running. Press Ctrl+C to stop.")

while True:
    slot = random.choice(SLOTS)
    status = random.choice(["available", "occupied"])
    try:
        r = requests.post(
            URL,
            json={"slot": slot, "status": status},
            headers={"X-API-Key": API_KEY},
            timeout=5,
        )
        print(slot, "->", status, "|", r.status_code, r.json().get("message") or r.json().get("error"))
    except Exception as e:
        print("Error:", e)
    time.sleep(random.randint(10, 20))