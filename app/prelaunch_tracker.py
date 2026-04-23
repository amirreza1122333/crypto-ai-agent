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
import re
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

# These imports are intentionally isolated — each module is self-contained
# and the whole pipeline degrades gracefully when any of them is missing
# (e.g. Helius key not configured → sniper detector returns UNKNOWN).
from app.creator_reputation import (
    init_creator_reputation_table,
    get_creator_stats,
    recompute_creator,
    recompute_all_creators,
    top_creators,
)
from app.sniper_detector import (
    init_sniper_table,
    check_sniper_concentration,
    format_sniper_line,
)
from app.live_scorer import adjust_tier as ml_adjust_tier, model_is_available

DB_PATH          = Path(__file__).resolve().parent.parent / "user_data.db"
PUMPFUN_WS       = "wss://pumpportal.fun/api/data"
GRADUATION_MCAP       = 65_000  # ~$69K graduation threshold
TRACK_HOURS           = 6       # stop tracking after 6 hours
MONITOR_FRESH_SECS    = 30      # check tokens < 15 min old every 30 seconds
MONITOR_VETERAN_SECS  = 120     # check older tokens every 2 minutes
FRESH_TOKEN_MINUTES   = 15      # tokens under this age get priority checking
MONITOR_CRITICAL_SECS = 15      # tokens near graduation checked every 15 seconds
IMMINENT_MCAP_USD     = 35_000  # start "critical" tracking above $35K (~54% to grad)
IMMINENT_ETA_MIN      = 20      # fire imminent alert when ETA drops below 20 min
NEAR_GRAD_MCAP        = 50_000  # $50K = 77% to graduation — alert even without ETA data

# Graduation zone scanner — polls pump.fun API for tokens already mid-flight
GRAD_ZONE_POLL_SECS   = 60      # scan every 60 seconds
GRAD_ZONE_MIN_MCAP    = 30_000  # lower bound of zone
GRAD_ZONE_MAX_MCAP    = 65_000  # upper bound (graduation threshold)
GRAD_ZONE_API_LIMIT   = 50      # tokens per API page

_alert_callbacks: list = []  # registered Telegram send functions

# Option C: immediate alert threshold — tokens starting above this MCap
# already had a strong initial buy at creation (above bonding curve floor)
INSTANT_ALERT_MCAP = 3_500   # $3.5K+ initial MCap = above bonding curve floor

# Batch digest for everything below threshold (max 1 per 10 min, top 5 by mcap)
_batch_buffer:  list  = []
_batch_last_ts: float = 0.0
BATCH_INTERVAL = 600   # 10 minutes between batch digests (was 2 min)

