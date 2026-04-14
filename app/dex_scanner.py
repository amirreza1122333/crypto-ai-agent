"""
Three-tier gem scanner (fastest to slowest):

Tier 0 — GeckoTerminal new pools  (real-time, single API call, < 5 min detection)
  Source : api.geckoterminal.com/api/v2/networks/{chain}/new_pools
  Goal   : catch any new pool within minutes of creation

Tier 1 — Pump.fun early warning   (< 60 min, browser headers to bypass CF)
  Source : frontend-api.pump.fun/coins
  Goal   : detect pump.fun launches before DEX graduation

Tier 2 — DEX Screener profiles    (fallback, profiled/boosted tokens)
  Source : api.dexscreener.com/token-profiles/latest/v1
  Goal   : catch anything missed by Tier 0/1
"""
import time
import requests
import urllib3
from datetime import datetime, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
DEXSCREENER_BASE   = "https://api.dexscreener.com"
PUMPFUN_URLS       = [
    "https://frontend-api.pump.fun/coins",
    "https://frontend-api-2.pump.fun/coins",
    "https://client-api-2.pump.fun/coins",
]
PUMPFUN_KOTH_URL   = "https://frontend-api.pump.fun/coins/king-of-the-hill"

BROWSER_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://pump.fun/",
    "Origin":          "https://pump.fun",
}

GKO_HEADERS = {
    "Accept": "application/json;version=20230302",
}

# Chains to scan on GeckoTerminal
GKO_CHAINS = ["solana", "ethereum", "base", "bsc"]

TARGET_CHAINS = {"solana", "ethereum", "bsc", "base", "arbitrum"}

# ── Thresholds ─────────────────────────────────────────────────────────────
GKO_MAX_AGE_HOURS   = 2.0     # GeckoTerminal: alert on pools < 2 hours old
GKO_MIN_LIQUIDITY   = 5_000   # $5K minimum liquidity
GKO_MIN_VOL_5M      = 200     # $200 in last 5 minutes (very low = catches early)
PUMPFUN_MIN_MCAP    = 8_000
PUMPFUN_MAX_AGE_H   = 1.0
EARLY_MIN_LIQUIDITY = 5_000
EARLY_MAX_AGE_HOURS = 6.0
MAX_FDV             = 50_000_000
GEM_ALERT_SCORE     = 55      # lowered for faster alerts


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _get(url, params=None, headers=None, timeout=12):
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[DEX] {url}: {e}")
        return None


def _get_silent(url, params=None, headers=None, timeout=12):
    """Like _get but no error print."""
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout, verify=False)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def _age_hours(ts_ms) -> float:
    if not ts_ms:
        return 9999
    try:
        created = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        return (datetime.now(tz=timezone.utc) - created).total_seconds() / 3600
    except Exception:
        return 9999


def _age_hours_iso(ts_str) -> float:
    if not ts_str:
        return 9999
    try:
        created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except Exception:
        return 9999


def _fmt_pool_url(chain: str, pool_address: str) -> str:
    return f"https://dexscreener.com/{chain}/{pool_address}"


# ──────────────────────────────────────────────────────────────────────────
# TIER 0 — GeckoTerminal new pools  (FASTEST)
# ──────────────────────────────────────────────────────────────────────────

