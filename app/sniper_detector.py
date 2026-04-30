"""
Sniper-bot detection for pump.fun tokens.

Two complementary signals:

  1. Top-holder concentration ("supply has been hoovered up")
     If top 5 wallets already own >N% of supply, it's been sniped.
     Catches a single bot or small group accumulating most of the supply.

  2. Block-cluster detection ("cabal coordinated buys")
     If N >= CLUSTER_MIN_BUYERS distinct wallets execute buys in the same
     block during the first ~90 seconds, it's a coordinated cabal.
     Catches groups that distribute their buys across many wallets to
     defeat a top-holder check.

Both signals raise the `sniped` flag and feed into the same SNIPED label
so callers don't need to know which one tripped.

We only run this on HOT or post-$5K tokens to keep Helius credit usage low.
The cluster check is the more expensive one (~100 credits) so it is
gated identically to the holder check and cached per mint forever.

Output is cached per mint for 30 minutes — supply distribution rarely
changes that much on a new bonding-curve token, and cluster status of
the first 90 seconds is permanent once the window has passed.
"""

import sqlite3
import time
from collections import defaultdict
from pathlib import Path

from app.helius_enricher import (
    _get_top_holders,
    _get_token_supply,
    get_token_transactions,
)

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"

# Holder-concentration thresholds — conservative so we don't falsely flag
# normal launches.
SNIPED_TOP5_PCT  = 40.0   # top 5 wallets own >= this % → sniped
HEAVY_TOP5_PCT   = 25.0   # warning zone
SNIPED_TOP1_PCT  = 15.0   # single wallet owns >= this % → sniped

# Block-cluster thresholds.
CLUSTER_MIN_BUYERS     = 3     # N distinct wallets in the same slot → cluster
CLUSTER_WINDOW_SECONDS = 90    # only look at first 90s after detection
CLUSTER_SAMPLE_LIMIT   = 100   # max txs to fetch from Helius per check

CACHE_TTL_SECONDS = 1800  # 30 minutes — concentration is slow-moving
_cache: dict = {}


def init_sniper_table() -> None:
    """Persist sniper checks so stats and ML features can query historically."""
    con = sqlite3.connect(DB_PATH, timeout=5)
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
    # Cluster columns added later; safe ALTER for existing DBs.
    for col, typedef in [
        ("cluster_detected",   "INTEGER DEFAULT 0"),
        # Largest count of distinct buyer wallets observed in a single
        # block within the cluster window. >= CLUSTER_MIN_BUYERS = cabal.
        ("cluster_max_buyers", "INTEGER DEFAULT 0"),
        # Number of slots that hit the cluster threshold (informational).
        ("cluster_slot_count", "INTEGER DEFAULT 0"),
        # Set once the cluster check has run, so we never re-query Helius
        # for the same mint regardless of how _cache evicts.
        ("cluster_checked",    "INTEGER DEFAULT 0"),
    ]:
        try:
            con.execute(f"ALTER TABLE sniper_checks ADD COLUMN {col} {typedef}")
            con.commit()
        except Exception:
            pass
    con.close()


def _persist(mint: str, result: dict) -> None:
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.execute("""
        INSERT INTO sniper_checks
        (mint, checked_ts, top1_pct, top5_pct, top10_pct,
         sniped, label, total_supply,
         cluster_detected, cluster_max_buyers, cluster_slot_count, cluster_checked)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mint) DO UPDATE SET
            checked_ts          = excluded.checked_ts,
            top1_pct            = excluded.top1_pct,
            top5_pct            = excluded.top5_pct,
            top10_pct           = excluded.top10_pct,
            sniped              = excluded.sniped,
            label               = excluded.label,
            total_supply        = excluded.total_supply,
            cluster_detected    = excluded.cluster_detected,
            cluster_max_buyers  = excluded.cluster_max_buyers,
            cluster_slot_count  = excluded.cluster_slot_count,
            cluster_checked     = excluded.cluster_checked
    """, (
        mint, result["checked_ts"],
        result["top1_pct"], result["top5_pct"], result["top10_pct"],
        1 if result["sniped"] else 0,
        result["label"], result["total_supply"],
        1 if result.get("cluster_detected") else 0,
        int(result.get("cluster_max_buyers", 0) or 0),
        int(result.get("cluster_slot_count", 0) or 0),
        1 if result.get("cluster_checked") else 0,
    ))
    con.commit()
    con.close()