# Outcome-logging horizons (minutes). For every detected token we record:
#   +1h  → did it 2x quickly?  (early-pump classifier label)
#   +6h  → did it graduate?    (bonding-curve completion label)
#   +24h → what was the peak?  (full lifecycle label)
# These power the future ML training set. DO NOT change the list lightly —
# historical rows are keyed by (mint, horizon_min).
OUTCOME_HORIZONS_MIN = [60, 360, 1440]
OUTCOME_POLL_INTERVAL = 300      # poll every 5 minutes
OUTCOME_LOOKBACK_HOURS = 48      # only consider tokens detected in last 48h


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
        m_5k            INTEGER DEFAULT 0,
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
    # Outcome snapshots — the foundation of the future ML training set.
    # One row per (mint, horizon_min). Populated by outcome_poll_loop()
    # when a token's age crosses a horizon boundary.
    #
    # Columns:
    #   horizon_min        60 / 360 / 1440 — the target age we're measuring at
    #   snapshot_ts        when we actually recorded it (may lag horizon slightly)
    #   mcap_at_snapshot   MCap USD at snapshot time
    #   peak_mcap_so_far   highest MCap observed between detection and horizon
    #   graduated_by_then  did the token reach DEX by this horizon?
    #   return_pct         (peak_mcap_so_far - initial_mcap_usd) / initial_mcap_usd * 100
    con.execute("""
    CREATE TABLE IF NOT EXISTS prelaunch_outcomes (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        mint              TEXT NOT NULL,
        horizon_min       INTEGER NOT NULL,
        snapshot_ts       INTEGER NOT NULL,
        mcap_at_snapshot  REAL,
        peak_mcap_so_far  REAL,
        graduated_by_then INTEGER DEFAULT 0,
        return_pct        REAL,
        UNIQUE(mint, horizon_min)
    )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_outcomes_mint ON prelaunch_outcomes(mint)"
    )
    con.commit()
    # Add new columns safely (ALTER TABLE ignored if column exists)
    new_cols = [
        ("eta_2h",           "INTEGER DEFAULT 0"),
        ("eta_1h",           "INTEGER DEFAULT 0"),
        ("eta_30m",          "INTEGER DEFAULT 0"),
        ("launch_score",     "INTEGER DEFAULT 0"),
        ("launch_tier",      "TEXT    DEFAULT 'COLD'"),
        ("has_twitter",      "INTEGER DEFAULT 0"),
        ("has_telegram",     "INTEGER DEFAULT 0"),
        ("has_website",      "INTEGER DEFAULT 0"),
        # Stable anchor MCap at the moment of first detection. Unlike peak_mcap_usd
        # (which gets overwritten as the token grows), this never changes after
        # insert — it's the denominator for every return calculation.
        ("initial_mcap_usd", "REAL    DEFAULT 0"),
        # m_5k replaced the old m_10k column (threshold was always $5K, not $10K).
        ("m_5k",             "INTEGER DEFAULT 0"),
        # Fired once when token is < IMMINENT_ETA_MIN minutes from graduation.
        ("m_imminent",       "INTEGER DEFAULT 0"),
    ]
    for col, typedef in new_cols:
        try:
            con.execute(f"ALTER TABLE prelaunch_tokens ADD COLUMN {col} {typedef}")
            con.commit()
        except Exception:
            pass
    # Migrate any existing m_10k data into m_5k for DBs created before the rename.
    try:
        con.execute(
            "UPDATE prelaunch_tokens SET m_5k = m_10k WHERE m_5k = 0 AND m_10k = 1"
        )
        con.commit()
    except Exception:
        pass
    con.close()

    # Sibling tables live in separate modules but share this DB. Init them
    # here so a single call to init_prelaunch_tables() sets up everything.
    init_creator_reputation_table()
    init_sniper_table()


# ──────────────────────────────────────────────────────────────────────────
# Token quality scorer  (runs at creation, zero extra API calls)
# ──────────────────────────────────────────────────────────────────────────

_RANDOM_RE = re.compile(r'^[a-z0-9]{1,4}$')   # "dsf", "fds", "5", "yn"

def _name_score(name: str) -> int:
    """0-20 points for token name quality."""
    if not name or len(name) < 2:
        return 0
    if _RANDOM_RE.match(name.strip().lower()):
        return 0                          # random garbage name
    if len(name) <= 2:
        return 0
    if ' ' in name:                       # multi-word = more thought
        return 20
    if any(c.isupper() for c in name[1:]):# has capitals = some branding
        return 15
    return 10


def score_new_token(ws_msg: dict, sol_price: float = 150.0) -> tuple:
    """
    Score a newly created pump.fun token from WebSocket event data.

    Signal weights (total 100):
      Twitter present   → +30  (most predictive of pumps)
      Telegram present  → +20  (community = buyers ready)
      Website present   → +10
      Quality name      → +20  (not random letters)
      Has description   → +10
      Strong initial buy→ +10

    Tiers:
      HOT  (≥55) → alert immediately
      WARM (≥30) → monitor closely, alert at first milestone
      COLD (<30) → batch digest only, probably dies
    """
    score   = 0
    reasons = []

    name        = (ws_msg.get("name")        or "").strip()
    description = (ws_msg.get("description") or "").strip()
    twitter     = (ws_msg.get("twitter")     or "").strip()
    telegram    = (ws_msg.get("telegram")    or "").strip()
    website     = (ws_msg.get("website")     or "").strip()
    mcap_sol    = float(ws_msg.get("marketCapSol", 0) or 0)
    mcap_usd    = mcap_sol * sol_price

    # Social presence — most predictive signal
    if twitter:
        score += 30
        reasons.append(f"Twitter")
    if telegram:
        score += 20
        reasons.append("Telegram")
    if website:
        score += 10
        reasons.append("Website")

    # Metadata quality
    nq = _name_score(name)
    score += nq
    if nq > 0:
        reasons.append(f"Name: {name}")

    if description and len(description) > 25:
        score += 10
        reasons.append("Description")

    # Initial buy above bonding curve floor
    if mcap_usd >= 6_000:
        score += 10
        reasons.append(f"Buy-in: {_fmt(mcap_usd)}")
    elif mcap_usd >= 4_000:
        score += 5

    # Tier
    #   HOT  (≥50): Twitter OR Telegram + something = likely organized launch
    #   WARM (≥15): quality name, desc, or website = worth watching
    #   COLD (<15): random name, nothing set = 99% dead on arrival
    if score >= 50:
        tier = "HOT"
    elif score >= 15:
        tier = "WARM"
    else:
        tier = "COLD"

    return score, reasons, tier


# ──────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────

def _add(mint, name, symbol, creator, mcap_usd, sol_price,
         score=0, tier="COLD", has_twitter=0, has_telegram=0, has_website=0):
    now = int(time.time())
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR IGNORE INTO prelaunch_tokens
        (mint, name, symbol, creator, detected_ts, last_mcap_usd, peak_mcap_usd,
         initial_mcap_usd,
         last_checked_ts, sol_price, launch_score, launch_tier,
         has_twitter, has_telegram, has_website)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (mint, name, symbol, creator, now, mcap_usd, mcap_usd,
          mcap_usd,
          now, sol_price, score, tier, has_twitter, has_telegram, has_website))
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
        SELECT m_5k, m_30k, m_50k, m_grad,
               COALESCE(eta_2h,0), COALESCE(eta_1h,0), COALESCE(eta_30m,0),
               COALESCE(m_imminent,0)
        FROM prelaunch_tokens WHERE mint=?
    """, (mint,))
    row = cur.fetchone()
    con.close()
    if not row:
        return {}
    return {
        "m_5k":      bool(row[0]), "m_30k":    bool(row[1]),
        "m_50k":     bool(row[2]), "m_grad":   bool(row[3]),
        "eta_2h":    bool(row[4]), "eta_1h":   bool(row[5]), "eta_30m": bool(row[6]),
        "m_imminent": bool(row[7]),
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


def _velocity_trend(mint: str) -> tuple:
    """
    Returns (v_short, v_long) — velocity in USD/min over the last 5 min
    and last 20 min respectively.
    v_short > v_long * 1.3  → ACCELERATING (momentum building)
    v_short < v_long * 0.6  → SLOWING (momentum fading)
    Returns (-1, -1) when there is not enough data.
    """
    now = int(time.time())
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT mcap, ts FROM prelaunch_history WHERE mint=? AND ts > ? ORDER BY ts ASC",
        (mint, now - 1500),   # last 25 minutes
    )
    rows = cur.fetchall()
    con.close()

    if len(rows) < 3:
        return -1, -1

    cutoff_5m = now - 300
    recent = [(m, t) for m, t in rows if t >= cutoff_5m]
    older  = [(m, t) for m, t in rows if t < cutoff_5m]

    def _vel(pts):
        if len(pts) < 2:
            return -1.0
        elapsed = max(pts[-1][1] - pts[0][1], 1) / 60.0
        return (pts[-1][0] - pts[0][0]) / elapsed

    return _vel(recent), _vel(older)


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
# Monitoring loop — priority-based (fresh tokens checked every 30s)
# ──────────────────────────────────────────────────────────────────────────