def _score_gko(attrs: dict, age_hours: float) -> int:
    score  = 0
    liq    = float(attrs.get("reserve_in_usd", 0) or 0)
    vol5m  = float((attrs.get("volume_usd") or {}).get("m5", 0) or 0)
    vol1h  = float((attrs.get("volume_usd") or {}).get("h1", 0) or 0)
    ch5m   = float((attrs.get("price_change_percentage") or {}).get("m5", 0) or 0)
    ch1h   = float((attrs.get("price_change_percentage") or {}).get("h1", 0) or 0)
    ch24h  = float((attrs.get("price_change_percentage") or {}).get("h24", 0) or 0)
    txns5m = int((attrs.get("transactions") or {}).get("m5", {}).get("buys", 0) or 0)

    # 5-minute momentum — most critical for early detection
    if ch5m >= 100:   score += 40
    elif ch5m >= 50:  score += 30
    elif ch5m >= 20:  score += 20
    elif ch5m >= 10:  score += 12
    elif ch5m >= 5:   score += 6

    # 1h momentum
    if ch1h >= 200:   score += 25
    elif ch1h >= 100: score += 18
    elif ch1h >= 50:  score += 12
    elif ch1h >= 20:  score += 6

    # 5-minute volume
    if vol5m >= 100_000: score += 20
    elif vol5m >= 20_000: score += 15
    elif vol5m >= 5_000:  score += 10
    elif vol5m >= 500:    score += 5

    # Recent buy transactions
    if txns5m >= 50:  score += 15
    elif txns5m >= 20: score += 10
    elif txns5m >= 5:  score += 5

    # Age bonus — fresher = more opportunity
    if age_hours <= 0.083:   score += 30   # < 5 min — just launched
    elif age_hours <= 0.25:  score += 22   # < 15 min
    elif age_hours <= 0.5:   score += 15   # < 30 min
    elif age_hours <= 1.0:   score += 8    # < 1 hour
    elif age_hours <= 2.0:   score += 4

    # Liquidity safety
    if liq >= 100_000:  score += 10
    elif liq >= 30_000: score += 6
    elif liq >= 10_000: score += 3

    return min(score, 100)


def scan_geckoterminal_new(max_results: int = 8) -> list:
    results = []
    seen    = set()

    for chain in GKO_CHAINS:
        try:
            url  = f"{GECKOTERMINAL_BASE}/networks/{chain}/new_pools"
            data = _get_silent(url, params={"include": "base_token,dex"}, headers=GKO_HEADERS)
            if not data:
                continue

            pools    = data.get("data", [])
            included = data.get("included", [])
            tokens   = {i["id"]: i for i in included if i.get("type") == "token"}
            dexes    = {i["id"]: i for i in included if i.get("type") == "dex"}

            for pool in pools:
                attrs     = pool.get("attributes", {})
                rels      = pool.get("relationships", {})
                pool_addr = attrs.get("address", "")

                if pool_addr in seen:
                    continue

                age = _age_hours_iso(attrs.get("pool_created_at"))
                if age > GKO_MAX_AGE_HOURS:
                    continue

                liq   = float(attrs.get("reserve_in_usd", 0) or 0)
                vol5m = float((attrs.get("volume_usd") or {}).get("m5", 0) or 0)
                if liq < GKO_MIN_LIQUIDITY:
                    continue
                if vol5m < GKO_MIN_VOL_5M:
                    continue

                # Get base token details
                bt_id  = (rels.get("base_token") or {}).get("data", {}).get("id", "")
                token  = tokens.get(bt_id, {})
                t_attr = token.get("attributes", {})

                # Get DEX name
                dex_id   = (rels.get("dex") or {}).get("data", {}).get("id", "")
                dex_info = dexes.get(dex_id, {})
                dex_name = dex_info.get("attributes", {}).get("name", dex_id)

                fdv   = float(attrs.get("fdv_usd", 0) or 0)
                if fdv > MAX_FDV:
                    continue

                score = _score_gko(attrs, age)
                seen.add(pool_addr)

                ch5m  = float((attrs.get("price_change_percentage") or {}).get("m5", 0) or 0)
                ch1h  = float((attrs.get("price_change_percentage") or {}).get("h1", 0) or 0)
                ch24h = float((attrs.get("price_change_percentage") or {}).get("h24", 0) or 0)

                results.append({
                    "token_address":    t_attr.get("address", pool_addr),
                    "chain":            chain,
                    "name":             t_attr.get("name", attrs.get("name", "Unknown")),
                    "symbol":           t_attr.get("symbol", "?"),
                    "price_usd":        float(attrs.get("base_token_price_usd", 0) or 0),
                    "market_cap_usd":   fdv,
                    "price_change_5m":  ch5m,
                    "price_change_1h":  ch1h,
                    "price_change_24h": ch24h,
                    "volume_5m":        vol5m,
                    "volume_24h":       float((attrs.get("volume_usd") or {}).get("h24", 0) or 0),
                    "liquidity_usd":    liq,
                    "fdv":              fdv,
                    "age_hours":        age,
                    "buys_24h":         0,
                    "sells_24h":        0,
                    "reply_count":      0,
                    "gem_score":        score,
                    "is_koth":          False,
                    "tier":             "geckoterminal",
                    "dex":              dex_name,
                    "url":              _fmt_pool_url(chain, pool_addr),
                })

        except Exception as e:
            print(f"[GKO] {chain}: {e}")

    results.sort(key=lambda x: x["gem_score"], reverse=True)
    return results[:max_results]


