"""
Pre-launch tracker for pump.fun tokens.

How it works:
  1. Connects to pump.fun WebSocket (wss://pumpportal.fun/api/data)
  2. Subscribes to "new token" events — fires within SECONDS of mint creation
  3. Stores every new token in SQLite watchlist
  4. Background loop checks each token's bonding curve progress every 2 min
  5. Sends Telegram alerts at milestones:
       $10K  → gaining traction
       $30K  → strong momentum
       $50K  → approaching DEX graduation (~$69K target)
       $65K+ → GRADUATED — now launching on Raydium

Credit cost: ZERO (pump.fun WebSocket is public, no API key needed)
Helius only used for enrichment after milestone alerts.
"""
import asyncio
import json
import sqlite3
import time
import threading
import requests
import urllib3
from pathlib import Path
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

try:
    import websockets
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False
    print("[PRELAUNCH] websockets not installed — run: pip install websockets>=12.0")

DB_PATH          = Path(__file__).resolve().parent.parent / "user_data.db"
PUMPFUN_WS       = "wss://pumpportal.fun/api/data"
GRADUATION_MCAP  = 65_000    # ~$69K is the graduation threshold
TRACK_HOURS      = 6         # stop tracking after 6 hours
MONITOR_INTERVAL = 120       # check progress every 2 minutes

_alert_callbacks: list = []  # registered Telegram send functions

# Batch buffer for WebSocket new-token alerts (rate-limited to 1 per 2 min)
_batch_buffer:  list  = []
_batch_last_ts: float = 0.0
BATCH_INTERVAL = 120   # seconds between batch digests


# ──────────────────────────────────────────────────────────────────────────
# DB setup
# ──────────────────────────────────────────────────────────────────────────

def init_prelaunch_tables():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
    CREATE TABLE IF NOT EXISTS prelaunch_tokens (
        mint            TEXT PRIMARY KEY,
        name            TEXT,
        symbol          TEXT,
        creator         TEXT,
        detected_ts     INTEGER,
        last_mcap_usd   REAL    DEFAULT 0,
        peak_mcap_usd   REAL    DEFAULT 0,
        last_checked_ts INTEGER DEFAULT 0,
        graduated       INTEGER DEFAULT 0,
        m_10k           INTEGER DEFAULT 0,
        m_30k           INTEGER DEFAULT 0,
        m_50k           INTEGER DEFAULT 0,
        m_grad          INTEGER DEFAULT 0,
        sol_price       REAL    DEFAULT 150
    )
    """)
    con.execute("""
    CREATE TABLE IF NOT EXISTS prelaunch_history (
        id   INTEGER PRIMARY KEY AUTOINCREMENT,
        mint TEXT,
        mcap REAL,
        ts   INTEGER
    )
    """)
    con.commit()
    # Add ETA alert columns for existing DBs (safe to run multiple times)
    for col in ["eta_2h", "eta_1h", "eta_30m"]:
        try:
            con.execute(f"ALTER TABLE prelaunch_tokens ADD COLUMN {col} INTEGER DEFAULT 0")
            con.commit()
        except Exception:
            pass
    con.close()


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────

def _add(mint, name, symbol, creator, mcap_usd, sol_price):
    now = int(time.time())
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR IGNORE INTO prelaunch_tokens
        (mint, name, symbol, creator, detected_ts, last_mcap_usd, peak_mcap_usd, last_checked_ts, sol_price)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (mint, name, symbol, creator, now, mcap_usd, mcap_usd, now, sol_price))
    con.commit()
    con.close()


def _update(mint, mcap):
    now = int(time.time())
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        UPDATE prelaunch_tokens
        SET last_mcap_usd=?, last_checked_ts=?,
            peak_mcap_usd=MAX(peak_mcap_usd, ?)
        WHERE mint=?
    """, (mcap, now, mcap, mint))
    con.execute(
        "INSERT INTO prelaunch_history (mint, mcap, ts) VALUES (?,?,?)",
        (mint, mcap, now)
    )
    con.commit()
    con.close()