def _check_token(t: dict):
    """Check one token's MCap and fire any milestone/ETA alerts."""
    mint   = t["mint"]
    name   = t["name"]
    symbol = t["symbol"]

    mcap = _fetch_mcap(mint)
    if mcap <= 0:
        return

    _update(mint, mcap)
    f       = _flags(mint)
    age_min = int((time.time() - t["detected_ts"]) / 60)
    eta     = _eta_minutes(mint, mcap)
    eta_str = f"~{eta}min to DEX" if eta > 0 else ""

    # ── MCap milestones (age-filtered to avoid stale alerts) ──
    if mcap >= GRADUATION_MCAP and not f.get("m_grad"):
        _mark(mint, "m_grad")
        _mark(mint, "graduated")
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE prelaunch_tokens SET graduated=1 WHERE mint=?", (mint,))
        con.commit()
        con.close()
        _alert(
            f"GRADUATED TO DEX!\n\n"
            f"{name} ({symbol.upper()}) just hit {_fmt(mcap)} MCap\n"
            f"Now launching on Raydium!\n"
            f"Age: {age_min}m\n"
            f"Chart: https://dexscreener.com/solana/{mint}"
        )

    elif mcap >= 50_000 and not f.get("m_50k"):
        _mark(mint, "m_50k")
        if age_min <= 180:
            _alert(
                f"APPROACHING DEX LAUNCH!\n\n"
                f"{name} ({symbol.upper()})\n"
                f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                f"{eta_str}\n"
                f"Close to $69K graduation!\n"
                f"https://pump.fun/{mint}"
            )

    elif mcap >= 30_000 and not f.get("m_30k"):
        _mark(mint, "m_30k")
        if age_min <= 120:
            _alert(
                f"PRE-LAUNCH: Strong Momentum\n\n"
                f"{name} ({symbol.upper()})\n"
                f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                f"{eta_str}\n"
                f"https://pump.fun/{mint}"
            )

    elif mcap >= 5_000 and not f.get("m_5k"):
        _mark(mint, "m_5k")
        if age_min <= 60:
            # Fetch score from DB for richer alert
            con  = sqlite3.connect(DB_PATH)
            cur  = con.cursor()
            cur.execute(
                "SELECT launch_score, launch_tier, has_twitter, has_telegram FROM prelaunch_tokens WHERE mint=?",
                (mint,)
            )
            row = cur.fetchone()
            con.close()
            sc   = row[0] if row else 0
            tier = row[1] if row else "?"
            twit = "𝕏" if (row and row[2]) else ""
            tele = "✈️" if (row and row[3]) else ""
            soc  = " ".join(filter(None, [twit, tele])) or "—"

            # Sniper check — only worth the Helius credits now that the
            # token has proven it can get off the floor. If the top 5
            # wallets already own most of the supply, suppress the alert
            # entirely (tokens dominated by snipers rarely moon).
            sniper = check_sniper_concentration(mint)
            if sniper.get("sniped"):
                print(
                    f"[PRELAUNCH] Suppressing $5K alert for {symbol} — "
                    f"sniped (top5={sniper['top5_pct']}%)"
                )
                return
            sniper_line = format_sniper_line(sniper)

            _alert(
                f"PRE-LAUNCH: Gaining Traction!\n\n"
                f"{name} ({symbol.upper()})\n"
                f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                f"Score: {sc}/100 [{tier}] | Socials: {soc}\n"
                + (sniper_line + "\n" if sniper_line else "")
                + f"https://pump.fun/{mint}"
            )

    # ── Imminent DEX launch — fires on ETA threshold OR MCap proximity ──
    imminent_by_eta  = (0 < eta <= IMMINENT_ETA_MIN)
    imminent_by_mcap = (mcap >= NEAR_GRAD_MCAP)   # 77%+ to graduation = imminent
    if (mcap >= IMMINENT_MCAP_USD and (imminent_by_eta or imminent_by_mcap)
            and not f.get("m_imminent")):
        _mark(mint, "m_imminent")

        v_short, v_long = _velocity_trend(mint)
        if v_short > 0 and v_long > 0:
            if v_short >= v_long * 1.3:
                accel_tag = " ACCELERATING"
            elif v_short < v_long * 0.6:
                accel_tag = " SLOWING"
            else:
                accel_tag = ""
            vel_line = f"Speed: +${v_short:.0f}/min{accel_tag}\n"
        elif v_short > 0:
            vel_line = f"Speed: +${v_short:.0f}/min\n"
        else:
            vel_line = ""

        con = sqlite3.connect(DB_PATH)
        cur = con.cursor()
        cur.execute(
            "SELECT launch_score, launch_tier, has_twitter, has_telegram, creator FROM prelaunch_tokens WHERE mint=?",
            (mint,)
        )
        row = cur.fetchone()
        con.close()
        sc      = row[0] if row else 0
        tier    = row[1] if row else "?"
        twit    = "𝕏" if (row and row[2]) else ""
        tele    = "✈️" if (row and row[3]) else ""
        creator = row[4] if row and row[4] else ""
        soc     = " ".join(filter(None, [twit, tele])) or "—"

        cstats = get_creator_stats(creator) if creator else {}
        ctier  = cstats.get("tier", "UNKNOWN")
        creator_line = (
            f"\nCreator: WINNER ({cstats.get('graduations',0)}/"
            f"{cstats.get('total_launches',0)} grads)"
            if ctier == "WINNER" else ""
        )

        pct    = min(mcap / GRADUATION_MCAP * 100, 100)
        filled = int(pct / 10)
        bar    = "#" * filled + "-" * (10 - filled)

        sniper      = check_sniper_concentration(mint)
        sniper_line = format_sniper_line(sniper)

        eta_label = f"~{eta}min to Raydium" if eta > 0 else f"{pct:.0f}% to graduation"
        _alert(
            f"IMMINENT DEX LAUNCH — {eta_label}!\n\n"
            f"{name} ({symbol.upper()})\n"
            f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
            f"[{bar}] {pct:.0f}% to graduation\n"
            f"{vel_line}"
            f"Score: {sc}/100 [{tier}] | Socials: {soc}"
            + creator_line
            + ("\n" + sniper_line if sniper_line else "")
            + f"\n\nBuy NOW before DEX listing:\nhttps://pump.fun/{mint}"
        )

    # ── ETA countdown alerts ──
    if eta > 0:
        if eta <= 30 and not f.get("eta_30m"):
            _mark(mint, "eta_30m")
            _alert(
                f"LAUNCHING IN ~30 MINUTES!\n\n"
                f"{name} ({symbol.upper()})\n"
                f"MCap: {_fmt(mcap)} | Age: {age_min}m\n"
                f"Buy on pump.fun NOW:\n"
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


def monitor_loop():
    """
    Three-tier priority monitor:
      - Critical tokens (MCap >= $35K, near graduation) → every 15 seconds
      - Fresh tokens   (< 15 min old, MCap < $35K)     → every 30 seconds
      - Veteran tokens (>= 15 min old, MCap < $35K)    → every 2 minutes

    Critical tier is the key innovation: a token at $50K with 10 min ETA
    is more time-sensitive than a brand-new token at $2K.
    """
    time.sleep(30)   # short initial wait

    veteran_last_check: dict  = {}   # mint → last check timestamp
    critical_last_check: dict = {}   # mint → last check timestamp

    while True:
        try:
            now    = time.time()
            tokens = get_active_tokens()

            critical = [t for t in tokens
                        if t["last_mcap_usd"] >= IMMINENT_MCAP_USD]
            fresh    = [t for t in tokens
                        if t["last_mcap_usd"] < IMMINENT_MCAP_USD
                        and (now - t["detected_ts"]) / 60 < FRESH_TOKEN_MINUTES]
            veterans = [t for t in tokens
                        if t["last_mcap_usd"] < IMMINENT_MCAP_USD
                        and (now - t["detected_ts"]) / 60 >= FRESH_TOKEN_MINUTES]

            # ── Critical: check every 15 s regardless of age ──
            for t in critical:
                mint = t["mint"]
                if now - critical_last_check.get(mint, 0) >= MONITOR_CRITICAL_SECS:
                    critical_last_check[mint] = now
                    try:
                        _check_token(t)
                    except Exception as e:
                        print(f"[PRELAUNCH] Critical check error {mint[:8]}: {e}")
                    time.sleep(0.2)

            # ── Fresh: check every pass (every 30 s) ──
            for t in fresh:
                try:
                    _check_token(t)
                except Exception as e:
                    print(f"[PRELAUNCH] Check error {t['mint'][:8]}: {e}")
                time.sleep(0.3)

            # ── Veterans: check only if 2+ min since last check ──
            for t in veterans:
                mint = t["mint"]
                if now - veteran_last_check.get(mint, 0) >= MONITOR_VETERAN_SECS:
                    veteran_last_check[mint] = now
                    try:
                        _check_token(t)
                    except Exception as e:
                        print(f"[PRELAUNCH] Check error {mint[:8]}: {e}")
                    time.sleep(0.5)

        except Exception as e:
            print(f"[PRELAUNCH] Monitor error: {e}")

        time.sleep(MONITOR_FRESH_SECS)   # 30 second main loop


# ──────────────────────────────────────────────────────────────────────────
# Outcome logging — foundation of the future ML training set
# ──────────────────────────────────────────────────────────────────────────
#
# For every token we detect on pump.fun, we log the outcome at fixed
# horizons (+1h, +6h, +24h) regardless of whether it's still "active" in
# the monitor loop. This gives us labeled data to train on later:
#
#   features: everything in prelaunch_tokens at detection time
#             (launch_score, has_twitter, has_telegram, has_website, name, ...)
#   labels:   prelaunch_outcomes.return_pct / graduated_by_then at +Xh
#
# The monitor loop stops tracking tokens after TRACK_HOURS (6h), so for
# longer horizons we do a one-shot MCap fetch here.

def _get_detected_initial_mcap(mint: str) -> float:
    """Return the anchor MCap used to compute returns for a token.

    Prefer initial_mcap_usd (stable snapshot at insert time). Fall back to
    the earliest prelaunch_history row if the column is missing or zero
    (older rows inserted before this column existed).
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT COALESCE(initial_mcap_usd, 0) FROM prelaunch_tokens WHERE mint=?",
        (mint,),
    )
    row = cur.fetchone()
    initial = float(row[0]) if row and row[0] else 0.0

    if initial <= 0:
        cur.execute(
            "SELECT mcap FROM prelaunch_history WHERE mint=? ORDER BY ts ASC LIMIT 1",
            (mint,),
        )
        first = cur.fetchone()
        if first and first[0]:
            initial = float(first[0])
            # Backfill so we don't repeat this lookup.
            con.execute(
                "UPDATE prelaunch_tokens SET initial_mcap_usd=? WHERE mint=?",
                (initial, mint),
            )
            con.commit()
    con.close()
    return initial


def _outcomes_due() -> list:
    """Find (mint, horizon_min) pairs whose snapshot deadline has passed
    and which don't yet have a row in prelaunch_outcomes.
    """
    now = int(time.time())
    cutoff = now - OUTCOME_LOOKBACK_HOURS * 3600

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT mint, detected_ts, last_mcap_usd, peak_mcap_usd, graduated
        FROM prelaunch_tokens
        WHERE detected_ts > ?
    """, (cutoff,))
    tokens = cur.fetchall()

    cur.execute("SELECT mint, horizon_min FROM prelaunch_outcomes")
    already = {(m, h) for m, h in cur.fetchall()}
    con.close()

    due = []
    for mint, detected_ts, last_mcap, peak_mcap, graduated in tokens:
        age_min = (now - detected_ts) / 60.0
        for h in OUTCOME_HORIZONS_MIN:
            if age_min >= h and (mint, h) not in already:
                due.append({
                    "mint":          mint,
                    "horizon_min":   h,
                    "detected_ts":   detected_ts,
                    "last_mcap_usd": last_mcap or 0.0,
                    "peak_mcap_usd": peak_mcap or 0.0,
                    "graduated":     bool(graduated),
                    "age_min":       age_min,
                })
    # Snapshot the longest-overdue horizons first.
    due.sort(key=lambda d: d["age_min"] - d["horizon_min"], reverse=True)
    return due