# ──────────────────────────────────────────────────────────────────────────
# TIER 1 — Pump.fun early scanner
# ──────────────────────────────────────────────────────────────────────────

def _get_pumpfun(url, params=None) -> list:
    for _ in range(2):
        try:
            r = requests.get(url, params=params, headers=BROWSER_HEADERS,
                             timeout=12, verify=False)
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
    score = 0
    mcap  = float(coin.get("usd_market_cap", 0) or 0)
    rcnt  = int(coin.get("reply_count", 0) or 0)
    koth  = bool(coin.get("king_of_the_hill_timestamp"))

    if mcap >= 60_000:   score += 35
    elif mcap >= 30_000: score += 25
    elif mcap >= 15_000: score += 15
    elif mcap >= 8_000:  score += 8

    if rcnt >= 100:  score += 20
    elif rcnt >= 30: score += 12
    elif rcnt >= 10: score += 6

    if age_hours <= 0.083:  score += 30
    elif age_hours <= 0.25: score += 22
    elif age_hours <= 0.5:  score += 15
    elif age_hours <= 1.0:  score += 8

    if koth:
        score += 20

    return min(score, 100)


def scan_pumpfun_launches(max_results: int = 5) -> list:
    coins = _fetch_pumpfun_new(limit=60)
    koth  = {c.get("mint"): True for c in _fetch_pumpfun_koth()}
    results = []

    for coin in coins:
        if coin.get("complete"):
            continue

        mint = coin.get("mint", "")
        age  = _age_hours(coin.get("created_timestamp"))
        if age > PUMPFUN_MAX_AGE_H:
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
            "price_usd":        0.0,
            "market_cap_usd":   mcap,
            "price_change_5m":  0.0,
            "price_change_1h":  0.0,
            "price_change_24h": 0.0,
            "volume_5m":        0.0,
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
            "dex":              "Pump.fun",
            "url":              f"https://pump.fun/{mint}",
        })

    results.sort(key=lambda x: x["gem_score"], reverse=True)
    return results[:max_results]


# ──────────────────────────────────────────────────────────────────────────
# TIER 2 — DEX Screener profiles (slowest, fallback)
# ──────────────────────────────────────────────────────────────────────────

def fetch_latest_profiles() -> list:
    data = _get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def fetch_token_pairs(token_address: str) -> list:
    data = _get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}")
    return data.get("pairs", []) or [] if data else []


def score_gem(pair: dict) -> int:
    score  = 0
    liq    = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    vol_24 = float(pair.get("volume", {}).get("h24", 0) or 0)
    vol_5m = float(pair.get("volume", {}).get("m5", 0) or 0)
    ch5m   = float(pair.get("priceChange", {}).get("m5", 0) or 0)
    ch1h   = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    ch24h  = float(pair.get("priceChange", {}).get("h24", 0) or 0)
    buys5m = int(pair.get("txns", {}).get("m5", {}).get("buys", 0) or 0)
    sells5 = int(pair.get("txns", {}).get("m5", {}).get("sells", 0) or 0)
    fdv    = float(pair.get("fdv", 0) or 0)

    if ch5m >= 50:    score += 30
    elif ch5m >= 20:  score += 20
    elif ch5m >= 10:  score += 12
    elif ch5m >= 5:   score += 6

    if ch1h >= 100:  score += 20
    elif ch1h >= 50: score += 14
    elif ch1h >= 20: score += 8

    if ch24h >= 100: score += 10
    elif ch24h >= 50: score += 6

    t5 = buys5m + sells5
    if t5 > 0:
        br = buys5m / t5
        if br >= 0.80:   score += 20
        elif br >= 0.65: score += 12
        elif br >= 0.55: score += 6

    if vol_5m >= 50_000:  score += 15
    elif vol_5m >= 10_000: score += 10
    elif vol_5m >= 1_000:  score += 5

    if liq > 0:
        score += min(int((vol_24 / liq) * 5), 12)

    if liq >= 100_000:   score += 8
    elif liq >= 30_000:  score += 5
    elif liq >= 10_000:  score += 2

    if 0 < fdv < 500_000:   score += 8
    elif fdv < 2_000_000:   score += 4

    return min(score, 100)


