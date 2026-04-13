import os
import time
import sqlite3
import threading
import requests
from pathlib import Path
from dotenv import load_dotenv
from app.paper_trader import (
    ensure_paper_account,
    open_position,
    close_position,
    get_closed_stats,
)
from app.paper_trading_service import build_position_snapshot, fetch_market_results, find_coin
from app.user_store import ensure_user, update_user, all_users
from app.dex_scanner import scan_new_gems, GEM_ALERT_SCORE

# -----------------------------
# Load ENV
# -----------------------------
env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(env_path, override=True)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found in .env")

BASE = f"https://api.telegram.org/bot{TOKEN}"

print("BOT RUNNING...")
print("API:", API_URL)

offset = None

# -----------------------------
# Local DB
# -----------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

# هر چند ثانیه watchlist alert چک شود
ALERT_POLL_SECONDS = 300
ALERT_COOLDOWN_SECONDS = 1800
GEM_POLL_SECONDS = 300          # scan for new gems every 5 min
GEM_COOLDOWN_SECONDS = 3600     # don't re-alert same gem within 1 hour

# In-memory set of alerted gem token addresses (address:chain)
_alerted_gems: set = set()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS watchlist (
        chat_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        UNIQUE(chat_id, symbol)
    )
    """)

    # 🔥 state پیشرفته
    c.execute("""
    CREATE TABLE IF NOT EXISTS alert_state (
        chat_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        last_alert_ts INTEGER NOT NULL,
        last_prob REAL DEFAULT 0,
        last_signal TEXT DEFAULT '',
        UNIQUE(chat_id, symbol)
    )
    """)


    c.execute("""
CREATE TABLE IF NOT EXISTS alert_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    symbol TEXT,
    entry_price REAL,
    exit_price REAL,
    return_pct REAL,
    is_win INTEGER,
    created_ts INTEGER,
    evaluated_ts INTEGER,
    status TEXT
)
""")



    c.execute("""
CREATE TABLE IF NOT EXISTS paper_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    entry_score REAL,
    entry_prob REAL,
    entry_signal TEXT,
    entry_ts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    exit_ts INTEGER,
    pnl REAL,
    return_pct REAL
)
""")

    c.execute("""
CREATE TABLE IF NOT EXISTS paper_account (
    chat_id INTEGER PRIMARY KEY,
    balance REAL NOT NULL DEFAULT 10000,
    equity REAL NOT NULL DEFAULT 10000
)
""")
    c.execute("""
