"""
Persistent coin memory across scan cycles.
Tracks how many times a coin appears with good signals,
score trends, and first/last seen timestamps.
"""
import time
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

# Minimum consecutive scans to be considered "consistent"
CONSISTENCY_THRESHOLD = 3


def _conn():
    return sqlite3.connect(DB_PATH)


def init_memory_table():
    con = _conn()
    con.execute("""
    CREATE TABLE IF NOT EXISTS coin_memory (
        symbol         TEXT PRIMARY KEY,
        first_seen_ts  INTEGER NOT NULL,
        last_seen_ts   INTEGER NOT NULL,
        scan_count     INTEGER NOT NULL DEFAULT 1,
        consecutive    INTEGER NOT NULL DEFAULT 1,
        avg_score      REAL    NOT NULL DEFAULT 0,
        last_score     REAL    NOT NULL DEFAULT 0,
        last_signal    TEXT    NOT NULL DEFAULT '',
        last_price     REAL    NOT NULL DEFAULT 0,
        bullish_count  INTEGER NOT NULL DEFAULT 0,
        bearish_count  INTEGER NOT NULL DEFAULT 0
    )
    """)
    con.commit()
    con.close()


def update_coin_memory(symbol: str, score: float, signal: str, price: float) -> None:
    symbol = symbol.upper().strip()
    now    = int(time.time())
    is_bullish = 1 if signal in ("Strong Consider", "Breakout Watch", "Momentum Watch") else 0
    is_bearish = 1 if signal in ("Avoid",) else 0

    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT scan_count, avg_score, last_seen_ts, bullish_count, bearish_count, consecutive FROM coin_memory WHERE symbol=?", (symbol,))
    row = cur.fetchone()

    if row is None:
        cur.execute("""
            INSERT INTO coin_memory (symbol, first_seen_ts, last_seen_ts, scan_count, consecutive,
                                     avg_score, last_score, last_signal, last_price, bullish_count, bearish_count)
            VALUES (?, ?, ?, 1, 1, ?, ?, ?, ?, ?, ?)
        """, (symbol, now, now, score, score, signal, price, is_bullish, is_bearish))
    else:
        scan_count, avg_score, last_ts, b_count, be_count, consec = row
        new_count  = scan_count + 1
        new_avg    = (avg_score * scan_count + score) / new_count
        # Reset consecutive if not seen in last 2 hours
        new_consec = (consec + 1) if (now - last_ts < 7200) else 1
        cur.execute("""
            UPDATE coin_memory SET
                last_seen_ts   = ?,
                scan_count     = ?,
                consecutive    = ?,
                avg_score      = ?,
                last_score     = ?,
                last_signal    = ?,
                last_price     = ?,
                bullish_count  = ?,
                bearish_count  = ?
            WHERE symbol = ?
        """, (now, new_count, new_consec, new_avg, score, signal, price,
              b_count + is_bullish, be_count + is_bearish, symbol))

    con.commit()
    con.close()


def get_coin_memory(symbol: str) -> dict:
    symbol = symbol.upper().strip()
    con = _conn()
    cur = con.cursor()
    cur.execute("SELECT * FROM coin_memory WHERE symbol=?", (symbol,))
    row = cur.fetchone()
    con.close()

    if not row:
        return _empty(symbol)

    cols = ["symbol", "first_seen_ts", "last_seen_ts", "scan_count", "consecutive",
            "avg_score", "last_score", "last_signal", "last_price", "bullish_count", "bearish_count"]
    data = dict(zip(cols, row))
    data["is_consistent"] = data["consecutive"] >= CONSISTENCY_THRESHOLD
    data["bullish_rate"]  = (data["bullish_count"] / data["scan_count"]) if data["scan_count"] > 0 else 0
    return data


def get_trending_coins(min_scans: int = 3, limit: int = 10) -> list:
    """Return coins seen most consistently with best average scores."""
    con = _conn()
    cur = con.cursor()
    cur.execute("""
        SELECT symbol, scan_count, consecutive, avg_score, last_score, last_signal, bullish_count
        FROM coin_memory
        WHERE scan_count >= ?
        ORDER BY (consecutive * 2 + avg_score * 10) DESC
        LIMIT ?
    """, (min_scans, limit))
    rows = cur.fetchall()
    con.close()

    result = []
    for r in rows:
        result.append({
            "symbol":       r[0],
            "scan_count":   r[1],
            "consecutive":  r[2],
            "avg_score":    round(r[3], 4),
            "last_score":   round(r[4], 4),
            "last_signal":  r[5],
            "bullish_count": r[6],
        })
    return result


def get_memory_score(symbol: str) -> dict:
    """Return a normalized memory score (0-100) and reason for a symbol."""
    data = get_coin_memory(symbol)
    if data["scan_count"] == 0:
        return {"memory_score": 50, "memory_reason": [], "is_consistent": False}

    score  = 50
    reason = []

    # Consistency bonus
    if data["consecutive"] >= 5:
        score += 20
        reason.append(f"Seen consistently {data['consecutive']} times in a row")
    elif data["consecutive"] >= 3:
        score += 10
        reason.append(f"Consistent signal for {data['consecutive']} scans")

    # Avg score bonus
    if data["avg_score"] >= 0.70:
        score += 15
        reason.append(f"High avg scan score ({data['avg_score']:.2f})")
    elif data["avg_score"] >= 0.55:
        score += 7

    # Bullish rate bonus
    brate = data["bullish_rate"]
    if brate >= 0.7:
        score += 10
        reason.append(f"Bullish in {brate:.0%} of scans")
    elif brate <= 0.2 and data["scan_count"] >= 3:
        score -= 10
        reason.append(f"Rarely bullish ({brate:.0%} of scans)")

    # Total appearances
    if data["scan_count"] >= 20:
        score += 5
        reason.append(f"Tracked for {data['scan_count']} scan cycles")

    score = max(0, min(100, score))
    return {
        "memory_score":  score,
        "memory_reason": reason,
        "is_consistent": data["is_consistent"],
        "scan_count":    data["scan_count"],
        "consecutive":   data["consecutive"],
    }


def _empty(symbol: str) -> dict:
    return {
        "symbol":        symbol,
        "first_seen_ts": 0,
        "last_seen_ts":  0,
        "scan_count":    0,
        "consecutive":   0,
        "avg_score":     0.0,
        "last_score":    0.0,
        "last_signal":   "",
        "last_price":    0.0,
        "bullish_count": 0,
        "bearish_count": 0,
        "is_consistent": False,
        "bullish_rate":  0.0,
    }
