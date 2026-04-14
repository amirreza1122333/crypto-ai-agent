"""
Two-tier gem scanner:

Tier 1 — Pump.fun early warning (< 60 min old)
  Source : frontend-api.pump.fun/coins (newest launches)
  Poll   : every 60s
  Goal   : alert within 5-10 minutes of launch

Tier 2 — DEX Screener confirmed gems (1-6 h old, has DEX liquidity)
  Source : api.dexscreener.com
  Goal   : catch tokens that graduated pump.fun or launched on other DEXes
"""
import time
import requests
import urllib3
from datetime import datetime, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEXSCREENER_BASE  = "https://api.dexscreener.com"
PUMPFUN_URLS      = [
    "https://frontend-api.pump.fun/coins",
    "https://frontend-api-2.pump.fun/coins",
    "https://client-api-2.pump.fun/coins",
]
PUMPFUN_KOTH_URL  = "https://frontend-api.pump.fun/coins/king-of-the-hill"

# Browser-like headers to bypass Cloudflare
BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://pump.fun/",
    "Origin":          "https://pump.fun",
}

TARGET_CHAINS = {"solana", "ethereum", "bsc", "base", "arbitrum"}

# ── Tier 1: pump.fun thresholds ────────────────────────────────────────────
PUMPFUN_MIN_MCAP       = 8_000    # $8K market cap
PUMPFUN_MAX_AGE_HOURS  = 1.0      # only tokens < 1 hour old
PUMPFUN_ALERT_MCAP     = 15_000   # $15K = send alert

# ── Tier 2: DEX Screener thresholds ───────────────────────────────────────
EARLY_MIN_LIQUIDITY    = 5_000    # $5K (was $30K — reduced for early detection)
EARLY_MIN_VOL_5M       = 500      # $500 in last 5 minutes
EARLY_MAX_AGE_HOURS    = 6.0      # only tokens < 6 hours old

# Legacy thresholds (kept for /newgems command)
MIN_LIQUIDITY_USD = 30_000
MIN_VOLUME_24H    = 30_000
MAX_FDV           = 50_000_000
MAX_AGE_HOURS     = 168
MIN_PRICE_CHANGE  = 20.0

GEM_ALERT_SCORE   = 60   # lowered from 65 to catch earlier