CREATE TABLE IF NOT EXISTS gem_alerted (
    token_key TEXT PRIMARY KEY,
    alerted_ts INTEGER NOT NULL
)
""")

    conn.commit()
    conn.close()


# -----------------------------
# Watchlist helpers
# -----------------------------
def add_to_watchlist(chat_id: int, symbol: str) -> bool:
    symbol = symbol.upper().strip()
    if not symbol:
        return False

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    try:
        c.execute(
            "INSERT INTO watchlist (chat_id, symbol) VALUES (?, ?)",
            (chat_id, symbol)
        )
        conn.commit()
        ok = True
    except sqlite3.IntegrityError:
        ok = False
    finally:
        conn.close()

    return ok

def paper_positions_text(chat_id: int) -> str:
    positions = build_position_snapshot(chat_id)
    if not positions:
        return "📄 No open paper positions."

    text = "📄 Open Paper Positions\n\n"
    for i, p in enumerate(positions, 1):
        text += (
            f"{i}. {p['name']} ({p['symbol']})\n"
            f"Entry: {p['entry_price']:.4f}\n"
            f"Now: {p['current_price']:.4f}\n"
            f"Qty: {p['quantity']}\n"
            f"Signal: {p['current_signal']}\n"
            f"AI: {p['current_prob']:.2f}\n"
            f"PnL: {p['pnl']:.4f} | Return: {p['return_pct']:.2%}\n\n"
        )
    return text.strip()





def remove_from_watchlist(chat_id: int, symbol: str) -> bool:
    symbol = symbol.upper().strip()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "DELETE FROM watchlist WHERE chat_id=? AND symbol=?",
        (chat_id, symbol)
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()

    return deleted

def paper_stats_text(chat_id: int) -> str:
    s = get_closed_stats(chat_id)
    return (
        "📊 Paper Trading Stats\n\n"
        f"Balance: {s['balance']:.2f}\n"
        f"Total Trades: {s['total_trades']}\n"
        f"Wins: {s['wins']}\n"
        f"Winrate: {s['winrate']:.2%}\n"
        f"Avg Return: {s['avg_return']:.2%}\n"
        f"Total PnL: {s['total_pnl']:.2f}"
    )


def get_watchlist(chat_id: int) -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "SELECT symbol FROM watchlist WHERE chat_id=? ORDER BY symbol ASC",
        (chat_id,)
    )
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_all_watchlist_users() -> list[int]:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT DISTINCT chat_id FROM watchlist")
    rows = c.fetchall()
    conn.close()
    return [int(r[0]) for r in rows]


def watchlist_text(chat_id: int) -> str:
    items = get_watchlist(chat_id)
    if not items:
        return "⭐ Watchlist is empty.\n\nUse:\n/watch BTC"

    return "⭐ Your Watchlist\n\n" + "\n".join(f"- {x}" for x in items)


# -----------------------------
# Alert cooldown helpers
# -----------------------------
def should_send_alert_advanced(chat_id: int, coin: dict) -> bool:
    now_ts = int(time.time())
    symbol = str(coin.get("symbol", "")).upper()

    prob = float(coin.get("pump_probability_6h", 0) or 0)
    signal = str(coin.get("ai_signal", ""))

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        SELECT last_alert_ts, last_prob, last_signal
        FROM alert_state
        WHERE chat_id=? AND symbol=?
    """, (chat_id, symbol))

    row = c.fetchone()

    # اگر اولین بار است → ارسال کن
    if row is None:
        conn.close()
        return True

    last_ts, last_prob, last_signal = row
    last_ts = int(last_ts)
    last_prob = float(last_prob or 0)

    # ⛔ cooldown
    if now_ts - last_ts < ALERT_COOLDOWN_SECONDS:
        conn.close()
        return False

    # 🔥 تغییر واقعی:
    prob_jump = prob - last_prob

    # شرط ارسال:
    if (
        prob >= 0.70 and prob_jump >= 0.08  # جهش واقعی
        or signal != last_signal            # تغییر سیگنال
    ):
        conn.close()
        return True

    conn.close()
    return False