def _record_outcome(mint: str, horizon_min: int,
                    current_mcap: float, peak_mcap: float,
                    graduated: bool) -> None:
    """Insert one outcome row. Idempotent via UNIQUE(mint, horizon_min)."""
    initial = _get_detected_initial_mcap(mint)
    return_pct = None
    if initial and initial > 0:
        return_pct = ((peak_mcap or 0.0) - initial) / initial * 100.0

    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR IGNORE INTO prelaunch_outcomes
        (mint, horizon_min, snapshot_ts,
         mcap_at_snapshot, peak_mcap_so_far,
         graduated_by_then, return_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (mint, horizon_min, int(time.time()),
          float(current_mcap or 0.0), float(peak_mcap or 0.0),
          1 if graduated else 0, return_pct))
    con.commit()
    con.close()


def _refresh_mcap_for_old_token(d: dict) -> tuple:
    """For tokens past the active-monitor window (6h), the DB's
    last_mcap_usd / peak_mcap_usd are stale. Do a one-shot fetch and
    update the peak before recording the outcome.
    """
    age_hours = d["age_min"] / 60.0
    current = d["last_mcap_usd"]
    peak = d["peak_mcap_usd"]

    if age_hours <= TRACK_HOURS:
        # Active monitor is keeping these fresh enough.
        return current, peak

    fresh = _fetch_mcap(d["mint"])
    if fresh > 0:
        current = fresh
        peak = max(peak, fresh)
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            UPDATE prelaunch_tokens
               SET last_mcap_usd = ?,
                   peak_mcap_usd = MAX(peak_mcap_usd, ?),
                   last_checked_ts = ?
             WHERE mint = ?
        """, (fresh, fresh, int(time.time()), d["mint"]))
        con.execute(
            "INSERT INTO prelaunch_history (mint, mcap, ts) VALUES (?,?,?)",
            (d["mint"], fresh, int(time.time())),
        )
        con.commit()
        con.close()
    return current, peak


def record_outcomes_once() -> int:
    """Record all outcome snapshots that are currently due. Returns count.

    After recording, incrementally refreshes the reputation of each
    affected creator so their tier is always based on the latest outcomes.
    """
    due = _outcomes_due()
    recorded = 0
    touched_creators: set = set()
    for d in due:
        try:
            current, peak = _refresh_mcap_for_old_token(d)
            _record_outcome(
                d["mint"], d["horizon_min"],
                current, peak, d["graduated"],
            )
            recorded += 1
            # Track which creator's stats need refreshing.
            con = sqlite3.connect(DB_PATH)
            cur = con.cursor()
            cur.execute("SELECT creator FROM prelaunch_tokens WHERE mint=?", (d["mint"],))
            row = cur.fetchone()
            con.close()
            if row and row[0]:
                touched_creators.add(row[0])
            # Gentle on APIs when the backlog is large.
            time.sleep(0.3)
        except Exception as e:
            print(f"[OUTCOMES] Record error {d['mint'][:8]} @ {d['horizon_min']}m: {e}")

    for c in touched_creators:
        try:
            recompute_creator(c)
        except Exception as e:
            print(f"[OUTCOMES] Creator recompute error {c[:8]}: {e}")
    if touched_creators:
        print(f"[OUTCOMES] Refreshed reputation for {len(touched_creators)} creator(s)")
    return recorded


def outcome_poll_loop():
    """Background loop: every OUTCOME_POLL_INTERVAL seconds, snapshot any
    tokens that have crossed a horizon boundary since last poll.
    """
    time.sleep(60)  # let the WS listener warm up first
    while True:
        try:
            n = record_outcomes_once()
            if n:
                print(f"[OUTCOMES] Recorded {n} outcome snapshot(s)")
        except Exception as e:
            print(f"[OUTCOMES] Loop error: {e}")
        time.sleep(OUTCOME_POLL_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────
# Creator reputation — periodic full recompute
# ──────────────────────────────────────────────────────────────────────────

CREATOR_RECOMPUTE_INTERVAL = 3600   # hourly — catches late outcomes


def creator_recompute_loop():
    """Hourly full sweep of creator_reputation to pick up any drift the
    incremental-update path in record_outcomes_once() might have missed
    (e.g. edge cases where a creator's token was deleted, or a creator's
    stats rolled across a tier boundary thanks to a live peak update).
    """
    time.sleep(300)  # stagger 5m after startup
    while True:
        try:
            n = recompute_all_creators()
            if n:
                print(f"[CREATOR_REP] Recomputed {n} creator(s)")
        except Exception as e:
            print(f"[CREATOR_REP] Loop error: {e}")
        time.sleep(CREATOR_RECOMPUTE_INTERVAL)


# ──────────────────────────────────────────────────────────────────────────
# ML retrainer — automatic when enough data exists
# ──────────────────────────────────────────────────────────────────────────

RETRAIN_INTERVAL_HOURS = 12   # re-evaluate twice a day; trainer itself
                              # aborts cheaply if data requirements unmet


def retrain_loop():
    """Periodically attempt to (re)train the pump.fun XGBoost model.

    pumpfun_trainer.train_pumpfun_model() has hard guards on dataset size,
    distinct days, and positive class count — it simply returns a reason
    dict and skips saving if prerequisites aren't met. That means this
    loop is safe to run from day 1: it becomes a no-op cost of a single
    DataFrame build every 12h until enough outcome data has accumulated,
    at which point it silently starts producing models.
    """
    # Wait long enough after startup that the outcome logger has had a
    # chance to backfill anything due.
    time.sleep(900)
    while True:
        try:
            from app.pumpfun_trainer import train_pumpfun_model
            report = train_pumpfun_model()
            if report.get("ok"):
                print(f"[RETRAIN] New model saved. Metrics: {report['metrics']}")
                # Hot-reload live_scorer so the next detected token uses it.
                try:
                    import app.live_scorer as ls
                    ls._loaded = False
                    ls._model = None
                    ls._features = None
                except Exception:
                    pass
            else:
                print(f"[RETRAIN] Skipped: {report.get('reason', 'unknown')}")
        except Exception as e:
            print(f"[RETRAIN] Loop error: {e}")
        time.sleep(RETRAIN_INTERVAL_HOURS * 3600)


# ──────────────────────────────────────────────────────────────────────────
# /stats command — rolling validation of the current heuristic
# ──────────────────────────────────────────────────────────────────────────

def format_stats() -> str:
    """Human-readable performance summary for the /stats Telegram command.

    Answers the only question that matters: is the current scoring
    actually predictive of outcomes? Breaks down graduation rate and
    median/max 6h return by launch_tier and shows the top creators.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("SELECT COUNT(*) FROM prelaunch_tokens")
    total_tokens = int(cur.fetchone()[0] or 0)

    cur.execute("""
        SELECT COUNT(*) FROM prelaunch_outcomes WHERE horizon_min = 360
    """)
    total_outcomes_6h = int(cur.fetchone()[0] or 0)

    # Per-tier stats joining the latest tier assignment with 6h outcomes.
    cur.execute("""
        SELECT t.launch_tier,
               COUNT(*) AS n,
               SUM(CASE WHEN o.graduated_by_then = 1 THEN 1 ELSE 0 END) AS grads,
               AVG(o.return_pct),
               MAX(o.return_pct)
        FROM prelaunch_tokens t
        INNER JOIN prelaunch_outcomes o
                ON o.mint = t.mint AND o.horizon_min = 360
        GROUP BY t.launch_tier
    """)
    tier_rows = cur.fetchall()

    # Sniper-suppression effectiveness.
    cur.execute("""
        SELECT
          SUM(CASE WHEN s.sniped = 1 THEN 1 ELSE 0 END) AS sniped,
          SUM(CASE WHEN s.sniped = 0 AND s.label='CLEAN' THEN 1 ELSE 0 END) AS clean,
          COUNT(*) AS total
        FROM sniper_checks s
    """)
    srow = cur.fetchone() or (0, 0, 0)
    con.close()

    lines = [
        "Stats — AI Token Finder Performance",
        "",
        f"Total tokens tracked: {total_tokens}",
        f"Outcomes recorded (+6h): {total_outcomes_6h}",
    ]
    lines.append(f"ML model loaded: {'YES' if model_is_available() else 'no (still accumulating data)'}")
    lines.append("")
    lines.append("By Launch Tier (6h horizon):")
    if not tier_rows:
        lines.append("  (no outcomes yet — keep the bot running)")
    else:
        for tier, n, grads, avg_ret, max_ret in tier_rows:
            grad_rate = (grads / n * 100) if n else 0
            avg_ret = avg_ret if avg_ret is not None else 0
            max_ret = max_ret if max_ret is not None else 0
            lines.append(
                f"  {tier}: n={n} | grads={grads} ({grad_rate:.0f}%) | "
                f"avg={avg_ret:+.0f}% | max={max_ret:+.0f}%"
            )

    if srow and srow[2]:
        sniped, clean, total = srow
        lines.append("")
        lines.append(
            f"Sniper checks: {total} total | sniped={sniped} | clean={clean}"
        )

    winners = top_creators(limit=5, min_launches=2)
    if winners:
        lines.append("")
        lines.append("Top Creators (min 2 launches):")
        for w in winners:
            c = (w["creator"] or "")[:8] + "…"
            lines.append(
                f"  {c} [{w['tier']}] | launches={w['total_launches']} | "
                f"grads={w['graduations']} | peak=${w['max_peak_mcap_usd']:,.0f}"
            )

    lines.append("")
    lines.append("A meaningful HOT tier should beat WARM beat COLD.")
    lines.append("If they're indistinguishable, the heuristic is noise.")
    return "\n".join(lines)


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

                        mint     = msg.get("mint", "")
                        name     = msg.get("name", "Unknown")
                        symbol   = msg.get("symbol", "?")
                        creator  = msg.get("traderPublicKey", "")
                        mcap_sol = float(msg.get("marketCapSol", 0) or 0)

                        if not mint:
                            continue

                        sol      = _cached_sol_price()
                        mcap_usd = mcap_sol * sol

                        # ── Fetch social metadata from pump.fun API ──
                        # WebSocket fires instantly on-chain, but pump.fun REST API
                        # needs ~2s to index the new mint. Without this delay we get
                        # empty socials even for organized launches with Twitter set.
                        await asyncio.sleep(2)
                        try:
                            api_resp = await asyncio.wait_for(
                                asyncio.to_thread(
                                    lambda m=mint: requests.get(
                                        f"https://frontend-api.pump.fun/coins/{m}",
                                        headers={"User-Agent": "Mozilla/5.0",
                                                 "Referer": "https://pump.fun/"},
                                        timeout=5, verify=False,
                                    )
                                ),
                                timeout=6,
                            )
                            if api_resp.status_code == 200:
                                api_data = api_resp.json()
                                msg["twitter"]     = api_data.get("twitter")     or ""
                                msg["telegram"]    = api_data.get("telegram")    or ""
                                msg["website"]     = api_data.get("website")     or ""
                                msg["description"] = api_data.get("description") or ""
                                has_any = bool(msg["twitter"] or msg["telegram"] or msg["website"])
                                print(
                                    f"[PRELAUNCH] API OK: "
                                    f"tw={bool(msg['twitter'])} "
                                    f"tg={bool(msg['telegram'])} "
                                    f"web={bool(msg['website'])} "
                                    f"desc={len(msg['description'])}c"
                                    + (" ← HAS SOCIALS" if has_any else "")
                                )
                            else:
                                print(f"[PRELAUNCH] API {api_resp.status_code} for {mint[:8]}")
                        except asyncio.TimeoutError:
                            print(f"[PRELAUNCH] API timeout for {mint[:8]}")
                        except Exception as ex:
                            print(f"[PRELAUNCH] API error {mint[:8]}: {ex}")

                        # ── Score token at creation ──
                        score, reasons, tier = score_new_token(msg, sol)

                        # Creator-wallet reputation boost. A wallet that has
                        # graduated tokens before is the single strongest
                        # pre-launch signal available on-chain — stronger
                        # than any social metadata.
                        cstats = get_creator_stats(creator) if creator else {}
                        ctier = cstats.get("tier", "UNKNOWN")
                        if ctier == "WINNER":
                            tier = "HOT"
                            reasons.insert(
                                0,
                                f"Creator WINNER ({cstats.get('graduations',0)}/"
                                f"{cstats.get('total_launches',0)} grads)"
                            )
                            score = min(100, score + 25)
                        elif ctier == "RUGGER":
                            # Serial rugger — never alert, always cold.
                            tier = "COLD"
                            reasons.insert(0, "Creator RUGGER — suppressed")

                        # Optional ML tier adjustment. Only fires once a trained
                        # model exists on disk; no-op otherwise.
                        ml_ctx = {
                            "mint":             mint,
                            "name":             name,
                            "creator":          creator,
                            "initial_mcap_usd": mcap_usd,
                            "launch_score":     score,
                            "launch_tier":      tier,
                            "has_twitter":      1 if msg.get("twitter")  else 0,
                            "has_telegram":     1 if msg.get("telegram") else 0,
                            "has_website":      1 if msg.get("website")  else 0,
                        }
                        tier, ml_prob = ml_adjust_tier(tier, ml_ctx)
                        if ml_prob is not None:
                            reasons.append(f"ML={ml_prob:.2f}")

                        print(
                            f"[PRELAUNCH] New: {name} ({symbol}) "
                            f"MCap ${mcap_usd:,.0f} | Score:{score} [{tier}]"
                            f"{' | CreatorTier:' + ctier if ctier != 'UNKNOWN' else ''}"
                        )

                        _add(mint, name, symbol, creator, mcap_usd, sol,
                             score=score, tier=tier,
                             has_twitter=1 if msg.get("twitter") else 0,
                             has_telegram=1 if msg.get("telegram") else 0,
                             has_website=1 if msg.get("website") else 0)

                        global _batch_buffer, _batch_last_ts
                        now = time.time()

                        if tier == "HOT":
                            # Immediate alert — has social presence + quality name
                            social = []
                            if msg.get("twitter"):  social.append("𝕏 Twitter")
                            if msg.get("telegram"): social.append("✈️ Telegram")
                            if msg.get("website"):  social.append("🌐 Website")
                            social_str = " | ".join(social) if social else "—"

                            _alert(
                                f"HOT NEW LAUNCH!  Score:{score}/100\n\n"
                                f"{name} ({symbol.upper()})\n"
                                f"MCap: {_fmt(mcap_usd)} | Just created\n"
                                f"Socials: {social_str}\n"
                                f"Signals: {', '.join(reasons[:4])}\n\n"
                                f"Buy NOW (seconds old):\n"
                                f"https://pump.fun/{mint}"
                            )

                        elif tier == "WARM":
                            # Worth watching — fast monitor (30s) will alert at $5K
                            _batch_buffer.append({
                                "name": name, "symbol": symbol,
                                "mcap": mcap_usd, "mint": mint,
                                "score": score, "tier": "WARM",
                            })

                        else:
                            # COLD — random junk name + no socials. Track silently,
                            # never alert. These tokens die at $2-3K (99%+ of all mints).
                            pass

                        # ── Batch digest — every 10 min, top scored WARM tokens ──
                        if now - _batch_last_ts >= BATCH_INTERVAL and _batch_buffer:
                            _batch_last_ts = now

                            # Deduplicate by name (copy scams reuse the same name)
                            seen_names: dict = {}
                            for t in _batch_buffer:
                                n = t["name"].lower()
                                if n not in seen_names or t["score"] > seen_names[n]["score"]:
                                    seen_names[n] = t
                            unique = list(seen_names.values())
                            _batch_buffer.clear()

                            # Sort by score, show top 5
                            top = sorted(unique, key=lambda x: x["score"], reverse=True)[:5]
                            if top:
                                tier_icon = {"HOT": "🔥", "WARM": "🟡", "COLD": "❄️"}
                                lines = [f"New Launches — {len(top)} Promising Tokens\n"]
                                for t in top:
                                    icon = tier_icon.get(t["tier"], "")
                                    lines.append(
                                        f"{icon} {t['name']} ({t['symbol'].upper()})"
                                        f" — {_fmt(t['mcap'])} | Score:{t['score']}/100\n"
                                        f"  https://pump.fun/{t['mint']}"
                                    )
                                lines.append("\nDYOR — unverified. Monitor these closely.")
                                _alert("\n".join(lines))

                    except Exception as e:
                        print(f"[PRELAUNCH] Parse error: {e}")

        except Exception as e:
            print(f"[PRELAUNCH] WS error: {e} — retry in 5s")
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────────────────
# Graduation Zone Scanner — WebSocket trade feed (replaces broken REST API)
# ──────────────────────────────────────────────────────────────────────────
#
# pump.fun's REST API is Cloudflare-blocked globally. Instead we open a
# SECOND WebSocket connection to pumpportal.fun and subscribe to ALL token
# trade events. Every buy/sell carries the current marketCapSol, so we see
# the live MCap for every actively-traded token.
#
# When a trade comes in for a token we don't know:
#   MCap in [$30K, $65K) → fetch metadata from pump.fun, add to DB.
#                           monitor_loop picks it up within 15 seconds.
#   MCap outside zone    → ignore (we only care about graduation-zone tokens)
#
# For tokens we already track, we update their MCap in real time, giving
# _eta_minutes() more data points and improving ETA accuracy.

