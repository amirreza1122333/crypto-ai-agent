import time
import sqlite3
import requests

from pathlib import Path
from app.config import API_BASE_URL

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"
API_URL = API_BASE_URL


EVALUATION_DELAY = 3600  # 1 ساعت


def get_open_alerts():
    conn = sqlite3.connect(DB_PATH, timeout=5)
    c = conn.cursor()

    c.execute("""
        SELECT id, symbol, entry_price, created_ts
        FROM alert_results
        WHERE status='open'
    """)

    rows = c.fetchall()
    conn.close()

    return rows


def fetch_current_price(symbol):
    try:
        r = requests.get(f"{API_URL}/scan", timeout=30)
        data = r.json().get("results", [])

        for c in data:
            if str(c.get("symbol", "")).upper() == symbol.upper():
                return float(c.get("current_price", 0) or 0)
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"[WARN] fetch_current_price failed for {symbol}: {e}")
        return None

    return None


def evaluate_alerts():
    alerts = get_open_alerts()
    now = int(time.time())

    for alert_id, symbol, entry_price, created_ts in alerts:
        if now - created_ts < EVALUATION_DELAY:
            continue

        current_price = fetch_current_price(symbol)
        if not current_price or entry_price == 0:
            continue

        return_pct = (current_price - entry_price) / entry_price
        is_win = 1 if return_pct > 0 else 0

        conn = sqlite3.connect(DB_PATH, timeout=5)
        c = conn.cursor()

        c.execute("""
            UPDATE alert_results
            SET
                exit_price=?,
                return_pct=?,
                is_win=?,
                evaluated_ts=?,
                status='closed'
            WHERE id=?
        """, (
            current_price,
            return_pct,
            is_win,
            now,
            alert_id
        ))

        conn.commit()
        conn.close()

        print(f"Evaluated {symbol}: {return_pct:.2%}")


def loop():
    while True:
        try:
            evaluate_alerts()
        except Exception as e:
            print("Evaluation error:", e)

        time.sleep(300)


if __name__ == "__main__":
    loop()