def mark_alert_sent_advanced(chat_id: int, coin: dict):
    symbol = str(coin.get("symbol", "")).upper()
    prob = float(coin.get("pump_probability_6h", 0) or 0)
    signal = str(coin.get("ai_signal", ""))

    now_ts = int(time.time())

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        INSERT INTO alert_state (chat_id, symbol, last_alert_ts, last_prob, last_signal)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, symbol)
        DO UPDATE SET
            last_alert_ts=excluded.last_alert_ts,
            last_prob=excluded.last_prob,
            last_signal=excluded.last_signal
    """, (chat_id, symbol, now_ts, prob, signal))

    conn.commit()
    conn.close()


# -----------------------------
# Telegram send helpers
# -----------------------------
def send_message(chat_id: int, text: str) -> None:
    max_len = 3500
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]

    for chunk in chunks:
        res = requests.post(
            f"{BASE}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
            },
            timeout=20,
        )
        try:
            print("SEND STATUS:", res.status_code, res.json())
        except Exception:
            print("SEND STATUS:", res.status_code, res.text)


def send_menu(chat_id: int) -> None:
    keyboard = {
        "keyboard": [
            [{"text": "/overall"}, {"text": "/momentum"}],
            [{"text": "/safer"}, {"text": "/scan"}],
            [{"text": "/alerts"}, {"text": "/watchlist"}],
            [{"text": "/refresh"}, {"text": "/settings"}],
            [{"text": "/alerts_on"}, {"text": "/alerts_off"}],
            [{"text": "/help"}],
            [{"text": "/paper_positions"}, {"text": "/paper_stats"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

    requests.post(
        f"{BASE}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "👇 منوی بات آماده است",
            "reply_markup": keyboard,
        },
        timeout=20,
    )


# -----------------------------
# Formatting helpers
# -----------------------------
def risk_emoji(risk: str) -> str:
    return {"Low": "🟢", "Medium": "🟡", "High": "🔴"}.get(risk, "⚪")


def trend_emoji(trend: str) -> str:
    return {
        "Bullish": "🚀",
        "Positive": "📈",
        "Neutral": "➖",
        "Weak": "📉",
    }.get(trend, "•")


def signal_emoji(signal_type: str) -> str:
    return {
        "Strong Consider": "🔥",
        "Breakout Watch": "⚡",
        "Momentum Watch": "👀",
        "Risky Pump": "🎲",
        "Avoid": "⛔",
    }.get(signal_type, "•")


def format_top(title: str, data: dict) -> str:
    results = data.get("results", [])
    if not results:
        return f"{title}\nNo data found."
    

    

    text = f"{title}\n\n"

    for i, c in enumerate(results[:10], 1):
        risk = c.get("risk_level", "-")
        trend = c.get("trend", "-")
        action = c.get("action", "-")
        confidence = c.get("confidence", 0)
        signal_type = c.get("signal_type", "-")
        prob = c.get("pump_probability_6h", 0)
        ai = c.get("ai_signal", "-")

        text += (
            f"{i}. {c.get('name', '-')} ({str(c.get('symbol', '-')).upper()})\n"
            f"Score: {c.get('final_score', 0):.3f}\n"
            f"{trend_emoji(trend)} {trend} | {risk_emoji(risk)} {risk}\n"
           f"{signal_emoji(signal_type)} {signal_type}\n"
           f"🤖 AI: {prob:.2f} | {ai}\n"
           f"Action: {action} | Confidence: {confidence}%\n\n"
        )

    return text.strip()


def format_scan(data: dict, limit: int = 6) -> str:
    results = data.get("results", [])
    if not results:
        return "🔎 Scan Results\nNo scan data found."

    text = "🔎 Scan Results\n\n"

    for i, c in enumerate(results[:limit], 1):
        risk = c.get("risk_level", "-")
        trend = c.get("trend", "-")
        action = c.get("action", "-")
        confidence = c.get("confidence", 0)
        signal_type = c.get("signal_type", "-")
        reason = c.get("reason", "-")
        prob = c.get("pump_probability_6h", 0)
        ai = c.get("ai_signal", "-")

        text += (
            f"{i}. {c.get('name', '-')} ({str(c.get('symbol', '-')).upper()})\n"
            f"Score: {c.get('final_score', 0):.3f}\n"
            f"{trend_emoji(trend)} {trend} | {risk_emoji(risk)} {risk}\n"
            f"{signal_emoji(signal_type)} {signal_type}\n"
            f"🤖 AI: {prob:.2f} | {ai}\n"
            f"Action: {action} | Confidence: {confidence}%\n"
            f"🧠 {reason}\n\n"
        )

    return text.strip()


def format_alerts(data: dict, limit: int = 5) -> str:
    results = data.get("results", [])
    if not results:
        return "🚨 Alert Candidates\nNo alert candidates found."

    text = "🚨 Alert Candidates\n\n"

    for i, c in enumerate(results[:limit], 1):
        risk = c.get("risk_level", "-")
        trend = c.get("trend", "-")
        confidence = c.get("confidence", 0)
        signal_type = c.get("signal_type", "-")
        prob = c.get("pump_probability_6h", 0)
        ai = c.get("ai_signal", "-")

        text += (
            f"{i}. {c.get('name', '-')} ({str(c.get('symbol', '-')).upper()})\n"
            f"{signal_emoji(signal_type)} {signal_type}\n"
            f"🤖 AI: {prob:.2f} | {ai}\n"
            f"{trend_emoji(trend)} {trend} | {risk_emoji(risk)} {risk}\n"
            f"Confidence: {confidence}%\n\n"
        )

    return text.strip()


def help_text() -> str:
    return (
        "🤖 Crypto Bot Help\n\n"
        "/start - start bot\n"
        "/overall - top overall coins\n"
        "/momentum - top momentum coins\n"
        "/safer - safer coins\n"
        "/scan - mixed signal scan\n"
        "/alerts - top alert candidates\n"
        "/watch BTC - add symbol to watchlist\n"
        "/unwatch BTC - remove symbol from watchlist\n"
        "/watchlist - show your watchlist\n"
        "/refresh - refresh cache\n"
        "/settings - your current settings\n"
        "/alerts_on - enable alerts\n"
        "/alerts_off - disable alerts\n"
        "/setscore 0.60 - set minimum score for alerts\n"
        "/paper_open TAO 10 - open paper position\n"
        "/paper_close TAO - close paper position\n"
        "/paper_positions - show open paper trades\n"
        "/paper_stats - show paper trading stats\n"
        "/newgems - scan for new gem launches\n"
        "/gems_on - enable gem alerts\n"
        "/gems_off - disable gem alerts\n"
    )


def settings_text(chat_id: int) -> str:
    settings = ensure_user(chat_id)
    return (
        "⚙️ Your Settings\n\n"
        f"alerts_enabled: {settings.get('alerts_enabled')}\n"
        f"gems_enabled: {settings.get('gems_enabled', True)}\n"
        f"min_score: {settings.get('min_score')}\n"
        f"scan_limit: {settings.get('scan_limit')}\n"
        f"favorite_bucket: {settings.get('favorite_bucket')}\n"
    )


def format_gems(gems: list) -> str:
    if not gems:
        return "💎 New Gem Scanner\n\nNo new gems found right now.\nTry again in a few minutes."

    text = "💎 New Gem Scanner\n\n"
    for i, g in enumerate(gems, 1):
        age = g["age_hours"]
        age_str = f"{age:.0f}h" if age < 48 else f"{age/24:.1f}d"
        fdv = g["fdv"]
        fdv_str = f"${fdv/1_000_000:.1f}M" if fdv >= 1_000_000 else f"${fdv/1_000:.0f}K"
        vol_str = f"${g['volume_24h']/1_000:.0f}K" if g['volume_24h'] < 1_000_000 else f"${g['volume_24h']/1_000_000:.1f}M"
        liq_str = f"${g['liquidity_usd']/1_000:.0f}K" if g['liquidity_usd'] < 1_000_000 else f"${g['liquidity_usd']/1_000_000:.1f}M"

        text += (
            f"{i}. {g['name']} ({g['symbol'].upper()}) [{g['chain'].upper()}]\n"
            f"Price: ${g['price_usd']:.6f}\n"
            f"24h: +{g['price_change_24h']:.1f}% | 1h: {g['price_change_1h']:+.1f}%\n"
            f"Vol: {vol_str} | Liq: {liq_str} | FDV: {fdv_str}\n"
            f"Age: {age_str} | Buys/Sells: {g['buys_24h']}/{g['sells_24h']}\n"
            f"Gem Score: {g['gem_score']}/100\n"
        )
        if g.get("url"):
            text += f"Chart: {g['url']}\n"
        text += "\n"

    text += "DYOR - high risk, new tokens can rug."
    return text.strip()


def _is_gem_alerted(token_key: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT alerted_ts FROM gem_alerted WHERE token_key=?", (token_key,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return (int(time.time()) - row[0]) < GEM_COOLDOWN_SECONDS


def _mark_gem_alerted(token_key: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO gem_alerted (token_key, alerted_ts) VALUES (?, ?)",
        (token_key, int(time.time()))
    )
    conn.commit()
    conn.close()


def gem_alert_loop():
    time.sleep(30)  # wait for bot to fully start
    while True:
        try:
            users = all_users()
            gem_users = [
                int(uid) for uid, s in users.items()
                if s.get("gems_enabled", True)
            ]

            if gem_users:
                print("[GEM] Scanning for new gems...")
                gems = scan_new_gems(max_results=5)

                for gem in gems:
                    if gem["gem_score"] < GEM_ALERT_SCORE:
                        continue

                    token_key = f"{gem['token_address']}:{gem['chain']}"
                    if _is_gem_alerted(token_key):
                        continue

                    _mark_gem_alerted(token_key)

                    age = gem["age_hours"]
                    age_str = f"{age:.0f}h" if age < 48 else f"{age/24:.1f}d"
                    msg = (
                        f"💎 NEW GEM ALERT\n\n"
                        f"{gem['name']} ({gem['symbol'].upper()}) [{gem['chain'].upper()}]\n"
                        f"Price: ${gem['price_usd']:.6f}\n"
                        f"24h: +{gem['price_change_24h']:.1f}%\n"
                        f"Vol: ${gem['volume_24h']:,.0f}\n"
                        f"Liq: ${gem['liquidity_usd']:,.0f}\n"
                        f"Age: {age_str} | Score: {gem['gem_score']}/100\n"
                    )
                    if gem.get("url"):
                        msg += f"Chart: {gem['url']}\n"
                    msg += "\nDYOR - high risk!"

                    for chat_id in gem_users:
                        try:
                            send_message(chat_id, msg)
                            time.sleep(0.3)
                        except Exception:
                            pass

        except Exception as e:
            print(f"[GEM] Loop error: {e}")

        time.sleep(GEM_POLL_SECONDS)


# -----------------------------
# API helper
# -----------------------------
def api(path: str) -> dict:
    r = requests.get(f"{API_URL}{path}", timeout=60)
    r.raise_for_status()
    return r.json()



def save_alert_entry(chat_id: int, coin: dict):
    symbol = coin.get("symbol")
    price = float(coin.get("current_price", 0) or 0)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        INSERT INTO alert_results (
            chat_id,
            symbol,
            entry_price,
            created_ts,
            status
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        chat_id,
        symbol,
        price,
        int(time.time()),
        "open"
    ))

    conn.commit()
    conn.close()


# -----------------------------
# Alert engine
# -----------------------------
def send_watchlist_alerts():
    try:
        users = get_all_watchlist_users()
        if not users:
            return

        data = api("/scan")
        results = data.get("results", [])
        if not results:
            return

        for chat_id in users:
            settings = ensure_user(chat_id)
            if not settings.get("alerts_enabled", True):
                continue

            min_score = float(settings.get("min_score", 0.60) or 0.60)
            watchlist = get_watchlist(chat_id)
            if not watchlist:
                continue

            matches = [
                c for c in results
                if str(c.get("symbol", "")).upper() in watchlist
                and float(c.get("final_score", 0) or 0) >= min_score
                and (
                       c.get("signal_type") in ["Strong Consider", "Breakout Watch"]
                       or c.get("action") == "Consider"
                       or float(c.get("pump_probability_6h", 0)) >= 0.70
                    )
            ]

            for c in matches:
                symbol = str(c.get("symbol", "")).upper()
                if not should_send_alert_advanced(chat_id, c):
                  continue

                risk = c.get("risk_level", "-")
                trend = c.get("trend", "-")
                confidence = c.get("confidence", 0)
                signal_type = c.get("signal_type", "-")
                reason = c.get("reason", "-")
                prob = c.get("pump_probability_6h", 0)
                ai = c.get("ai_signal", "-")

                msg = (
                    "🚨 WATCHLIST ALERT\n\n"
                    f"{c.get('name', '-')} ({symbol})\n"
                    f"Score: {c.get('final_score', 0):.3f}\n"
                    f"{signal_emoji(signal_type)} {signal_type}\n"
                    f"{trend_emoji(trend)} {trend} | {risk_emoji(risk)} {risk}\n"
                    f"🤖 AI: {prob:.2f} | {ai}\n"
                    f"Confidence: {confidence}%\n"
                    f"🧠 {reason}"
                )

                send_message(chat_id, msg)
                save_alert_entry(chat_id, c)
                mark_alert_sent_advanced(chat_id, c)

                
                

    except Exception as e:
        print("Alert error:", e)


def alert_loop():
    while True:
        try:
            send_watchlist_alerts()
        except Exception as e:
            print("ALERT LOOP ERROR:", e)
        time.sleep(ALERT_POLL_SECONDS)


# -----------------------------
# Main loop
# -----------------------------
def main():
    global offset

    while True:
        try:
            params = {"timeout": 30}
            if offset is not None:
                params["offset"] = offset

            res = requests.get(f"{BASE}/getUpdates", params=params, timeout=35)
            data = res.json()

            if not data.get("ok"):
                print("Telegram API error:", data)
                time.sleep(2)
                continue

            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                msg = upd.get("message")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                text = msg.get("text", "").strip()

                print("MESSAGE:", text)

                ensure_user(chat_id)
                ensure_paper_account(chat_id)

                if text == "/start":
                    send_message(
                        chat_id,
                        "🚀 Crypto AI Bot is ready\n\nUse the menu below 👇"
                    )
                    send_menu(chat_id)

                elif text == "/help":
                    send_message(chat_id, help_text())

                elif text == "/overall":
                    send_message(chat_id, format_top("🏆 Top Overall", api("/top-overall")))

                elif text == "/momentum":
                    send_message(chat_id, format_top("⚡ Top Momentum", api("/top-momentum")))

                elif text == "/safer":
                    send_message(chat_id, format_top("🛡 Top Safer", api("/top-safer")))

                elif text == "/scan":
                    user_settings = ensure_user(chat_id)
                    limit = int(user_settings.get("scan_limit", 6))
                    send_message(chat_id, format_scan(api("/scan"), limit=limit))

                elif text == "/alerts":
                    send_message(chat_id, format_alerts(api("/alerts")))
                elif text == "/watchlist":
                    send_message(chat_id, watchlist_text(chat_id))

                elif text == "/paper_positions":
                    send_message(chat_id, paper_positions_text(chat_id))

                elif text == "/paper_stats":
                    send_message(chat_id, paper_stats_text(chat_id))

                elif text.startswith("/paper_open"):
                    parts = text.split()

                    if len(parts) != 3:
                        send_message(chat_id, "Usage: /paper_open TAO 10")
                        continue

                    symbol = parts[1].upper().strip()

                    try:
                        qty = float(parts[2])
                    except ValueError:
                        send_message(chat_id, "Quantity must be numeric. Example: /paper_open TAO 10")
                        continue

                    if qty <= 0:
                        send_message(chat_id, "Quantity must be greater than 0.")
                        continue

                    results = fetch_market_results()
                    coin = find_coin(symbol, results)

                    if not coin:
                        send_message(chat_id, f"❌ Symbol not found in current scan: {symbol}")
                        continue

                    current_price = float(coin.get("current_price", 0) or 0)
                    if current_price <= 0:
                        send_message(chat_id, f"❌ Invalid current price for {symbol}")
                        continue

                    ok = open_position(
                        chat_id=chat_id,
                        symbol=symbol,
                        name=coin.get("name", symbol),
                        entry_price=current_price,
                        quantity=qty,
                        entry_score=float(coin.get("final_score", 0) or 0),
                        entry_prob=float(coin.get("pump_probability_6h", 0) or 0),
                        entry_signal=str(coin.get("signal_type", "-")),
                    )

                    if ok:
                        send_message(
                            chat_id,
                            f"✅ Paper position opened: {symbol}\n"
                            f"Qty: {qty}\n"
                            f"Entry: {current_price:.6f}"
                        )
                    else:
                        send_message(chat_id, f"ℹ️ Open paper position already exists for {symbol}")

                elif text.startswith("/paper_close"):
                    parts = text.split()

                    if len(parts) != 2:
                        send_message(chat_id, "Usage: /paper_close TAO")
                        continue

                    symbol = parts[1].upper().strip()

                    results = fetch_market_results()
                    coin = find_coin(symbol, results)

                    if not coin:
                        send_message(chat_id, f"❌ Symbol not found in current scan: {symbol}")
                        continue

                    current_price = float(coin.get("current_price", 0) or 0)
                    if current_price <= 0:
                        send_message(chat_id, f"❌ Invalid current price for {symbol}")
                        continue

                    ok = close_position(chat_id, symbol, current_price)

                    if ok:
                        send_message(
                            chat_id,
                            f"✅ Paper position closed: {symbol}\n"
                            f"Exit: {current_price:.6f}"
                        )
                    else:
                        send_message(chat_id, f"ℹ️ No open paper position found for {symbol}")

                elif text.startswith("/watch"):
                    parts = text.split(maxsplit=1)

                    if len(parts) != 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /watch BTC")
                        continue

                    symbol = parts[1].strip().upper()
                    ok = add_to_watchlist(chat_id, symbol)

                    if ok:
                        send_message(chat_id, f"⭐ Added to watchlist: {symbol}")
                    else:
                        send_message(chat_id, f"ℹ️ Already in watchlist: {symbol}")

                elif text.startswith("/unwatch"):
                    parts = text.split(maxsplit=1)

                    if len(parts) != 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /unwatch BTC")
                        continue

                    symbol = parts[1].strip().upper()
                    ok = remove_from_watchlist(chat_id, symbol)

                    if ok:
                        send_message(chat_id, f"🗑 Removed from watchlist: {symbol}")
                    else:
                        send_message(chat_id, f"ℹ️ Not found in watchlist: {symbol}")

                elif text == "/refresh":
                    r = api("/refresh")
                    send_message(
                        chat_id,
                        f"♻️ Refreshed\nrows: {r.get('rows')}\nupdated_at: {r.get('updated_at')}"
                    )

                elif text == "/settings":
                    send_message(chat_id, settings_text(chat_id))

                elif text == "/alerts_on":
                    update_user(chat_id, alerts_enabled=True)
                    send_message(chat_id, "✅ Alerts turned ON")

                elif text == "/alerts_off":
                    update_user(chat_id, alerts_enabled=False)
                    send_message(chat_id, "⛔ Alerts turned OFF")

                elif text.startswith("/setscore"):
                    parts = text.split()
                    if len(parts) != 2:
                        send_message(chat_id, "Usage: /setscore 0.60")
                    else:
                        try:
                            score = float(parts[1])
                            update_user(chat_id, min_score=score)
                            send_message(chat_id, f"✅ min_score updated to {score}")
                        except ValueError:
                            send_message(chat_id, "❌ invalid score. Example: /setscore 0.60")

                elif text == "/newgems":
                    send_message(chat_id, "Scanning for new gems... this may take 30 seconds.")
                    gems = scan_new_gems(max_results=8)
                    send_message(chat_id, format_gems(gems))

                elif text == "/gems_on":
                    update_user(chat_id, gems_enabled=True)
                    send_message(chat_id, "💎 Gem alerts turned ON")

                elif text == "/gems_off":
                    update_user(chat_id, gems_enabled=False)
                    send_message(chat_id, "Gem alerts turned OFF")

                else:
                    send_message(chat_id, f"echo: {text}")

        except Exception as e:
            print("ERROR:", e)
            time.sleep(2)


if __name__ == "__main__":
    init_db()
    threading.Thread(target=alert_loop, daemon=True).start()
    threading.Thread(target=gem_alert_loop, daemon=True).start()
    main()