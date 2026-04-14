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
from app.brain import format_brain_text, analyze_coin_brain
from app.news_scanner import format_news_text
from app.social_scanner import format_social_text
from app.whale_tracker import format_whale_text
from app.memory_store import init_memory_table, get_trending_coins
from app.fear_greed import format_fear_greed
from app.funding_rates import format_funding_text
from app.helius_enricher import enrich_gem, format_enrichment
from app.price_alerts import (
    init_price_alerts_table, add_price_alert, format_user_alerts,
    remove_price_alert, get_all_active_alerts, mark_alert_triggered,
)
from app.portfolio import init_portfolio_table, add_holding, remove_holding, get_holdings, format_portfolio
from app.prelaunch_tracker import init_prelaunch_tables, start_listener, register_callback, format_prelaunch_list

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
GEM_POLL_SECONDS        = 45    # scan for new gems every 45 seconds
GEM_COOLDOWN_SECONDS    = 3600  # don't re-alert same gem within 1 hour
PRICE_ALERT_POLL        = 300   # check price alerts every 5 min
BRAIN_ALERT_THRESHOLD   = 72    # brain score threshold for brain alerts
DAILY_REPORT_HOUR_UTC   = 8     # send daily report at 8am UTC

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
            [{"text": "/overall"}, {"text": "/momentum"}, {"text": "/safer"}],
            [{"text": "/scan"}, {"text": "/alerts"}, {"text": "/newgems"}],
            [{"text": "/feargreed"}, {"text": "/trending"}, {"text": "/report"}],
            [{"text": "/watchlist"}, {"text": "/refresh"}, {"text": "/settings"}],
            [{"text": "/paper_positions"}, {"text": "/paper_stats"}],
            [{"text": "/port"}, {"text": "/myalerts"}, {"text": "/help"}],
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False,
    }

    requests.post(
        f"{BASE}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": "Menu ready. Use commands below or type /help for full list.",
            "reply_markup": keyboard,
        },
        timeout=20,
    )


def send_inline_buttons(chat_id: int, text: str, buttons: list) -> None:
    """Send a message with inline keyboard buttons.
    buttons: list of {"text": "label", "callback_data": "data"} dicts (per row)
    """
    keyboard = {"inline_keyboard": [buttons]}
    requests.post(
        f"{BASE}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "reply_markup": keyboard,
        },
        timeout=20,
    )