# Deduplicate discovery fetches — don't hammer pump.fun API for the same
# unknown mint repeatedly while we're waiting for the fetch to complete.
_zone_discovery_pending: set = set()


async def _fetch_token_metadata(mint: str) -> dict:
    """One-shot pump.fun API fetch for name/symbol/socials of a discovered token."""
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: requests.get(
                    f"https://frontend-api.pump.fun/coins/{mint}",
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://pump.fun/"},
                    timeout=6, verify=False,
                )
            ),
            timeout=8,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}


async def _ingest_trade_token(mint: str, mcap_usd: float, sol_price: float) -> None:
    """Discover and add a graduation-zone token found via the trade feed."""
    if mint in _zone_discovery_pending:
        return
    _zone_discovery_pending.add(mint)
    try:
        # Small delay so pump.fun API has time to index the token
        await asyncio.sleep(1)
        meta = await _fetch_token_metadata(mint)

        name    = (meta.get("name")    or "Unknown").strip()
        symbol  = (meta.get("symbol")  or "?").strip()
        creator = (meta.get("creator") or "").strip()

        ws_msg = {
            "name":         name,
            "description":  (meta.get("description") or ""),
            "twitter":      (meta.get("twitter")     or ""),
            "telegram":     (meta.get("telegram")    or ""),
            "website":      (meta.get("website")     or ""),
            "marketCapSol": mcap_usd / sol_price if sol_price > 0 else 0,
        }
        score, _reasons, tier = score_new_token(ws_msg, sol_price)

        # Tokens already deep in the graduation zone get at least WARM
        if tier == "COLD" and mcap_usd >= IMMINENT_MCAP_USD:
            tier = "WARM"

        _add(mint, name, symbol, creator, mcap_usd, sol_price,
             score=score, tier=tier,
             has_twitter=1  if ws_msg["twitter"]  else 0,
             has_telegram=1 if ws_msg["telegram"] else 0,
             has_website=1  if ws_msg["website"]  else 0)
        _update(mint, mcap_usd)   # seed history point for ETA calculation

        print(
            f"[GRAD_ZONE] Discovered via trade: {name} ({symbol}) "
            f"MCap={_fmt(mcap_usd)} Score={score} [{tier}]"
        )
    except Exception as e:
        print(f"[GRAD_ZONE] Ingest error {mint[:8]}: {e}")
    finally:
        _zone_discovery_pending.discard(mint)