def is_rug_risk(pair: dict) -> bool:
    liq   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    buys  = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
    sells = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    if liq < EARLY_MIN_LIQUIDITY:
        return True
    total = buys + sells
    if total > 10 and sells > buys * 3.0:
        return True
    return False


def scan_dex_new_gems(max_results: int = 5) -> list:
    profiles = fetch_latest_profiles()
    gems     = []
    seen     = set()

    for profile in profiles[:60]:
        chain_id      = profile.get("chainId", "")
        token_address = profile.get("tokenAddress", "")
        if not token_address or token_address in seen:
            continue
        if chain_id not in TARGET_CHAINS:
            continue

        seen.add(token_address)
        pairs = fetch_token_pairs(token_address)
        if not pairs:
            time.sleep(0.1)
            continue

        chain_pairs = [p for p in pairs if p.get("chainId") == chain_id]
        if not chain_pairs:
            time.sleep(0.1)
            continue

        pair = max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        age  = _age_hours(pair.get("pairCreatedAt"))
        liq  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        fdv  = float(pair.get("fdv", 0) or 0)
        ch5m = float(pair.get("priceChange", {}).get("m5", 0) or 0)
        v5m  = float(pair.get("volume", {}).get("m5", 0) or 0)

        if age  > EARLY_MAX_AGE_HOURS:   time.sleep(0.1); continue
        if liq  < EARLY_MIN_LIQUIDITY:   time.sleep(0.1); continue
        if fdv  > MAX_FDV:               time.sleep(0.1); continue
        if is_rug_risk(pair):            time.sleep(0.1); continue

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
            "price_change_5m":  ch5m,
            "price_change_1h":  float(pair.get("priceChange", {}).get("h1", 0) or 0),
            "price_change_24h": float(pair.get("priceChange", {}).get("h24", 0) or 0),
            "volume_5m":        v5m,
            "volume_24h":       float(pair.get("volume", {}).get("h24", 0) or 0),
            "liquidity_usd":    liq,
            "fdv":              fdv,
            "age_hours":        age,
            "buys_24h":         buys,
            "sells_24h":        sells,
            "reply_count":      0,
            "gem_score":        score_gem(pair),
            "is_koth":          False,
            "tier":             "dex",
            "dex":              "DEX Screener",
            "url":              pair.get("url", ""),
        })
        time.sleep(0.15)

    gems.sort(key=lambda x: x["gem_score"], reverse=True)
    return gems[:max_results]


# ──────────────────────────────────────────────────────────────────────────
# Combined scanner — all tiers merged
# ──────────────────────────────────────────────────────────────────────────

def scan_new_gems(max_results: int = 10) -> list:
    results = []
    seen    = set()

    # Tier 0: GeckoTerminal (fastest — real-time new pools)
    try:
        for g in scan_geckoterminal_new(max_results=8):
            key = g["token_address"] or g["url"]
            if key and key not in seen:
                seen.add(key)
                results.append(g)
    except Exception as e:
        print(f"[GKO] Tier0 error: {e}")

    # Tier 1: Pump.fun (when not blocked)
    try:
        for g in scan_pumpfun_launches(max_results=5):
            key = g["token_address"]
            if key and key not in seen:
                seen.add(key)
                results.append(g)
    except Exception as e:
        print(f"[PF] Tier1 error: {e}")

    # Tier 2: DEX Screener profiles (fallback)
    try:
        for g in scan_dex_new_gems(max_results=5):
            key = g["token_address"]
            if key and key not in seen:
                seen.add(key)
                results.append(g)
    except Exception as e:
        print(f"[DEX] Tier2 error: {e}")

    results.sort(key=lambda x: x["gem_score"], reverse=True)
    return results[:max_results]