def _mark(mint, col):
    con = sqlite3.connect(DB_PATH)
    con.execute(f"UPDATE prelaunch_tokens SET {col}=1 WHERE mint=?", (mint,))
    con.commit()
    con.close()


def _flags(mint) -> dict:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT m_10k, m_30k, m_50k, m_grad,
               COALESCE(eta_2h,0), COALESCE(eta_1h,0), COALESCE(eta_30m,0)
        FROM prelaunch_tokens WHERE mint=?
    """, (mint,))
    row = cur.fetchone()
    con.close()
    if not row:
        return {}
    return {
        "m_10k":   bool(row[0]), "m_30k": bool(row[1]),
        "m_50k":   bool(row[2]), "m_grad": bool(row[3]),
        "eta_2h":  bool(row[4]), "eta_1h": bool(row[5]), "eta_30m": bool(row[6]),
    }


def get_active_tokens() -> list:
    cutoff = int(time.time()) - TRACK_HOURS * 3600
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT mint, name, symbol, creator, detected_ts,
               last_mcap_usd, peak_mcap_usd, graduated
        FROM prelaunch_tokens
        WHERE detected_ts > ? AND graduated = 0
        ORDER BY last_mcap_usd DESC
    """, (cutoff,))
    rows = cur.fetchall()
    con.close()
    return [
        {"mint": r[0], "name": r[1], "symbol": r[2], "creator": r[3],
         "detected_ts": r[4], "last_mcap_usd": r[5], "peak_mcap_usd": r[6], "graduated": r[7]}
        for r in rows
    ]


def get_all_recent(limit=20) -> list:
    """All tokens detected in last 6 hours, including graduated."""
    cutoff = int(time.time()) - TRACK_HOURS * 3600
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT mint, name, symbol, detected_ts, last_mcap_usd, peak_mcap_usd, graduated
        FROM prelaunch_tokens
        WHERE detected_ts > ?
        ORDER BY peak_mcap_usd DESC
        LIMIT ?
    """, (cutoff, limit))
    rows = cur.fetchall()
    con.close()
    return [{"mint": r[0], "name": r[1], "symbol": r[2], "detected_ts": r[3],
             "last_mcap_usd": r[4], "peak_mcap_usd": r[5], "graduated": bool(r[6])}
            for r in rows]


# ──────────────────────────────────────────────────────────────────────────
# Price / MCap fetchers
# ──────────────────────────────────────────────────────────────────────────

def _sol_price() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=6, verify=False,
        )
        if r.status_code == 200:
            return float(r.json().get("solana", {}).get("usd", 150))
    except Exception:
        pass
    return 150.0


_sol_price_cache = 150.0
_sol_price_ts    = 0.0


def _cached_sol_price() -> float:
    global _sol_price_cache, _sol_price_ts
    now = time.time()
    if now - _sol_price_ts > 600:
        _sol_price_cache = _sol_price()
        _sol_price_ts    = now
    return _sol_price_cache


def _fetch_mcap(mint: str) -> float:
    """Returns USD market cap for a pump.fun token."""
    # Try pump.fun API
    try:
        r = requests.get(
            f"https://frontend-api.pump.fun/coins/{mint}",
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pump.fun/"},
            timeout=8, verify=False,
        )
        if r.status_code == 200:
            v = float(r.json().get("usd_market_cap", 0) or 0)
            if v > 0:
                return v
    except Exception:
        pass

    # Fallback: GeckoTerminal
    try:
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}",
            headers={"Accept": "application/json;version=20230302"},
            timeout=8, verify=False,
        )
        if r.status_code == 200:
            a = r.json().get("data", {}).get("attributes", {})
            v = float(a.get("market_cap_usd") or a.get("fdv_usd") or 0)
            return v
    except Exception:
        pass

    return 0.0


def _eta_minutes(mint: str, current_mcap: float) -> int:
    """Estimate minutes until graduation using MCap velocity."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cutoff = int(time.time()) - 3600
    cur.execute(
        "SELECT mcap, ts FROM prelaunch_history WHERE mint=? AND ts>? ORDER BY ts ASC",
        (mint, cutoff)
    )
    rows = cur.fetchall()
    con.close()

    if len(rows) < 3:
        return -1

    first_mcap, first_ts = rows[0]
    last_mcap,  last_ts  = rows[-1]
    elapsed = last_ts - first_ts
    if elapsed <= 0 or last_mcap <= first_mcap:
        return -1

    velocity  = (last_mcap - first_mcap) / (elapsed / 60)  # USD per minute
    remaining = GRADUATION_MCAP - current_mcap
    if velocity <= 0 or remaining <= 0:
        return 0

    return max(1, int(remaining / velocity))