def _extract_buyer(tx: dict, mint: str) -> str:
    """
    Return the buyer wallet for a parsed Helius pump.fun transaction
    that's a buy of `mint`, or "" if this tx isn't a buy.

    Heuristic: any tokenTransfer where the target mint flows to a regular
    user wallet AND the source is different from the destination. On
    pump.fun this corresponds to "tokens out of bonding curve PDA → buyer
    wallet". Sells flow the other way.
    """
    transfers = tx.get("tokenTransfers") or []
    for t in transfers:
        if t.get("mint") != mint:
            continue
        to_user   = t.get("toUserAccount") or ""
        from_user = t.get("fromUserAccount") or ""
        # Pump.fun's bonding curve PDA shows up as a non-empty fromUserAccount
        # but it's the same address for every buy of a given mint, so we
        # don't need to identify it explicitly — distinct buyers cluster on
        # the to_user field. We just need to_user ≠ from_user.
        if to_user and to_user != from_user:
            return to_user
    return ""


def check_block_clusters(mint: str, detected_ts: int = 0) -> dict:
    """
    Detect coordinated multi-wallet buys in the same block — "cabals".

    Pulls up to CLUSTER_SAMPLE_LIMIT recent parsed transactions from Helius,
    filters to those within CLUSTER_WINDOW_SECONDS of the earliest observed
    blockTime (or `detected_ts` if provided), groups buyers by `slot`, and
    flags a cluster when any single slot contains >= CLUSTER_MIN_BUYERS
    distinct wallets.

    Args:
        mint:        token mint address
        detected_ts: unix ts of first detection; used as the window anchor.
                     If 0, falls back to the earliest blockTime returned by
                     Helius (less precise — pump.fun could have many trades
                     between mint and our detection).

    Returns dict with:
        clustered           bool   — True if a cluster was found
        max_buyers_in_slot  int    — largest distinct-buyer count in any slot
        cluster_slot_count  int    — number of slots that crossed threshold
        sample_size         int    — transactions actually inspected
        checked             bool   — True if Helius returned data we could parse
                                     (False = inconclusive, treat as not-clustered)
    """
    empty = {
        "clustered":          False,
        "max_buyers_in_slot": 0,
        "cluster_slot_count": 0,
        "sample_size":        0,
        "checked":            False,
    }

    txs = get_token_transactions(mint, limit=CLUSTER_SAMPLE_LIMIT)
    if not txs:
        return empty

    # Normalize timestamps. Helius gives `timestamp` as unix seconds.
    timed = [(int(tx.get("timestamp") or 0), int(tx.get("slot") or 0), tx) for tx in txs]
    timed = [t for t in timed if t[0] > 0]
    if not timed:
        return empty

    anchor = detected_ts if detected_ts > 0 else min(t[0] for t in timed)
    window_end = anchor + CLUSTER_WINDOW_SECONDS

    # Some grace either side: if detected_ts is post-mint, allow 30s before
    # the anchor to still count (we may have missed the earliest block).
    in_window = [t for t in timed if (anchor - 30) <= t[0] <= window_end]
    if not in_window:
        return {**empty, "checked": True, "sample_size": 0}

    by_slot: dict = defaultdict(set)
    for ts_secs, slot, tx in in_window:
        buyer = _extract_buyer(tx, mint)
        if buyer and slot > 0:
            by_slot[slot].add(buyer)

    if not by_slot:
        return {**empty, "checked": True, "sample_size": len(in_window)}

    distinct_counts   = [len(buyers) for buyers in by_slot.values()]
    max_in_slot       = max(distinct_counts)
    cluster_slot_cnt  = sum(1 for c in distinct_counts if c >= CLUSTER_MIN_BUYERS)

    return {
        "clustered":          max_in_slot >= CLUSTER_MIN_BUYERS,
        "max_buyers_in_slot": max_in_slot,
        "cluster_slot_count": cluster_slot_cnt,
        "sample_size":        len(in_window),
        "checked":            True,
    }