def _get(url, params=None, timeout=12):
    try:
        r = requests.get(url, params=params, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[DEX] {url}: {e}")
        return None


def _age_hours(ts_ms) -> float:
    """Convert a millisecond timestamp to age in hours."""
    if not ts_ms:
        return 9999
    try:
        created = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return (datetime.now(tz=timezone.utc) - created).total_seconds() / 3600
    except Exception:
        return 9999


# ──────────────────────────────────────────────────────────────────────────
# TIER 1 — Pump.fun early scanner
# ──────────────────────────────────────────────────────────────────────────

def _get_pumpfun(url, params=None) -> list:
    """GET from pump.fun with browser headers to bypass Cloudflare."""
    for _ in range(2):
        try:
            r = requests.get(
                url, params=params,
                headers=BROWSER_HEADERS,
                timeout=12, verify=False,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        time.sleep(1)
    return []


def _fetch_pumpfun_new(limit: int = 50) -> list:
    params = {"limit": limit, "sort": "created_timestamp", "order": "DESC", "includeNsfw": "false"}
    for url in PUMPFUN_URLS:
        result = _get_pumpfun(url, params=params)
        if result:
            return result
    return []


def _fetch_pumpfun_koth() -> list:
    return _get_pumpfun(PUMPFUN_KOTH_URL, params={"limit": 10})


def score_pumpfun(coin: dict, age_hours: float) -> int:
    score  = 0
    mcap   = float(coin.get("usd_market_cap", 0) or 0)
    rcnt   = int(coin.get("reply_count", 0) or 0)
    koth   = bool(coin.get("king_of_the_hill_timestamp"))

    # Market cap momentum
    if mcap >= 60_000:   score += 35   # near graduation (~$69K)
    elif mcap >= 30_000: score += 25
    elif mcap >= 15_000: score += 15
    elif mcap >= 8_000:  score += 8

    # Social engagement
    if rcnt >= 100:  score += 20
    elif rcnt >= 30: score += 12
    elif rcnt >= 10: score += 6

    # Age bonus — fresher = more upside remaining
    if age_hours <= 0.25:    score += 25   # < 15 min
    elif age_hours <= 0.5:   score += 18   # < 30 min
    elif age_hours <= 1.0:   score += 10   # < 1 hour

    # King of the Hill = closest to $69K graduation
    if koth:
        score += 20

    return min(score, 100)


def scan_pumpfun_launches(max_results: int = 5) -> list:
    coins = _fetch_pumpfun_new(limit=60)
    koth  = {c.get("mint"): True for c in _fetch_pumpfun_koth()}

    results = []
    for coin in coins:
        if coin.get("complete"):          # already graduated — handled by Tier 2
            continue

        mint      = coin.get("mint", "")
        ts        = coin.get("created_timestamp")
        age       = _age_hours(ts)

        if age > PUMPFUN_MAX_AGE_HOURS:
            continue

        mcap = float(coin.get("usd_market_cap", 0) or 0)
        if mcap < PUMPFUN_MIN_MCAP:
            continue

        coin["king_of_the_hill_timestamp"] = koth.get(mint)
        gem_score = score_pumpfun(coin, age)

        results.append({
            "token_address":    mint,
            "chain":            "solana",
            "name":             coin.get("name", "Unknown"),
            "symbol":           coin.get("symbol", "?"),
            "price_usd":        0.0,          # pump.fun price not in this API
            "market_cap_usd":   mcap,
            "price_change_24h": 0.0,
            "price_change_6h":  0.0,
            "price_change_1h":  0.0,
            "volume_24h":       0.0,
            "liquidity_usd":    0.0,
            "fdv":              mcap,
            "age_hours":        round(age, 3),
            "buys_24h":         0,
            "sells_24h":        0,
            "reply_count":      int(coin.get("reply_count", 0) or 0),
            "gem_score":        gem_score,
            "is_koth":          bool(coin.get("king_of_the_hill_timestamp")),
            "tier":             "pumpfun",
            "url":              f"https://pump.fun/{mint}",
        })

    results.sort(key=lambda x: x["gem_score"], reverse=True)
    return results[:max_results]


# ──────────────────────────────────────────────────────────────────────────
# TIER 2 — DEX Screener scanner (graduated tokens + other chains)
# ──────────────────────────────────────────────────────────────────────────

def fetch_latest_profiles() -> list:
    data = _get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def fetch_token_pairs(token_address: str) -> list:
    data = _get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}")
    return data.get("pairs", []) or [] if data else []


def score_gem(pair: dict) -> int:
    score = 0

    liquidity  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    vol_24h    = float(pair.get("volume", {}).get("h24", 0) or 0)
    vol_5m     = float(pair.get("volume", {}).get("m5", 0) or 0)
    ch24       = float(pair.get("priceChange", {}).get("h24", 0) or 0)
    ch1        = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    ch5m       = float(pair.get("priceChange", {}).get("m5", 0) or 0)
    buys_5m    = int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0)
    sells_5m   = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    buys_24h   = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
    sells_24h  = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    fdv        = float(pair.get("fdv", 0) or 0)

    # 5-minute momentum (most important for early detection)
    if ch5m >= 50:    score += 30
    elif ch5m >= 20:  score += 20
    elif ch5m >= 10:  score += 12
    elif ch5m >= 5:   score += 6

    # 1h momentum
    if ch1 >= 50:    score += 20
    elif ch1 >= 20:  score += 14
    elif ch1 >= 10:  score += 8

    # 24h momentum
    if ch24 >= 100:  score += 15
    elif ch24 >= 50: score += 10
    elif ch24 >= 20: score += 5

    # 5-minute buy pressure
    total_5m = buys_5m + sells_5m
    if total_5m > 0:
        buy_ratio_5m = buys_5m / total_5m
        if buy_ratio_5m >= 0.80:   score += 20
        elif buy_ratio_5m >= 0.65: score += 12
        elif buy_ratio_5m >= 0.55: score += 6

    # 5-minute volume activity
    if vol_5m >= 50_000:  score += 15
    elif vol_5m >= 10_000: score += 10
    elif vol_5m >= 1_000:  score += 5

    # Vol/Liq ratio
    if liquidity > 0:
        ratio = vol_24h / liquidity
        score += min(int(ratio * 5), 15)

    # Liquidity safety
    if liquidity >= 100_000:   score += 10
    elif liquidity >= 30_000:  score += 6
    elif liquidity >= 10_000:  score += 3

    # Low FDV = more room
    if 0 < fdv < 500_000:      score += 8
    elif fdv < 2_000_000:      score += 4

    return min(score, 100)