# ──────────────────────────────────────────────────────────────────────────
# Alert system
# ──────────────────────────────────────────────────────────────────────────

def register_callback(fn):
    _alert_callbacks.append(fn)


def _alert(msg: str):
    for fn in _alert_callbacks:
        try:
            fn(msg)
        except Exception:
            pass


def _fmt(v: float) -> str:
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:.0f}"


# ──────────────────────────────────────────────────────────────────────────
# Monitoring loop
# ──────────────────────────────────────────────────────────────────────────

def monitor_loop():
    time.sleep(90)
    while True:
        try:
            tokens = get_active_tokens()
            for t in tokens:
                mint   = t["mint"]
                name   = t["name"]
                symbol = t["symbol"]

                mcap = _fetch_mcap(mint)
                if mcap <= 0:
                    time.sleep(0.5)
                    continue

                _update(mint, mcap)
                f = _flags(mint)

                age_min = int((time.time() - t["detected_ts"]) / 60)
                eta     = _eta_minutes(mint, mcap)
                eta_str = f"~{eta}min to DEX" if eta > 0 else ""

                # ── MCap milestone alerts — ONLY fire if token is fresh (<= 90 min old) ──
                # Old stagnant tokens are silently marked to avoid spam
                if mcap >= GRADUATION_MCAP and not f.get("m_grad"):
                    _mark(mint, "m_grad")
                    _mark(mint, "graduated")
                    con = sqlite3.connect(DB_PATH)
                    con.execute("UPDATE prelaunch_tokens SET graduated=1 WHERE mint=?", (mint,))
                    con.commit()
                    con.close()
                    # Graduation alert always fires regardless of age
                    _alert(
                        f"GRADUATED TO DEX!\n\n"
                        f"{name} ({symbol}) just hit {_fmt(mcap)} MCap\n"
                        f"Now launching on Raydium!\n"
                        f"Age: {age_min}m\n"
                        f"Chart: https://dexscreener.com/solana/{mint}"
                    )

                elif mcap >= 50_000 and not f.get("m_50k"):
                    _mark(mint, "m_50k")
                    if age_min <= 180:   # only alert if < 3 hours old
                        _alert(
                            f"APPROACHING DEX LAUNCH!\n\n"
                            f"{name} ({symbol})\n"
                            f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                            f"{eta_str}\n"
                            f"Close to $69K graduation threshold!\n"
                            f"https://pump.fun/{mint}"
                        )

                elif mcap >= 30_000 and not f.get("m_30k"):
                    _mark(mint, "m_30k")
                    if age_min <= 120:   # only alert if < 2 hours old
                        _alert(
                            f"PRE-LAUNCH: Strong Momentum\n\n"
                            f"{name} ({symbol})\n"
                            f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                            f"{eta_str}\n"
                            f"https://pump.fun/{mint}"
                        )

                elif mcap >= 5_000 and not f.get("m_10k"):
                    _mark(mint, "m_10k")
                    if age_min <= 60:    # only alert if < 1 hour old — fresh token!
                        _alert(
                            f"PRE-LAUNCH: Gaining Traction!\n\n"
                            f"{name} ({symbol})\n"
                            f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                            f"https://pump.fun/{mint}"
                        )

                # ── ETA-based countdown alerts (independent of MCap milestones) ──
                if eta > 0:
                    if eta <= 30 and not f.get("eta_30m"):
                        _mark(mint, "eta_30m")
                        _alert(
                            f"LAUNCHING IN ~30 MINUTES!\n\n"
                            f"{name} ({symbol.upper()})\n"
                            f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                            f"Buy on pump.fun NOW before DEX listing:\n"
                            f"https://pump.fun/{mint}"
                        )
                    elif eta <= 60 and not f.get("eta_1h"):
                        _mark(mint, "eta_1h")
                        _alert(
                            f"DEX LAUNCH IN ~1 HOUR\n\n"
                            f"{name} ({symbol.upper()})\n"
                            f"MCap: {_fmt(mcap)} | ETA: ~{eta}min\n"
                            f"https://pump.fun/{mint}"
                        )
                    elif eta <= 120 and not f.get("eta_2h"):
                        _mark(mint, "eta_2h")
                        _alert(
                            f"DEX LAUNCH IN ~2 HOURS\n\n"
                            f"{name} ({symbol.upper()})\n"
                            f"MCap: {_fmt(mcap)} | ETA: ~{eta}min\n"
                            f"https://pump.fun/{mint}"
                        )

                time.sleep(0.5)

        except Exception as e:
            print(f"[PRELAUNCH] Monitor error: {e}")

        time.sleep(MONITOR_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────
# WebSocket listener
# ──────────────────────────────────────────────────────────────────────────

async def _ws_listen():
    while True:
        try:
            print("[PRELAUNCH] Connecting to pump.fun WebSocket...")
            async with websockets.connect(
                PUMPFUN_WS,
                additional_headers={
                    "Origin":     "https://pump.fun",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                },
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                await ws.send(json.dumps({"method": "subscribeNewToken"}))
                print("[PRELAUNCH] Subscribed — listening for new tokens")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        if msg.get("txType") != "create":
                            continue

                        mint    = msg.get("mint", "")
                        name    = msg.get("name", "Unknown")
                        symbol  = msg.get("symbol", "?")
                        creator = msg.get("traderPublicKey", "")
                        mcap_sol = float(msg.get("marketCapSol", 0) or 0)

                        if not mint:
                            continue

                        sol = _cached_sol_price()
                        mcap_usd = mcap_sol * sol

                        print(f"[PRELAUNCH] New: {name} ({symbol}) MCap ${mcap_usd:,.0f}")
                        _add(mint, name, symbol, creator, mcap_usd, sol)

                        # Batch buffer — collect new tokens and alert every 2 min
                        global _batch_buffer, _batch_last_ts
                        _batch_buffer.append({
                            "name": name, "symbol": symbol,
                            "mcap": mcap_usd, "mint": mint,
                        })

                        now = time.time()
                        if now - _batch_last_ts >= BATCH_INTERVAL and _batch_buffer:
                            _batch_last_ts = now
                            # Sort by highest initial mcap, show top 5
                            batch = sorted(
                                _batch_buffer, key=lambda x: x["mcap"], reverse=True
                            )[:5]
                            _batch_buffer.clear()

                            lines = [f"NEW PUMP.FUN LAUNCHES ({len(batch)} tokens)\n"]
                            for t in batch:
                                lines.append(
                                    f"• {t['name']} ({t['symbol'].upper()})\n"
                                    f"  MCap: {_fmt(t['mcap'])}\n"
                                    f"  https://pump.fun/{t['mint']}"
                                )
                            lines.append("\nMonitoring for DEX graduation...")
                            _alert("\n".join(lines))

                    except Exception as e:
                        print(f"[PRELAUNCH] Parse error: {e}")

        except Exception as e:
            print(f"[PRELAUNCH] WS error: {e} — retry in 5s")
            await asyncio.sleep(5)


def start_listener():
    if not WS_AVAILABLE:
        print("[PRELAUNCH] websockets not installed, listener skipped")
        return

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_ws_listen())

    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    print("[PRELAUNCH] Tracker started")


# ──────────────────────────────────────────────────────────────────────────
# Approaching-graduation helpers
# ──────────────────────────────────────────────────────────────────────────

def get_hot_preorders(max_age_minutes: int = 120, min_velocity: float = 40) -> list:
    """
    AI-curated pre-order list: tokens that are FRESH (< 2h old) AND
    growing fast enough (velocity >= $40/min) to be worth watching.
    Sorted by velocity — fastest pump first.
    """
    tokens = get_active_tokens()
    result = []
    now = time.time()

    for t in tokens:
        age_min = int((now - t["detected_ts"]) / 60)
        if age_min > max_age_minutes:
            continue          # too old — skip stagnant tokens

        mcap = t["last_mcap_usd"]
        if mcap < 3_000 or mcap >= GRADUATION_MCAP:
            continue

        # Need at least 2 history points to measure velocity
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "SELECT mcap, ts FROM prelaunch_history WHERE mint=? ORDER BY ts ASC",
            (t["mint"],)
        )
        rows = cur.fetchall()
        con.close()

        if len(rows) < 2:
            continue

        elapsed_min = max((rows[-1][1] - rows[0][1]) / 60, 1)
        velocity    = (rows[-1][0] - rows[0][0]) / elapsed_min   # USD per minute

        if velocity < min_velocity:
            continue          # not growing fast enough

        eta = _eta_minutes(t["mint"], mcap)
        pct = min(mcap / GRADUATION_MCAP * 100, 100)

        result.append({
            **t,
            "age_min":        age_min,
            "velocity_usd_m": velocity,
            "eta_minutes":    eta if eta > 0 else -1,
            "progress_pct":   pct,
        })

    # Fastest growing first
    result.sort(key=lambda x: x["velocity_usd_m"], reverse=True)
    return result


