import os
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

from app.user_store import all_users

env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

BASE = f"https://api.telegram.org/bot{TOKEN}"


def send_message(chat_id: int, text: str) -> None:
    requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=20,
    )


def format_alert(results: list) -> str:
    if not results:
        return ""

    text = "🚨 New Coin Alert\n\n"
    for i, c in enumerate(results[:3], 1):
        text += (
            f"{i}. {c['name']} ({c['symbol'].upper()})\n"
            f"score: {c['final_score']:.3f}\n"
            f"risk: {c['risk_level']} | bucket: {c['bucket']}\n\n"
        )
    return text.strip()


def get_candidates():
    data = requests.get(f"{API_URL}/top-overall", timeout=20).json()
    return data.get("results", [])


print("ALERT WORKER RUNNING...")

while True:
    try:
        users = all_users()
        candidates = get_candidates()

        for chat_id, settings in users.items():
            if not settings.get("alerts_enabled", True):
                continue

            min_score = float(settings.get("min_score", 0.55))
            favorite_bucket = settings.get("favorite_bucket", "any")

            filtered = []
            for c in candidates:
                if c.get("final_score", 0) < min_score:
                    continue
                if favorite_bucket != "any" and c.get("bucket") != favorite_bucket:
                    continue
                filtered.append(c)

            if filtered:
                msg = format_alert(filtered)
                if msg:
                    send_message(int(chat_id), msg)

        time.sleep(3600)  # هر 1 ساعت

    except Exception as e:
        print("ALERT WORKER ERROR:", e)
        time.sleep(30)