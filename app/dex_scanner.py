import time
import requests
import urllib3
from datetime import datetime, timezone

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEXSCREENER_BASE = "https://api.dexscreener.com"

TARGET_CHAINS = {"ethereum", "bsc", "solana", "base", "arbitrum"}

MIN_LIQUIDITY_USD = 30_000
MIN_VOLUME_24H    = 30_000
MAX_FDV           = 50_000_000
MAX_AGE_HOURS     = 168        # 7 days
MIN_PRICE_CHANGE  = 20.0       # at least +20% in 24h
GEM_ALERT_SCORE   = 65         # minimum score to send alert


def _get(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=15, verify=False)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[DEX] Request failed {url}: {e}")
        return None


def fetch_latest_profiles():
    data = _get(f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    if isinstance(data, list):
        return data
    return []


def fetch_token_pairs(token_address: str):
    data = _get(f"{DEXSCREENER_BASE}/latest/dex/tokens/{token_address}")
    if data:
        return data.get("pairs", []) or []
    return []


def get_age_hours(pair: dict) -> float:
    created_at = pair.get("pairCreatedAt")
    if not created_at:
        return 9999
    try:
        created_dt = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        return (now - created_dt).total_seconds() / 3600
    except Exception:
        return 9999


def score_gem(pair: dict) -> int:
    score = 0

    liquidity   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    volume_24h  = float(pair.get("volume", {}).get("h24", 0) or 0)
    ch24        = float(pair.get("priceChange", {}).get("h24", 0) or 0)
    ch1         = float(pair.get("priceChange", {}).get("h1", 0) or 0)
    ch6         = float(pair.get("priceChange", {}).get("h6", 0) or 0)
    buys        = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
    sells       = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)
    fdv         = float(pair.get("fdv", 0) or 0)

    # Volume/Liquidity ratio — high means hot
    if liquidity > 0:
        ratio = volume_24h / liquidity
        score += min(int(ratio * 8), 25)

    # 24h price momentum
    if ch24 >= 100:
        score += 25
    elif ch24 >= 50:
        score += 18
    elif ch24 >= 20:
        score += 10

    # 6h momentum
    if ch6 >= 30:
        score += 12
    elif ch6 >= 10:
        score += 6

    # 1h momentum
    if ch1 >= 15:
        score += 12
    elif ch1 >= 5:
        score += 6

    # Buy pressure
    total = buys + sells
    if total > 0:
        buy_ratio = buys / total
        if buy_ratio >= 0.70:
            score += 15
        elif buy_ratio >= 0.55:
            score += 8

    # Liquidity safety
    if liquidity >= 200_000:
        score += 10
    elif liquidity >= 100_000:
        score += 5
    elif liquidity >= 50_000:
        score += 2

    # Low FDV = more room to grow
    if 0 < fdv < 1_000_000:
        score += 5
    elif fdv < 5_000_000:
        score += 3

    return min(score, 100)


def is_rug_risk(pair: dict) -> bool:
    liquidity = float(pair.get("liquidity", {}).get("usd", 0) or 0)
    buys  = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
    sells = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)

    if liquidity < MIN_LIQUIDITY_USD:
        return True

    total = buys + sells
    if total > 20 and sells > buys * 2.5:
        return True

    return False


def scan_new_gems(max_results: int = 10) -> list:
    profiles = fetch_latest_profiles()
    if not profiles:
        print("[DEX] No profiles returned")
        return []

    gems = []
    seen = set()

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
            time.sleep(0.2)
            continue

        # Keep only pairs on the same chain, pick highest liquidity
        chain_pairs = [p for p in pairs if p.get("chainId") == chain_id]
        if not chain_pairs:
            time.sleep(0.2)
            continue

        pair = max(chain_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        age_hours  = get_age_hours(pair)
        liquidity  = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        volume_24h = float(pair.get("volume", {}).get("h24", 0) or 0)
        ch24       = float(pair.get("priceChange", {}).get("h24", 0) or 0)
        fdv        = float(pair.get("fdv", 0) or 0)

        # Apply filters
        if age_hours  > MAX_AGE_HOURS:    time.sleep(0.2); continue
        if liquidity  < MIN_LIQUIDITY_USD:time.sleep(0.2); continue
        if volume_24h < MIN_VOLUME_24H:   time.sleep(0.2); continue
        if ch24       < MIN_PRICE_CHANGE: time.sleep(0.2); continue
        if fdv        > MAX_FDV:          time.sleep(0.2); continue
        if is_rug_risk(pair):             time.sleep(0.2); continue

        gem_score = score_gem(pair)

        base  = pair.get("baseToken", {})
        buys  = int(pair.get("txns", {}).get("h24", {}).get("buys", 0) or 0)
        sells = int(pair.get("txns", {}).get("h24", {}).get("sells", 0) or 0)

        gems.append({
            "token_address":  token_address,
            "chain":          chain_id,
            "name":           base.get("name", "Unknown"),
            "symbol":         base.get("symbol", "?"),
            "price_usd":      float(pair.get("priceUsd", 0) or 0),
            "price_change_24h": ch24,
            "price_change_6h":  float(pair.get("priceChange", {}).get("h6", 0) or 0),
            "price_change_1h":  float(pair.get("priceChange", {}).get("h1", 0) or 0),
            "volume_24h":     volume_24h,
            "liquidity_usd":  liquidity,
            "fdv":            fdv,
            "age_hours":      age_hours,
            "buys_24h":       buys,
            "sells_24h":      sells,
            "gem_score":      gem_score,
            "url":            pair.get("url", ""),
        })

        time.sleep(0.3)

    gems.sort(key=lambda x: x["gem_score"], reverse=True)
    return gems[:max_results]