def answer_callback(callback_query_id: str) -> None:
    requests.post(
        f"{BASE}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id},
        timeout=10,
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
        "Crypto Bot Help\n\n"
        "--- Market Analysis ---\n"
        "/overall - top overall coins\n"
        "/momentum - top momentum coins\n"
        "/safer - safer coins\n"
        "/scan - mixed signal scan\n"
        "/alerts - top alert candidates\n"
        "/refresh - refresh cache\n\n"
        "--- Brain / AI ---\n"
        "/brain BTC - full AI brain analysis\n"
        "/braintop - top 10 coins by brain score\n"
        "/compare BTC ETH - side-by-side comparison\n"
        "/news BTC - latest news + sentiment\n"
        "/social BTC - Reddit mentions + sentiment\n"
        "/whales BTC - whale & volume activity\n"
        "/funding BTC - Binance funding rates\n"
        "/feargreed - crypto fear & greed index\n"
        "/trending - coins consistently in scans\n"
        "/report - full market report now\n\n"
        "--- Price Alerts ---\n"
        "/setalert BTC 100000 - alert when BTC hits price\n"
        "/myalerts - list your active price alerts\n"
        "/delalert 3 - remove alert by ID\n\n"
        "--- Portfolio ---\n"
        "/port - show portfolio P&L\n"
        "/port add BTC 0.5 95000 - add holding\n"
        "/port remove BTC - remove holding\n\n"
        "--- Watchlist & Alerts ---\n"
        "/watch BTC - add to watchlist\n"
        "/unwatch BTC - remove from watchlist\n"
        "/watchlist - show your watchlist\n"
        "/alerts_on - enable alerts\n"
        "/alerts_off - disable alerts\n"
        "/setscore 0.60 - set minimum alert score\n\n"
        "--- Paper Trading ---\n"
        "/paper_open TAO 10 - open paper position\n"
        "/paper_close TAO - close paper position\n"
        "/paper_positions - show open trades\n"
        "/paper_stats - trading stats\n\n"
        "--- Gems ---\n"
        "/newgems - scan for new gem launches\n"
        "/gems_on - enable gem alerts\n"
        "/gems_off - disable gem alerts\n"
        "/prelaunch - tokens detected BEFORE DEX listing\n\n"
        "/settings - your current settings\n"
        "/start - restart bot\n"
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


def _fmt_usd(v: float) -> str:
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def _fmt_age(h: float) -> str:
    if h < 1:    return f"{int(h*60)}m"
    if h < 48:   return f"{h:.1f}h"
    return f"{h/24:.1f}d"


def format_gems(gems: list) -> str:
    if not gems:
        return "New Gem Scanner\n\nNo new gems found right now.\nTry again in a few minutes."

    text = "New Gem Scanner\n\n"
    for i, g in enumerate(gems, 1):
        tier    = g.get("tier", "dex")
        age_str = _fmt_age(g["age_hours"])
        mcap    = g.get("market_cap_usd") or g.get("fdv", 0)

        dex_label = g.get("dex") or g["chain"].upper()
        if tier == "pumpfun":
            koth_tag = " [KING OF HILL]" if g.get("is_koth") else ""
            text += (
                f"{i}. {g['name']} ({g['symbol'].upper()}) [PUMP.FUN]{koth_tag}\n"
                f"MCap: {_fmt_usd(mcap)} | Age: {age_str}\n"
                f"Replies: {g.get('reply_count', 0)} | Score: {g['gem_score']}/100\n"
            )
        else:
            ch5m = g.get("price_change_5m", 0)
            text += (
                f"{i}. {g['name']} ({g['symbol'].upper()}) [{dex_label}]\n"
                f"Price: ${g['price_usd']:.6f}\n"
                f"5m: {ch5m:+.1f}% | 1h: {g['price_change_1h']:+.1f}% | 24h: {g['price_change_24h']:+.1f}%\n"
                f"Vol5m: {_fmt_usd(g.get('volume_5m',0))} | Liq: {_fmt_usd(g['liquidity_usd'])} | FDV: {_fmt_usd(g['fdv'])}\n"
                f"Age: {age_str} | Score: {g['gem_score']}/100\n"
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
                    # Pump.fun early launches use a lower score threshold
                    threshold = 40 if gem.get("tier") == "pumpfun" else GEM_ALERT_SCORE
                    if gem["gem_score"] < threshold:
                        continue

                    token_key = f"{gem['token_address']}:{gem['chain']}"
                    if _is_gem_alerted(token_key):
                        continue

                    _mark_gem_alerted(token_key)

                    age_str  = _fmt_age(gem["age_hours"])
                    tier     = gem.get("tier", "dex")
                    mcap     = gem.get("market_cap_usd") or gem.get("fdv", 0)
                    koth_tag = " [KING OF HILL]" if gem.get("is_koth") else ""

                    dex_label = gem.get("dex") or gem["chain"].upper()
                    if tier == "pumpfun":
                        msg = (
                            f"EARLY LAUNCH ALERT{koth_tag}\n\n"
                            f"{gem['name']} ({gem['symbol'].upper()}) on PUMP.FUN\n"
                            f"MCap: {_fmt_usd(mcap)}\n"
                            f"Age: {age_str} | Replies: {gem.get('reply_count', 0)}\n"
                            f"Score: {gem['gem_score']}/100\n"
                        )
                    else:
                        ch5m = gem.get("price_change_5m", 0)
                        msg = (
                            f"NEW GEM ALERT\n\n"
                            f"{gem['name']} ({gem['symbol'].upper()}) [{dex_label}]\n"
                            f"Price: ${gem['price_usd']:.6f}\n"
                            f"5m: {ch5m:+.1f}% | 1h: {gem['price_change_1h']:+.1f}%\n"
                            f"Vol5m: {_fmt_usd(gem.get('volume_5m',0))} | Liq: {_fmt_usd(gem['liquidity_usd'])}\n"
                            f"Age: {age_str} | Score: {gem['gem_score']}/100\n"
                        )

                    if gem.get("url"):
                        msg += f"Chart: {gem['url']}\n"

                    # Helius enrichment — adds rug risk + creator history
                    try:
                        enrichment = enrich_gem(
                            gem.get("token_address", ""),
                            gem.get("chain", "solana"),
                        )
                        risk_text = format_enrichment(enrichment)
                        if risk_text:
                            msg += f"\n{risk_text}\n"
                    except Exception:
                        pass

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
            send_brain_alerts()
        except Exception as e:
            print("ALERT LOOP ERROR:", e)
        time.sleep(ALERT_POLL_SECONDS)


def send_brain_alerts():
    """Alert watchlist users when brain score crosses BRAIN_ALERT_THRESHOLD."""
    try:
        users = get_all_watchlist_users()
        if not users:
            return

        data    = api("/scan?limit=50")
        results = data.get("results", [])
        if not results:
            return

        # Build brain scores for all scan results in one pass
        from app.brain import get_brain_report
        brain_map = get_brain_report(results)

        for chat_id in users:
            settings = ensure_user(chat_id)
            if not settings.get("alerts_enabled", True):
                continue

            watchlist = get_watchlist(chat_id)
            for sym in watchlist:
                brain = brain_map.get(sym.upper())
                if not brain:
                    continue
                score = brain.get("brain_score", 0)
                if score < BRAIN_ALERT_THRESHOLD:
                    continue

                # Use alert_state table to avoid spam (reuse existing cooldown)
                fake_coin = {"symbol": sym, "pump_probability_6h": score / 100, "ai_signal": f"Brain:{score}"}
                if not should_send_alert_advanced(chat_id, fake_coin):
                    continue

                reasons = "\n".join(f"  - {r}" for r in brain.get("brain_reason", [])[:3])
                msg = (
                    f"Brain Alert: {sym.upper()}\n\n"
                    f"Brain Score: {score}/100\n"
                    f"Signal: {brain.get('brain_signal', '-')}\n\n"
                    f"Detected:\n{reasons}"
                )
                send_message(chat_id, msg)
                mark_alert_sent_advanced(chat_id, fake_coin)

    except Exception as e:
        print(f"[BRAIN ALERT] Error: {e}")


def price_alert_loop():
    """Check price alerts against current scan data."""
    time.sleep(60)  # wait for bot to start
    while True:
        try:
            alerts = get_all_active_alerts()
            if alerts:
                data    = api("/scan?limit=100")
                results = data.get("results", [])
                prices  = {str(c.get("symbol", "")).upper(): float(c.get("current_price", 0) or 0)
                           for c in results}

                for alert in alerts:
                    sym   = alert["symbol"]
                    price = prices.get(sym, 0)
                    if price <= 0:
                        continue

                    target    = alert["target"]
                    direction = alert["direction"]
                    triggered = (direction == "above" and price >= target) or \
                                (direction == "below" and price <= target)

                    if triggered:
                        dir_label = "risen above" if direction == "above" else "dropped below"
                        msg = (
                            f"Price Alert Triggered!\n\n"
                            f"{sym} has {dir_label} ${target:,.4f}\n"
                            f"Current price: ${price:,.4f}"
                        )
                        send_message(alert["chat_id"], msg)
                        mark_alert_triggered(alert["id"])

        except Exception as e:
            print(f"[PRICE ALERT] Error: {e}")

        time.sleep(PRICE_ALERT_POLL)


def daily_report_loop():
    """Send a daily summary to all users at DAILY_REPORT_HOUR_UTC."""
    import datetime
    last_sent_date = None

    time.sleep(120)  # wait for everything to start
    while True:
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            today   = now_utc.date()

            if now_utc.hour == DAILY_REPORT_HOUR_UTC and last_sent_date != today:
                last_sent_date = today
                users = all_users()
                if not users:
                    time.sleep(3600)
                    continue

                # Build report
                try:
                    from app.fear_greed import get_fear_greed, fear_greed_context
                    fg      = get_fear_greed()
                    fg_val  = fg["value"]
                    fg_lab  = fg["label"]
                    fg_ctx  = fear_greed_context(fg_val)
                except Exception:
                    fg_val, fg_lab, fg_ctx = 50, "Neutral", "No data"

                try:
                    top_data = api("/top-overall?limit=5")
                    top_coins = top_data.get("results", [])
                except Exception:
                    top_coins = []

                try:
                    from app.brain import get_brain_report
                    brain_data = get_brain_report(top_coins[:10])
                    top_brain  = sorted(brain_data.values(), key=lambda x: x["brain_score"], reverse=True)[:3]
                except Exception:
                    top_brain = []

                bar = "#" * (fg_val // 10) + "-" * (10 - fg_val // 10)
                report = [
                    f"Daily Report",
                    f"",
                    f"Fear & Greed: {fg_val}/100 [{bar}]",
                    f"Status: {fg_lab} - {fg_ctx}",
                    f"",
                ]

                if top_coins:
                    report.append("Top Coins Today:")
                    for i, c in enumerate(top_coins[:5], 1):
                        sym   = str(c.get("symbol", "")).upper()
                        score = c.get("final_score", 0)
                        prob  = c.get("pump_probability_6h", 0)
                        report.append(f"  {i}. {sym} | Score: {score:.3f} | AI: {prob:.0%}")

                if top_brain:
                    report.append("")
                    report.append("Brain Picks:")
                    for b in top_brain:
                        report.append(
                            f"  {b['symbol']}: {b['brain_score']}/100 - {b['brain_signal']}"
                        )

                report.append("")
                report.append("Have a great trading day!")

                msg = "\n".join(report)
                for uid in users:
                    try:
                        send_message(int(uid), msg)
                        time.sleep(0.3)
                    except Exception:
                        pass

        except Exception as e:
            print(f"[DAILY REPORT] Error: {e}")

        time.sleep(3600)  # check every hour


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

                # Handle inline keyboard callbacks
                cb = upd.get("callback_query")
                if cb:
                    answer_callback(cb["id"])
                    cb_chat = cb["from"]["id"]
                    cb_data = cb.get("data", "")
                    ensure_user(cb_chat)
                    if cb_data.startswith("brain:"):
                        sym = cb_data.split(":", 1)[1]
                        send_message(cb_chat, f"Analyzing {sym}...")
                        send_message(cb_chat, format_brain_text(sym))
                    elif cb_data.startswith("news:"):
                        sym = cb_data.split(":", 1)[1]
                        send_message(cb_chat, format_news_text(sym))
                    elif cb_data.startswith("funding:"):
                        sym = cb_data.split(":", 1)[1]
                        send_message(cb_chat, format_funding_text(sym))
                    continue

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

                elif text.startswith("/brain"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /brain BTC\nExample: /brain ETH")
                    else:
                        sym = parts[1].strip().upper()
                        send_message(chat_id, f"Analyzing brain signals for {sym}...")
                        # Try to get coin data from scan for richer analysis
                        try:
                            scan_data = api("/scan?limit=50")
                            coin_data = next(
                                (c for c in scan_data.get("results", [])
                                 if str(c.get("symbol", "")).upper() == sym),
                                {}
                            )
                        except Exception:
                            coin_data = {}
                        send_message(chat_id, format_brain_text(sym, coin_data))

                elif text.startswith("/news"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /news BTC\nExample: /news ETH")
                    else:
                        sym = parts[1].strip().upper()
                        send_message(chat_id, f"Fetching news for {sym}...")
                        send_message(chat_id, format_news_text(sym))

                elif text.startswith("/social"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /social BTC\nExample: /social ETH")
                    else:
                        sym = parts[1].strip().upper()
                        send_message(chat_id, f"Scanning Reddit for {sym}...")
                        send_message(chat_id, format_social_text(sym))

                elif text.startswith("/whales"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /whales BTC\nExample: /whales ETH")
                    else:
                        sym = parts[1].strip().upper()
                        send_message(chat_id, format_whale_text(sym))

                elif text == "/trending":
                    coins = get_trending_coins(min_scans=3, limit=10)
                    if not coins:
                        send_message(chat_id, "No trending coins tracked yet.\nRun /scan a few times to build memory.")
                    else:
                        lines = ["Trending Coins (consistently in scans)\n"]
                        for i, c in enumerate(coins, 1):
                            lines.append(
                                f"{i}. {c['symbol']} | Score: {c['avg_score']:.3f} | "
                                f"Seen: {c['scan_count']}x | {c['consecutive']} in a row"
                            )
                        send_message(chat_id, "\n".join(lines))

                elif text == "/feargreed":
                    send_message(chat_id, format_fear_greed())

                elif text.startswith("/funding"):
                    parts = text.split(maxsplit=1)
                    if len(parts) < 2 or not parts[1].strip():
                        send_message(chat_id, "Usage: /funding BTC\nExample: /funding ETH")
                    else:
                        send_message(chat_id, format_funding_text(parts[1].strip().upper()))

                elif text == "/braintop":
                    send_message(chat_id, "Building brain scores for top coins...")
                    try:
                        top_data = api("/scan?limit=20")
                        top_results = top_data.get("results", [])
                        from app.brain import get_brain_report
                        brain_map = get_brain_report(top_results)
                        ranked = sorted(brain_map.values(), key=lambda x: x["brain_score"], reverse=True)[:10]
                        lines = ["Brain Leaderboard (Top 10)\n"]
                        for i, b in enumerate(ranked, 1):
                            lines.append(
                                f"{i}. {b['symbol']}: {b['brain_score']}/100 - {b['brain_signal']}"
                            )
                        send_message(chat_id, "\n".join(lines))
                    except Exception as e:
                        send_message(chat_id, f"Error building brain leaderboard: {e}")

                elif text.startswith("/compare"):
                    parts = text.split()
                    if len(parts) != 3:
                        send_message(chat_id, "Usage: /compare BTC ETH")
                    else:
                        sym1, sym2 = parts[1].upper(), parts[2].upper()
                        send_message(chat_id, f"Comparing {sym1} vs {sym2}...")
                        try:
                            scan_data = api("/scan?limit=50")
                            results   = scan_data.get("results", [])
                            c1 = next((c for c in results if str(c.get("symbol","")).upper() == sym1), {})
                            c2 = next((c for c in results if str(c.get("symbol","")).upper() == sym2), {})
                            b1 = analyze_coin_brain(sym1, c1)
                            b2 = analyze_coin_brain(sym2, c2)
                            def pad(s, n=12): return str(s)[:n].ljust(n)
                            lines = [
                                f"Comparison: {sym1} vs {sym2}\n",
                                f"{'Metric':<14} {pad(sym1):<12} {pad(sym2)}",
                                f"{'-'*38}",
                                f"{'Brain Score':<14} {b1['brain_score']:<12} {b2['brain_score']}",
                                f"{'Signal':<14} {pad(b1['brain_signal']):<12} {pad(b2['brain_signal'])}",
                                f"{'TA Score':<14} {b1['ta_score']:<12} {b2['ta_score']}",
                                f"{'AI Prob':<14} {b1['ai_prob']:.0%}{'':8} {b2['ai_prob']:.0%}",
                                f"{'News':<14} {pad(b1['news_sent']):<12} {pad(b2['news_sent'])}",
                                f"{'Reddit':<14} {pad(b1['social_sent']):<12} {pad(b2['social_sent'])}",
                                f"{'Whale':<14} {pad(b1['whale_signal']):<12} {pad(b2['whale_signal'])}",
                                f"{'F&G':<14} {b1['fear_greed']:<12} {b2['fear_greed']}",
                            ]
                            winner = sym1 if b1["brain_score"] >= b2["brain_score"] else sym2
                            lines.append(f"\nEdge: {winner} has the stronger brain score")
                            send_message(chat_id, "\n".join(lines))
                        except Exception as e:
                            send_message(chat_id, f"Compare error: {e}")

                elif text == "/report":
                    try:
                        from app.fear_greed import get_fear_greed, fear_greed_context
                        fg     = get_fear_greed()
                        fg_val = fg["value"]
                        fg_lab = fg["label"]
                        bar    = "#" * (fg_val // 10) + "-" * (10 - fg_val // 10)
                        top    = api("/top-overall?limit=5").get("results", [])
                        from app.brain import get_brain_report
                        brain_data = get_brain_report(top)
                        top_brain  = sorted(brain_data.values(), key=lambda x: x["brain_score"], reverse=True)[:3]

                        report = [
                            "Market Report\n",
                            f"Fear & Greed: {fg_val}/100 [{bar}]",
                            f"Status: {fg_lab} - {fear_greed_context(fg_val)}",
                            "",
                            "Top Coins:",
                        ]
                        for i, c in enumerate(top[:5], 1):
                            sym   = str(c.get("symbol","")).upper()
                            score = c.get("final_score", 0)
                            prob  = c.get("pump_probability_6h", 0)
                            report.append(f"  {i}. {sym} | Score: {score:.3f} | AI: {prob:.0%}")

                        if top_brain:
                            report += ["", "Brain Picks:"]
                            for b in top_brain:
                                report.append(f"  {b['symbol']}: {b['brain_score']}/100 - {b['brain_signal']}")

                        send_message(chat_id, "\n".join(report))
                    except Exception as e:
                        send_message(chat_id, f"Report error: {e}")

                elif text.startswith("/setalert"):
                    parts = text.split()
                    if len(parts) != 3:
                        send_message(chat_id, "Usage: /setalert BTC 100000\nBot detects above/below automatically.")
                    else:
                        try:
                            sym    = parts[1].upper()
                            target = float(parts[2].replace(",", ""))
                            # Detect direction from current price
                            scan_data = api("/scan?limit=100")
                            results   = scan_data.get("results", [])
                            coin      = next((c for c in results if str(c.get("symbol","")).upper() == sym), None)
                            if coin:
                                cur_price = float(coin.get("current_price", 0) or 0)
                                direction = "above" if target > cur_price else "below"
                            else:
                                direction = "above"  # default fallback
                            alert_id = add_price_alert(chat_id, sym, target, direction)
                            send_message(
                                chat_id,
                                f"Price alert set!\n{sym} {direction} ${target:,.4f}\nAlert ID: {alert_id}"
                            )
                        except ValueError:
                            send_message(chat_id, "Invalid price. Example: /setalert BTC 100000")

                elif text == "/myalerts":
                    send_message(chat_id, format_user_alerts(chat_id))

                elif text.startswith("/delalert"):
                    parts = text.split()
                    if len(parts) != 2:
                        send_message(chat_id, "Usage: /delalert <ID>\nSee IDs with /myalerts")
                    else:
                        try:
                            alert_id = int(parts[1])
                            ok = remove_price_alert(chat_id, alert_id)
                            send_message(chat_id, "Alert removed." if ok else "Alert not found.")
                        except ValueError:
                            send_message(chat_id, "Invalid ID. Example: /delalert 3")

                elif text.startswith("/port"):
                    parts = text.split(maxsplit=1)
                    sub   = parts[1].strip() if len(parts) > 1 else ""

                    if not sub or sub == "show":
                        # Show portfolio with live prices
                        scan_data = api("/scan?limit=200")
                        results   = scan_data.get("results", [])
                        live      = {str(c.get("symbol","")).upper(): float(c.get("current_price",0) or 0)
                                     for c in results}
                        send_message(chat_id, format_portfolio(chat_id, live))

                    elif sub.startswith("add "):
                        tokens = sub.split()
                        if len(tokens) != 4:
                            send_message(chat_id, "Usage: /port add BTC 0.5 95000\n(symbol, quantity, avg buy price)")
                        else:
                            try:
                                sym   = tokens[1].upper()
                                qty   = float(tokens[2])
                                price = float(tokens[3].replace(",", ""))
                                action = add_holding(chat_id, sym, qty, price)
                                send_message(chat_id, f"Portfolio {action}: {qty} {sym} @ ${price:,.4f}")
                            except ValueError:
                                send_message(chat_id, "Invalid numbers. Example: /port add BTC 0.5 95000")

                    elif sub.startswith("remove "):
                        sym = sub.split()[1].upper() if len(sub.split()) > 1 else ""
                        if not sym:
                            send_message(chat_id, "Usage: /port remove BTC")
                        else:
                            ok = remove_holding(chat_id, sym)
                            send_message(chat_id, f"Removed {sym} from portfolio." if ok else f"{sym} not in portfolio.")
                    else:
                        send_message(chat_id, "Portfolio commands:\n/port - show\n/port add BTC 0.5 95000\n/port remove BTC")

                elif text == "/prelaunch":
                    send_message(chat_id, format_prelaunch_list())

                else:
                    send_message(chat_id, f"Unknown command: {text}\nType /help for all commands.")

        except Exception as e:
            print("ERROR:", e)
            time.sleep(2)


def _send_all_gem_users(msg: str):
    """Broadcast a prelaunch alert to all users with gem alerts enabled."""
    try:
        users = all_users()
        for uid, s in users.items():
            if s.get("gems_enabled", True):
                try:
                    send_message(int(uid), msg)
                    time.sleep(0.3)
                except Exception:
                    pass
    except Exception as e:
        print(f"[PRELAUNCH] broadcast error: {e}")


if __name__ == "__main__":
    init_db()
    for init_fn in [init_memory_table, init_price_alerts_table, init_portfolio_table, init_prelaunch_tables]:
        try:
            init_fn()
        except Exception:
            pass
    register_callback(_send_all_gem_users)
    threading.Thread(target=alert_loop,       daemon=True).start()
    threading.Thread(target=gem_alert_loop,   daemon=True).start()
    threading.Thread(target=price_alert_loop, daemon=True).start()
    threading.Thread(target=daily_report_loop, daemon=True).start()
    start_listener()   # starts pump.fun WebSocket + monitor_loop threads
    main()