def format_preorder_list() -> str:
    """Format the /preorder command — fresh, fast-moving pre-launch tokens."""
    tokens = get_hot_preorders(max_age_minutes=120, min_velocity=40)

    if not tokens:
        return (
            "Pre-Order List — Hot New Launches\n\n"
            "No hot pre-launch tokens right now.\n\n"
            "This list shows tokens that are:\n"
            "  - Less than 2 hours old\n"
            "  - Growing fast on the bonding curve\n"
            "  - Not yet listed on DEX\n\n"
            "Check back in a few minutes."
        )

    lines = [f"Pre-Order List — {len(tokens)} Hot Launch{'es' if len(tokens) > 1 else ''}\n"]

    for i, t in enumerate(tokens[:8], 1):
        eta = t["eta_minutes"]
        eta_str = f"~{eta}min to DEX" if eta > 0 else "ETA unknown"
        pct  = t["progress_pct"]
        filled = int(pct / 10)
        bar  = "#" * filled + "-" * (10 - filled)
        vel  = t["velocity_usd_m"]

        lines.append(
            f"{i}. {t['name']} ({t['symbol'].upper()})\n"
            f"   MCap: {_fmt(t['last_mcap_usd'])} | Age: {t['age_min']}m\n"
            f"   Speed: +${vel:.0f}/min | {eta_str}\n"
            f"   [{bar}] {pct:.0f}% to DEX\n"
            f"   Buy: https://pump.fun/{t['mint']}\n"
        )

    lines.append("DYOR — buy on pump.fun BEFORE DEX listing!")
    return "\n".join(lines)