def is_rug_risk(pair: dict) -> bool:
    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    buys  = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
    sells = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)

    if liquidity < EARLY_MIN_LIQUIDITY:
        return True

    total = buys + sells
    if total > 10 and sells > buys * 3.0:   # 75%+ sells = likely dump
        return True

    return False


def scan_dex_new_gems(max_results: int = 8) -> list:
    profiles = fetch_latest_profiles()
    gems     = []
    seen     = set()

    for profile in profiles[:80]:
        chain_id      = profile.get("chainId", "")
        token_address = profile.get("tokenAddress", "")

        if not token_address or token_address in seen:
            continue
        if chain_id not in TARGET_CHAINS:
            continue

        seen.add(token_address)
        pairs = fetch_token_pairs(token_address)
        if not pairs:
            time.sleep(0.15)
            continue

        chain_pairs = [p for p in pairs if p.get("chainId") == chain_id]
        if not chain_pairs:
            time.sleep(0.15)
            continue

        pair = max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        age       = _age_hours(pair.get("pairCreatedAt"))
        liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        vol_5m    = float(pair.get("volume", {}).get("m5", 0) or 0)
        vol_24h   = float(pair.get("volume", {}).get("h24", 0) or 0)
        ch24      = float(pair.get("priceChange", {}).get("h24", 0) or 0)
        ch5m      = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        fdv       = float(pair.get("fdv", 0) or 0)

        # Early detection filters — much looser than original
        if age      > EARLY_MAX_AGE_HOURS:    time.sleep(0.15); continue
        if liquidity < EARLY_MIN_LIQUIDITY:   time.sleep(0.15); continue
        if vol_5m   < EARLY_MIN_VOL_5M and vol_24h < 5_000: time.sleep(0.15); continue
        if fdv      > MAX_FDV:                time.sleep(0.15); continue
        if is_rug_risk(pair):                 time.sleep(0.15); continue

        gem_score = score_gem(pair)
        base  = pair.get("baseToken", {})
        buys  = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
        sells = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)

        gems.append({
            "token_address":    token_address,
            "chain":            chain_id,
            "name":             base.get("name", "Unknown"),
            "symbol":           base.get("symbol", "?"),
            "price_usd":        float(pair.get("priceUsd", 0) or 0),
            "market_cap_usd":   fdv,
            "price_change_24h": ch24,
            "price_change_6h":  float(pair.get("priceChange", {}).get("h6", 0) or 0),
            "price_change_1h":  float(pair.get("priceChange", {}).get("h1", 0) or 0),
            "price_change_5m":  ch5m,
            "volume_24h":       vol_24h,
            "volume_5m":        vol_5m,
            "liquidity_usd":    liquidity,
            "fdv":              fdv,
            "age_hours":        age,
            "buys_24h":         buys,
            "sells_24h":        sells,
            "reply_count":      0,
            "gem_score":        gem_score,
            "is_koth":          False,
            "tier":             "dex",
            "url":              pair.get("url", ""),
        })

        time.sleep(0.2)

    gems.sort(key=lambda x: x["gem_score"], reverse=True)
    return gems[:max_results]


# ──────────────────────────────────────────────────────────────────────────
# Combined scanner
# ──────────────────────────────────────────────────────────────────────────

def scan_new_gems(max_results: int = 10) -> list:
    """Run both tiers and return merged, deduplicated results."""
    results = []
    seen    = set()

    # Tier 1: pump.fun (fastest, catches < 1h launches)
    try:
        pf = scan_pumpfun_launches(max_results=5)
        for g in pf:
            key = g["token_address"]
            if key not in seen:
                seen.add(key)
                results.append(g)
    except Exception as e:
        print(f"[DEX] Tier1 error: {e}")

    # Tier 2: DEX Screener (confirmed DEX liquidity)
    try:
        dex = scan_dex_new_gems(max_results=8)
        for g in dex:
            key = g["token_address"]
            if key not in seen:
                seen.add(key)
                results.append(g)
    except Exception as e:
        print(f"[DEX] Tier2 error: {e}")

    results.sort(key=lambda x: x["gem_score"], reverse=True)
    return results[:max_results]
