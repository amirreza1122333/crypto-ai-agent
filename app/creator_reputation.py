"""
Creator (developer wallet) reputation tracker.

Every pump.fun token has a `creator` field — the wallet that minted it. Most
tokens fail, but a small fraction of wallets repeatedly launch tokens that
graduate and pump. Identifying those wallets in advance is one of the
highest-signal edges in early-stage meme trading.

This module:
  1. Maintains a `creator_reputation` table summarizing every wallet's
     historical launches.
  2. Updates stats incrementally as new outcome snapshots arrive
     (prelaunch_outcomes.return_pct at +1h / +6h / +24h).
  3. Assigns each wallet a tier:
       WINNER   → ≥2 launches with ≥1 graduation and avg peak ≥ $30K
       NEUTRAL  → has launches but neither enough wins nor enough rugs
       RUGGER   → ≥3 launches, 0 graduations, all died below $5K
       UNKNOWN  → brand-new wallet, never seen before

The tier is used as a feature by the ML pipeline AND as a live signal boost
at token detection time (a WINNER wallet's new token jumps straight to HOT).

No external APIs — this reads entirely from the local prelaunch_* tables.
"""

import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

# Thresholds for tier assignment. Hand-tuned initial values — will be
# replaced by data-driven cutoffs once we have enough history.
WINNER_MIN_LAUNCHES   = 2
WINNER_MIN_GRADS      = 1
WINNER_MIN_AVG_PEAK   = 30_000   # USD
RUGGER_MIN_LAUNCHES   = 3
RUGGER_MAX_PEAK_EACH  = 5_000    # every launch died below this
GRADUATION_MCAP_USD   = 65_000   # must match prelaunch_tracker.GRADUATION_MCAP