def get_approaching_tokens(max_eta_hours: float = 6) -> list:
    """
    Returns tokens on the bonding curve that have positive velocity
    and an ETA to graduation within max_eta_hours.
    Sorted by closest ETA first.
    """
    tokens = get_active_tokens()
    result = []

    for t in tokens:
        mcap = t["last_mcap_usd"]
        if mcap < 4_000:       # too early, no real activity yet
            continue
        if mcap >= GRADUATION_MCAP:
            continue           # already graduated

        eta = _eta_minutes(t["mint"], mcap)
        if eta <= 0:
            continue           # flat or declining — no positive velocity
        if eta > max_eta_hours * 60:
            continue           # too far out

        # Calculate velocity from history for display
        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cutoff = int(time.time()) - 3600
        cur.execute(
            "SELECT mcap, ts FROM prelaunch_history WHERE mint=? AND ts>? ORDER BY ts ASC",
            (t["mint"], cutoff)
        )
        rows = cur.fetchall()
        con.close()

        if len(rows) >= 2:
            elapsed = max(rows[-1][1] - rows[0][1], 1) / 60
            velocity = (rows[-1][0] - rows[0][0]) / elapsed  # USD/min
        else:
            velocity = 0

        if velocity <= 0:
            continue

        result.append({
            **t,
            "eta_minutes":    eta,
            "velocity_usd_m": velocity,
            "progress_pct":   min(mcap / GRADUATION_MCAP * 100, 100),
        })

    result.sort(key=lambda x: x["eta_minutes"])
    return result


