"""
Sniper-bot detection for pump.fun tokens.

A "sniper" is a bot that buys a token within milliseconds of mint creation,
often accumulating a huge percentage of supply before any human can react.
Tokens dominated by snipers rarely moon — once the snipers dump, the token
dies.

Signal: check top-holder concentration when a token has been alive for
2-5 minutes. If top 5 wallets already own >N% of supply, it's been sniped.

We only run this on HOT or post-$5K tokens to keep Helius credit usage low
(~3 credits per check, pump.fun spawns 1000+ tokens/hour so checking every
one would be wasteful).

Output is cached per mint for 30 minutes — supply distribution rarely
changes that much on a new bonding-curve token.
"""

import sqlite3
import time
from pathlib import Path

from app.helius_enricher import _get_top_holders, _get_token_supply

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

# Thresholds — conservative so we don't falsely flag normal launches.
SNIPED_TOP5_PCT  = 40.0   # top 5 wallets own >= this % → sniped
HEAVY_TOP5_PCT   = 25.0   # warning zone
SNIPED_TOP1_PCT  = 15.0   # single wallet owns >= this % → sniped

CACHE_TTL_SECONDS = 1800  # 30 minutes — concentration is slow-moving
_cache: dict = {}


def init_sniper_table() -> None:
    """Persist sniper checks so stats and ML features can query historically."""
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS sniper_checks (
            mint           TEXT PRIMARY KEY,
            checked_ts     INTEGER,
            top1_pct       REAL,
            top5_pct       REAL,
            top10_pct      REAL,
            sniped         INTEGER DEFAULT 0,
            label          TEXT,
            total_supply   REAL
        )
    """)
    con.commit()
    con.close()


def _persist(mint: str, result: dict) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO sniper_checks
        (mint, checked_ts, top1_pct, top5_pct, top10_pct,
         sniped, label, total_supply)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(mint) DO UPDATE SET
            checked_ts   = excluded.checked_ts,
            top1_pct     = excluded.top1_pct,
            top5_pct     = excluded.top5_pct,
            top10_pct    = excluded.top10_pct,
            sniped       = excluded.sniped,
            label        = excluded.label,
            total_supply = excluded.total_supply
    """, (
        mint, result["checked_ts"],
        result["top1_pct"], result["top5_pct"], result["top10_pct"],
        1 if result["sniped"] else 0,
        result["label"], result["total_supply"],
    ))
    con.commit()
    con.close()


def check_sniper_concentration(mint: str) -> dict:
    """Fetch top holders via Helius and classify the token's concentration.

    Returns a dict with keys:
      top1_pct, top5_pct, top10_pct  — percentage of supply
      sniped    — bool flag
      label     — 'CLEAN' / 'HEAVY' / 'SNIPED' / 'UNKNOWN'
      checked_ts, total_supply
    """
    now = int(time.time())

    cached = _cache.get(mint)
    if cached and now - cached["checked_ts"] < CACHE_TTL_SECONDS:
        return cached

    result = {
        "mint":          mint,
        "checked_ts":    now,
        "top1_pct":      0.0,
        "top5_pct":      0.0,
        "top10_pct":     0.0,
        "sniped":        False,
        "label":         "UNKNOWN",
        "total_supply":  0.0,
    }

    try:
        holders = _get_top_holders(mint, 10)
        supply  = _get_token_supply(mint)
        if not holders or supply <= 0:
            _cache[mint] = result
            return result

        amounts = [float(h.get("amount", 0) or 0) for h in holders]
        top1    = amounts[0] if amounts else 0
        top5    = sum(amounts[:5])
        top10   = sum(amounts[:10])

        result["total_supply"] = supply
        result["top1_pct"]     = round(top1  / supply * 100, 1)
        result["top5_pct"]     = round(top5  / supply * 100, 1)
        result["top10_pct"]    = round(top10 / supply * 100, 1)

        if (result["top5_pct"] >= SNIPED_TOP5_PCT
                or result["top1_pct"] >= SNIPED_TOP1_PCT):
            result["sniped"] = True
            result["label"]  = "SNIPED"
        elif result["top5_pct"] >= HEAVY_TOP5_PCT:
            result["label"] = "HEAVY"
        else:
            result["label"] = "CLEAN"

    except Exception as e:
        print(f"[SNIPER] Check failed for {mint[:8]}: {e}")
        return result

    _cache[mint] = result
    try:
        _persist(mint, result)
    except Exception as e:
        print(f"[SNIPER] Persist error: {e}")
    return result


def get_last_check(mint: str) -> dict:
    """Retrieve the most recent persisted check, if any."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        SELECT checked_ts, top1_pct, top5_pct, top10_pct, sniped, label, total_supply
        FROM sniper_checks WHERE mint = ?
    """, (mint,))
    row = cur.fetchone()
    con.close()
    if not row:
        return {}
    return {
        "mint":         mint,
        "checked_ts":   row[0],
        "top1_pct":     row[1],
        "top5_pct":     row[2],
        "top10_pct":    row[3],
        "sniped":       bool(row[4]),
        "label":        row[5],
        "total_supply": row[6],
    }


def format_sniper_line(result: dict) -> str:
    """One-line human-readable summary for Telegram alerts."""
    if not result or result.get("label") == "UNKNOWN":
        return ""
    label = result["label"]
    t1, t5 = result["top1_pct"], result["top5_pct"]
    if label == "SNIPED":
        return f"Snipers: top5={t5:.0f}% top1={t1:.0f}% — avoid, rug likely"
    if label == "HEAVY":
        return f"Holders: top5={t5:.0f}% (heavy — watch closely)"
    return f"Holders: top5={t5:.0f}% (distributed — clean launch)"