def init_creator_reputation_table() -> None:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""
        CREATE TABLE IF NOT EXISTS creator_reputation (
            creator            TEXT PRIMARY KEY,
            total_launches     INTEGER DEFAULT 0,
            graduations        INTEGER DEFAULT 0,
            avg_peak_mcap_usd  REAL    DEFAULT 0,
            max_peak_mcap_usd  REAL    DEFAULT 0,
            avg_return_6h_pct  REAL    DEFAULT 0,
            max_return_6h_pct  REAL    DEFAULT 0,
            tier               TEXT    DEFAULT 'UNKNOWN',
            last_updated_ts    INTEGER DEFAULT 0
        )
    """)
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_creator_tier ON creator_reputation(tier)"
    )
    con.commit()
    con.close()


def _compute_tier(total: int, grads: int, avg_peak: float,
                  max_peak: float) -> str:
    """Deterministic tier from aggregate stats."""
    if total == 0:
        return "UNKNOWN"
    if (total >= WINNER_MIN_LAUNCHES
            and grads >= WINNER_MIN_GRADS
            and avg_peak >= WINNER_MIN_AVG_PEAK):
        return "WINNER"
    if (total >= RUGGER_MIN_LAUNCHES
            and grads == 0
            and max_peak < RUGGER_MAX_PEAK_EACH):
        return "RUGGER"
    return "NEUTRAL"


def recompute_creator(creator: str) -> dict:
    """Recompute one creator's stats from scratch, write to table, return it.

    Joins prelaunch_tokens (launches) with prelaunch_outcomes (@6h horizon)
    to get the clearest single-number label per launch.
    """
    if not creator:
        return {}

    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()

    # Pull every launch by this wallet along with its 6h outcome row if any.
    cur.execute("""
        SELECT t.mint,
               COALESCE(t.peak_mcap_usd, 0),
               COALESCE(t.graduated, 0),
               o.return_pct,
               o.peak_mcap_so_far,
               o.graduated_by_then
        FROM prelaunch_tokens t
        LEFT JOIN prelaunch_outcomes o
               ON o.mint = t.mint AND o.horizon_min = 360
        WHERE t.creator = ?
    """, (creator,))
    rows = cur.fetchall()

    total = len(rows)
    grads = 0
    peaks = []
    returns_6h = []

    for _mint, live_peak, live_grad, ret_pct, out_peak, out_grad in rows:
        # Prefer outcome-row values (stable, finalized at +6h); fall back to
        # live values for launches that haven't reached a horizon yet.
        peak = out_peak if out_peak is not None else live_peak
        grad = out_grad if out_grad is not None else live_grad
        peaks.append(float(peak or 0))
        if grad:
            grads += 1
        if ret_pct is not None:
            returns_6h.append(float(ret_pct))

    avg_peak = sum(peaks) / len(peaks) if peaks else 0.0
    max_peak = max(peaks) if peaks else 0.0
    avg_ret  = sum(returns_6h) / len(returns_6h) if returns_6h else 0.0
    max_ret  = max(returns_6h) if returns_6h else 0.0
    tier = _compute_tier(total, grads, avg_peak, max_peak)

    now = int(time.time())
    con.execute("""
        INSERT INTO creator_reputation
        (creator, total_launches, graduations,
         avg_peak_mcap_usd, max_peak_mcap_usd,
         avg_return_6h_pct, max_return_6h_pct,
         tier, last_updated_ts)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(creator) DO UPDATE SET
            total_launches    = excluded.total_launches,
            graduations       = excluded.graduations,
            avg_peak_mcap_usd = excluded.avg_peak_mcap_usd,
            max_peak_mcap_usd = excluded.max_peak_mcap_usd,
            avg_return_6h_pct = excluded.avg_return_6h_pct,
            max_return_6h_pct = excluded.max_return_6h_pct,
            tier              = excluded.tier,
            last_updated_ts   = excluded.last_updated_ts
    """, (creator, total, grads, avg_peak, max_peak,
          avg_ret, max_ret, tier, now))
    con.commit()
    con.close()

    return {
        "creator": creator,
        "total_launches": total,
        "graduations": grads,
        "avg_peak_mcap_usd": avg_peak,
        "max_peak_mcap_usd": max_peak,
        "avg_return_6h_pct": avg_ret,
        "max_return_6h_pct": max_ret,
        "tier": tier,
    }


def get_creator_stats(creator: str) -> dict:
    """Fast lookup for live scoring. Returns empty dict for unknown wallets."""
    if not creator:
        return {}
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("""
        SELECT total_launches, graduations,
               avg_peak_mcap_usd, max_peak_mcap_usd,
               avg_return_6h_pct, max_return_6h_pct, tier
        FROM creator_reputation WHERE creator = ?
    """, (creator,))
    row = cur.fetchone()
    con.close()
    if not row:
        return {"creator": creator, "tier": "UNKNOWN", "total_launches": 0}
    return {
        "creator": creator,
        "total_launches":    row[0],
        "graduations":       row[1],
        "avg_peak_mcap_usd": row[2],
        "max_peak_mcap_usd": row[3],
        "avg_return_6h_pct": row[4],
        "max_return_6h_pct": row[5],
        "tier":              row[6],
    }


def get_creator_tier(creator: str) -> str:
    """Single-value accessor for the hot path at token detection."""
    stats = get_creator_stats(creator)
    return stats.get("tier", "UNKNOWN")


def recompute_all_creators() -> int:
    """Full rebuild — call periodically (hourly) from a background loop.

    Cheap: typically <10K distinct creators even after months of scanning.
    """
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute(
        "SELECT DISTINCT creator FROM prelaunch_tokens WHERE creator IS NOT NULL AND creator != ''"
    )
    creators = [r[0] for r in cur.fetchall()]
    con.close()

    count = 0
    for c in creators:
        try:
            recompute_creator(c)
            count += 1
        except Exception as e:
            print(f"[CREATOR_REP] Recompute error for {c[:8]}: {e}")
    return count


def top_creators(limit: int = 10, min_launches: int = 2) -> list:
    """Leaderboard for /stats command."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("""
        SELECT creator, total_launches, graduations,
               avg_peak_mcap_usd, max_peak_mcap_usd,
               avg_return_6h_pct, tier
        FROM creator_reputation
        WHERE total_launches >= ?
        ORDER BY graduations DESC, avg_peak_mcap_usd DESC
        LIMIT ?
    """, (min_launches, limit))
    rows = cur.fetchall()
    con.close()
    return [
        {
            "creator":           r[0],
            "total_launches":    r[1],
            "graduations":       r[2],
            "avg_peak_mcap_usd": r[3],
            "max_peak_mcap_usd": r[4],
            "avg_return_6h_pct": r[5],
            "tier":              r[6],
        }
        for r in rows
    ]