def format_upcoming() -> str:
    """Format the /upcoming command — tokens actively approaching DEX launch."""
    tokens = get_approaching_tokens(max_eta_hours=6)

    if not tokens:
        return (
            "Upcoming DEX Launches\n\n"
            "No tokens detected approaching graduation right now.\n\n"
            "Tokens appear here when they show strong buying momentum "
            "on the pump.fun bonding curve.\n"
            "Target: ~$69K MCap to graduate to Raydium DEX."
        )

    lines = [f"Upcoming DEX Launches — {len(tokens)} approaching\n"]

    for t in tokens[:8]:
        eta = t["eta_minutes"]
        if eta < 60:
            eta_str = f"~{eta}min"
        elif eta < 120:
            eta_str = f"~1h {eta % 60}m"
        else:
            eta_str = f"~{eta // 60}h {eta % 60}m"

        pct = t["progress_pct"]
        filled = int(pct / 10)
        bar = "#" * filled + "-" * (10 - filled)
        age_min = int((time.time() - t["detected_ts"]) / 60)

        lines.append(
            f"{t['name']} ({t['symbol'].upper()})\n"
            f"  MCap: {_fmt(t['last_mcap_usd'])} | ETA: {eta_str} | Age: {age_min}m\n"
            f"  [{bar}] {pct:.0f}% to DEX\n"
            f"  Buy: https://pump.fun/{t['mint']}\n"
        )

    lines.append("Buy on pump.fun BEFORE DEX listing for the best entry!")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Telegram display
# ──────────────────────────────────────────────────────────────────────────

def format_prelaunch_list() -> str:
    tokens = get_all_recent(limit=15)
    if not tokens:
        return (
            "Pre-Launch Tracker\n\n"
            "Waiting for new pump.fun launches...\n"
            "Tokens appear here within seconds of creation."
        )

    active    = [t for t in tokens if not t["graduated"]]
    graduated = [t for t in tokens if t["graduated"]]

    lines = [f"Pre-Launch Tracker — Last 6h\n"]

    if active:
        lines.append(f"On Bonding Curve ({len(active)}):")
        for t in active[:8]:
            age_min  = int((time.time() - t["detected_ts"]) / 60)
            pct      = min(t["last_mcap_usd"] / 69_000 * 100, 100)
            bar      = "#" * int(pct / 10) + "-" * (10 - int(pct // 10))
            lines.append(
                f"  {t['symbol']}: {_fmt(t['last_mcap_usd'])} | {age_min}m old\n"
                f"  [{bar}] {pct:.0f}% to DEX\n"
                f"  https://pump.fun/{t['mint']}"
            )

    if graduated:
        lines.append(f"\nGraduated to DEX ({len(graduated)}):")
        for t in graduated[:3]:
            age_min = int((time.time() - t["detected_ts"]) / 60)
            lines.append(
                f"  {t['symbol']}: peak {_fmt(t['peak_mcap_usd'])} | "
                f"detected {age_min}m ago\n"
                f"  https://dexscreener.com/solana/{t['mint']}"
            )

    return "\n".join(lines)
