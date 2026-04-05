import os
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BASE = f"https://api.telegram.org/bot{TOKEN}"

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN missing")

print("TOKEN LOADED =", True)

offset = None

while True:
    try:
        params = {"timeout": 30}
        if offset is not None:
            params["offset"] = offset

        r = requests.get(f"{BASE}/getUpdates", params=params, timeout=35)
        data = r.json()

        if not data.get("ok"):
            print("Telegram API error:", data)
            time.sleep(2)
            continue

        results = data.get("result", [])
        for upd in results:
            print("UPDATE:", upd)
            offset = upd["update_id"] + 1

            msg = upd.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")

            if text == "/start":
                reply = "start ok"
            elif text == "/overall":
                reply = "overall ok"
            else:
                reply = f"echo: {text}"

            requests.post(
                f"{BASE}/sendMessage",
                json={"chat_id": chat_id, "text": reply},
                timeout=20,
            )

    except Exception as e:
        print("ERROR:", e)
        time.sleep(2)