def _load_cluster_cache(mint: str) -> dict:
    """Read persisted cluster columns so we never re-query Helius."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute(
        "SELECT cluster_detected, cluster_max_buyers, cluster_slot_count, cluster_checked "
        "FROM sniper_checks WHERE mint = ?",
        (mint,),
    )
    row = cur.fetchone()
    con.close()
    if not row or not row[3]:   # cluster_checked == 0 means never run
        return {}
    return {
        "clustered":          bool(row[0]),
        "max_buyers_in_slot": int(row[1] or 0),
        "cluster_slot_count": int(row[2] or 0),
        "sample_size":        0,        # not persisted; informational only
        "checked":            True,
    }


def check_sniper_concentration(mint: str, detected_ts: int = 0) -> dict:
    """Fetch top holders + run cluster check via Helius and classify the token.

    Args:
        mint:        token mint address
        detected_ts: unix ts of first detection. Used by check_block_clusters
                     to anchor the 90-second window. 0 = let cluster check
                     fall back to the earliest blockTime it sees.

    Returns a dict with keys:
      top1_pct, top5_pct, top10_pct  — percentage of supply
      sniped    — bool flag (set by EITHER concentration OR cluster signal)
      label     — 'CLEAN' / 'HEAVY' / 'SNIPED' / 'CLUSTER' / 'UNKNOWN'
      cluster_detected, max_buyers_in_slot, cluster_slot_count, cluster_checked
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
        "cluster_detected":   False,
        "max_buyers_in_slot": 0,
        "cluster_slot_count": 0,
        "cluster_checked":    False,
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

        # Concentration label (provisional — cluster check below may upgrade)
        if (result["top5_pct"] >= SNIPED_TOP5_PCT
                or result["top1_pct"] >= SNIPED_TOP1_PCT):
            result["sniped"] = True
            result["label"]  = "SNIPED"
        elif result["top5_pct"] >= HEAVY_TOP5_PCT:
            result["label"] = "HEAVY"
        else:
            result["label"] = "CLEAN"

        # Cluster check — gated by DB cache so we run it at most once
        # per mint regardless of how many times we're asked.
        cluster_cache = _load_cluster_cache(mint)
        if cluster_cache:
            cluster = cluster_cache
        else:
            try:
                cluster = check_block_clusters(mint, detected_ts=detected_ts)
            except Exception as e:
                print(f"[SNIPER] Cluster check failed for {mint[:8]}: {e}")
                cluster = {"clustered": False, "max_buyers_in_slot": 0,
                           "cluster_slot_count": 0, "checked": False}

        result["cluster_detected"]   = bool(cluster.get("clustered"))
        result["max_buyers_in_slot"] = int(cluster.get("max_buyers_in_slot", 0))
        result["cluster_slot_count"] = int(cluster.get("cluster_slot_count", 0))
        result["cluster_checked"]    = bool(cluster.get("checked"))

        # Cluster-detected forces SNIPED. The label changes to "CLUSTER"
        # so the alert formatter can give a specific reason. Anything that
        # was already SNIPED stays SNIPED with cluster_detected as a side
        # flag — caller can read both fields.
        if result["cluster_detected"]:
            result["sniped"] = True
            if result["label"] != "SNIPED":
                result["label"] = "CLUSTER"

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
    con = sqlite3.connect(DB_PATH, timeout=5)
    cur = con.cursor()
    cur.execute("""
        SELECT checked_ts, top1_pct, top5_pct, top10_pct, sniped, label, total_supply,
               cluster_detected, cluster_max_buyers, cluster_slot_count, cluster_checked
        FROM sniper_checks WHERE mint = ?
    """, (mint,))
    row = cur.fetchone()
    con.close()
    if not row:
        return {}
    return {
        "mint":               mint,
        "checked_ts":         row[0],
        "top1_pct":           row[1],
        "top5_pct":           row[2],
        "top10_pct":          row[3],
        "sniped":             bool(row[4]),
        "label":              row[5],
        "total_supply":       row[6],
        "cluster_detected":   bool(row[7]) if row[7] is not None else False,
        "max_buyers_in_slot": int(row[8] or 0),
        "cluster_slot_count": int(row[9] or 0),
        "cluster_checked":    bool(row[10]) if row[10] is not None else False,
    }


def format_sniper_line(result: dict) -> str:
    """One-line human-readable summary for Telegram alerts."""
    if not result or result.get("label") == "UNKNOWN":
        return ""
    label = result["label"]
    t1, t5 = result["top1_pct"], result["top5_pct"]
    n_buyers = int(result.get("max_buyers_in_slot", 0) or 0)
    n_slots  = int(result.get("cluster_slot_count", 0) or 0)

    if label == "CLUSTER":
        return (
            f"Cabal: {n_buyers} wallets bought in same block "
            f"({n_slots} cluster slot{'s' if n_slots != 1 else ''}) — avoid, coordinated"
        )
    if label == "SNIPED":
        # If cluster is also detected, mention both.
        if result.get("cluster_detected"):
            return (
                f"Snipers + cabal: top5={t5:.0f}% | {n_buyers} wallets/block "
                f"— avoid, coordinated rug"
            )
        return f"Snipers: top5={t5:.0f}% top1={t1:.0f}% — avoid, rug likely"
    if label == "HEAVY":
        return f"Holders: top5={t5:.0f}% (heavy — watch closely)"
    return f"Holders: top5={t5:.0f}% (distributed — clean launch)"