def _is_known(mint: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT 1 FROM prelaunch_tokens WHERE mint=?", (mint,))
    found = cur.fetchone() is not None
    con.close()
    return found


async def _trade_feed_listen():
    """
    Second WebSocket connection — subscribes to ALL pump.fun token trades.
    Each trade event contains marketCapSol, so we see live MCap without
    any REST API call. Unknown tokens in the graduation zone are added to DB.
    """
    while True:
        try:
            print("[GRAD_ZONE] Connecting trade feed WebSocket...")
            async with websockets.connect(
                PUMPFUN_WS,
                additional_headers={
                    "Origin":     "https://pump.fun",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                },
                ping_interval=20,
                ping_timeout=10,
            ) as ws:
                # subscribeTokenTrade without keys = all pump.fun trades
                await ws.send(json.dumps({"method": "subscribeTokenTrade"}))
                print("[GRAD_ZONE] Trade feed subscribed — watching all pump.fun trades")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        txtype = msg.get("txType", "")
                        if txtype not in ("buy", "sell"):
                            continue

                        mint     = msg.get("mint", "")
                        mcap_sol = float(msg.get("marketCapSol") or 0)
                        if not mint or mcap_sol <= 0:
                            continue

                        sol      = _cached_sol_price()
                        mcap_usd = mcap_sol * sol

                        if _is_known(mint):
                            # Update MCap for tokens we already track
                            if mcap_usd > 0:
                                _update(mint, mcap_usd)
                        elif GRAD_ZONE_MIN_MCAP <= mcap_usd < GRAD_ZONE_MAX_MCAP:
                            # Unknown token in graduation zone — discover it
                            asyncio.ensure_future(_ingest_trade_token(mint, mcap_usd, sol))

                    except Exception as e:
                        print(f"[GRAD_ZONE] Parse error: {e}")

        except Exception as e:
            print(f"[GRAD_ZONE] Trade feed error: {e} — retry in 5s")
            await asyncio.sleep(5)


def _fetch_graduation_zone() -> list:
    """
    Return currently tracked tokens in the graduation zone for /gradzone command.
    Uses our own DB (populated by the trade feed) — no external API needed.
    """
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cutoff = int(time.time()) - TRACK_HOURS * 3600
    cur.execute("""
        SELECT mint, name, symbol, last_mcap_usd, has_twitter, has_telegram,
               launch_score, launch_tier, detected_ts
        FROM prelaunch_tokens
        WHERE last_mcap_usd >= ? AND last_mcap_usd < ?
          AND graduated = 0
          AND detected_ts > ?
        ORDER BY last_mcap_usd DESC
    """, (GRAD_ZONE_MIN_MCAP, GRAD_ZONE_MAX_MCAP, cutoff))
    rows = cur.fetchall()
    con.close()
    return [
        {
            "mint":         r[0], "name":       r[1], "symbol":      r[2],
            "usd_market_cap": r[3],
            "twitter":      r[4], "telegram":   r[5],
            "launch_score": r[6], "launch_tier": r[7],
            "detected_ts":  r[8],
        }
        for r in rows
    ]


def graduation_zone_loop():
    """Run the async trade-feed listener in its own event loop (background thread)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_trade_feed_listen())


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
    # Outcome logger — independent of the active-monitor window so it can
    # capture 24h-horizon labels for tokens that have long since stopped
    # being tracked. This table is what future ML training reads from.
    threading.Thread(target=outcome_poll_loop, daemon=True).start()
    # Creator reputation — hourly full sweep so tier assignments reflect
    # the latest outcomes even when incremental updates missed an edge case.
    threading.Thread(target=creator_recompute_loop, daemon=True).start()
    # ML retrainer — cheap no-op until ~300 rows and 7 distinct days of
    # data have accumulated; produces a model automatically once they have.
    threading.Thread(target=retrain_loop, daemon=True).start()
    # Graduation zone scanner — finds tokens already mid-flight that the WS missed.
    threading.Thread(target=graduation_zone_loop, daemon=True).start()
    print("[PRELAUNCH] Tracker started (outcomes + creator-rep + ML retrainer + grad-zone scanner)")


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


def format_imminent() -> str:
    """Format the /imminent command — tokens minutes away from DEX listing."""
    tokens = get_approaching_tokens(max_eta_hours=0.5)   # ETA < 30 min

    if not tokens:
        return (
            "Imminent DEX Launches\n\n"
            "No tokens within 30 minutes of graduation right now.\n\n"
            "Tokens appear here when their bonding curve velocity puts them\n"
            "< 30 min from the $69K Raydium listing threshold.\n"
            "Check /upcoming for tokens within 6 hours."
        )

    lines = [f"Imminent DEX Launches — {len(tokens)} token(s)\n"]
    for i, t in enumerate(tokens[:6], 1):
        eta    = t["eta_minutes"]
        pct    = t["progress_pct"]
        filled = int(pct / 10)
        bar    = "#" * filled + "-" * (10 - filled)
        vel    = t["velocity_usd_m"]
        age_min = int((time.time() - t["detected_ts"]) / 60)

        v_short, v_long = _velocity_trend(t["mint"])
        if v_short > 0 and v_long > 0 and v_short >= v_long * 1.3:
            accel = " ACCELERATING"
        elif v_short > 0 and v_long > 0 and v_short < v_long * 0.6:
            accel = " SLOWING"
        else:
            accel = ""

        lines.append(
            f"{i}. {t['name']} ({t['symbol'].upper()})\n"
            f"   MCap: {_fmt(t['last_mcap_usd'])} | ETA: ~{eta}min | Age: {age_min}m\n"
            f"   Speed: +${vel:.0f}/min{accel}\n"
            f"   [{bar}] {pct:.0f}% to DEX\n"
            f"   Buy: https://pump.fun/{t['mint']}\n"
        )

    lines.append("Buy on pump.fun BEFORE listing — price pumps at graduation!")
    return "\n".join(lines)


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
