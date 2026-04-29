"""
Pre-Launch Scout Bot — separate Telegram agent.

Monitors Twitter/X, Telegram channels, and Discord for upcoming
token launch announcements BEFORE they go live on pump.fun or DEX.

Requires SCOUT_BOT_TOKEN in .env (create via @BotFather).
Optional: TWITTER_BEARER_TOKEN for Twitter/X scanning.

Run independently:
    python -m app.scout_bot
"""
import os
import time
import threading
import requests
from pathlib import Path
from dotenv import load_dotenv
from app.launch_scout import (
    init_scout_tables,
    scan_all_sources,
    get_pending_alerts,
    mark_alerted,
    format_announcement,
    format_upcoming_list,
    TELEGRAM_CHANNELS,
)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCOUT_TOKEN = os.getenv("SCOUT_BOT_TOKEN", "")
if not SCOUT_TOKEN:
    raise RuntimeError(
        "SCOUT_BOT_TOKEN not set in .env\n"
        "Create a new bot via @BotFather and add:\n"
        "SCOUT_BOT_TOKEN=<token>"
    )

BASE   = f"https://api.telegram.org/bot{SCOUT_TOKEN}"
DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

SCAN_INTERVAL = 300   # scan sources every 5 minutes
offset        = None

import sqlite3

# ── User store ─────────────────────────────────────────────────────────────

def _subscribe(chat_id: int):
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute(
        "INSERT OR REPLACE INTO scout_users (chat_id, subscribed) VALUES (?, 1)",
        (chat_id,)
    )
    con.commit()
    con.close()


def _unsubscribe(chat_id: int):
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute(
        "UPDATE scout_users SET subscribed=0 WHERE chat_id=?", (chat_id,)
    )
    con.commit()
    con.close()


def _get_subscribers() -> list:
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("SELECT chat_id FROM scout_users WHERE subscribed=1")
    rows = cur.fetchall()
    con.close()
    return [r[0] for r in rows]


# ── Telegram helpers ───────────────────────────────────────────────────────

def send_msg(chat_id: int, text: str):
    try:
        requests.post(
            f"{BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[SCOUT BOT] Send error: {e}")


def broadcast(text: str):
    for uid in _get_subscribers():
        try:
            send_msg(uid, text)
            time.sleep(0.3)
        except Exception:
            pass


# ── Background scanner ─────────────────────────────────────────────────────

def scout_loop():
    """Runs every 5 minutes: scans sources → broadcasts new alerts."""
    time.sleep(30)   # wait for bot to start
    while True:
        try:
            print("[SCOUT] Scanning all sources...")
            scan_all_sources()   # saves new entries to DB

            pending = get_pending_alerts()
            print(f"[SCOUT] {len(pending)} new announcement(s) to send")

            for ann in pending:
                msg = format_announcement(ann)
                broadcast(msg)
                mark_alerted(ann["id"])
                time.sleep(1)

        except Exception as e:
            print(f"[SCOUT] Loop error: {e}")

        time.sleep(SCAN_INTERVAL)


# ── Commands ───────────────────────────────────────────────────────────────

def help_text() -> str:
    return (
        "Pre-Launch Scout Bot\n\n"
        "I scan Twitter/X, Telegram & Discord every 5 minutes\n"
        "looking for upcoming token launches BEFORE they go live.\n\n"
        "Commands:\n"
        "/sub — subscribe to pre-launch alerts\n"
        "/unsub — stop alerts\n"
        "/upcoming — show recent launch announcements\n"
        "/sources — list monitored channels & keywords\n"
        "/help — this message\n\n"
        "I look for:\n"
        "  'launching in X hours'\n"
        "  'fair launch', 'stealth launch'\n"
        "  pump.fun links\n"
        "  Solana contract addresses\n\n"
        "DYOR — all announcements are unverified."
    )


def sources_text() -> str:
    tg_list = "\n".join(f"  @{ch}" for ch in TELEGRAM_CHANNELS)
    return (
        "Monitored Sources\n\n"
        "𝕏 Twitter/X\n"
        "  Keywords: pump.fun, fair launch, stealth launch,\n"
        "  launching in, new token, solana launch\n\n"
        "✈️ Telegram Channels:\n"
        f"{tg_list}\n\n"
        " Discord\n"
        "  (coming soon — add servers via /adddiscord)\n\n"
        "Scan interval: every 5 minutes"
    )


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    global offset
    print("[SCOUT BOT] Running...")
    print(f"[SCOUT BOT] Monitoring {len(TELEGRAM_CHANNELS)} Telegram channels")

    while True:
        try:
            params = {"timeout": 30}
            if offset:
                params["offset"] = offset

            res  = requests.get(f"{BASE}/getUpdates", params=params, timeout=35)
            data = res.json()

            if not data.get("ok"):
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                msg = upd.get("message")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                text    = msg.get("text", "").strip()

                print(f"[SCOUT BOT] Message: {text}")

                if text in ("/start", "/sub", "/subscribe"):
                    _subscribe(chat_id)
                    send_msg(chat_id,
                        "Subscribed to pre-launch alerts!\n\n"
                        "You'll get notified when I find upcoming launches\n"
                        "on Twitter/X, Telegram, and Discord.\n\n"
                        + help_text()
                    )

                elif text in ("/unsub", "/unsubscribe"):
                    _unsubscribe(chat_id)
                    send_msg(chat_id, "Unsubscribed. Use /sub to re-enable alerts.")

                elif text == "/upcoming":
                    send_msg(chat_id, format_upcoming_list())

                elif text == "/sources":
                    send_msg(chat_id, sources_text())

                elif text == "/help":
                    send_msg(chat_id, help_text())

                else:
                    send_msg(chat_id,
                        f"Unknown command: {text}\n"
                        "Use /help to see all commands."
                    )

        except Exception as e:
            print(f"[SCOUT BOT] Error: {e}")
            time.sleep(2)


if __name__ == "__main__":
    init_scout_tables()
    threading.Thread(target=scout_loop, daemon=True).start()
    main()
