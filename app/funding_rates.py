"""
Binance futures funding rates + open interest.
Only works for coins listed on Binance perpetual futures.
"""
import time
import requests
import urllib3

from app.config import SSL_VERIFY

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FUNDING_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
OI_URL      = "https://fapi.binance.com/fapi/v1/openInterest"
CACHE_TTL   = 300  # 5 minutes

_cache:    dict  = {}
_cache_ts: float = 0.0


def _get_funding_rate(pair: str) -> float:
    try:
        r = requests.get(
            FUNDING_URL,
            params={"symbol": pair, "limit": 1},
            timeout=8,
            verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                return float(data[-1].get("fundingRate", 0))
    except Exception:
        pass
    return None


def _get_open_interest(pair: str) -> float:
    try:
        r = requests.get(
            OI_URL,
            params={"symbol": pair},
            timeout=8,
            verify=SSL_VERIFY,
        )
        if r.status_code == 200:
            return float(r.json().get("openInterest", 0))
    except Exception:
        pass
    return 0.0


def get_funding_data(symbol: str) -> dict:
    global _cache, _cache_ts
    symbol = symbol.upper()
    now    = time.time()

    if symbol in _cache and now - _cache_ts < CACHE_TTL:
        return _cache[symbol]

    pair = symbol + "USDT"
    rate = _get_funding_rate(pair)

    if rate is None:
        result = {
            "available": False,
            "funding_rate": 0.0,
            "open_interest": 0.0,
            "signal": "no_data",
            "score_adj": 0,
            "reason": [],
        }
        _cache[symbol] = result
        _cache_ts = now
        return result

    oi      = _get_open_interest(pair)
    signal  = "neutral"
    adj     = 0
    reason  = []

    rate_pct = rate * 100
    if rate_pct > 0.10:
        signal = "overheated"
        adj    = -15
        reason.append(f"Very high funding {rate_pct:.3f}%/8h - longs overextended")
    elif rate_pct > 0.05:
        signal = "bullish_funded"
        adj    = -5
        reason.append(f"High funding {rate_pct:.3f}%/8h - cautious long bias")
    elif rate_pct < -0.05:
        signal = "short_squeeze_risk"
        adj    = +10
        reason.append(f"Negative funding {rate_pct:.3f}%/8h - shorts paying, squeeze risk")
    elif rate_pct < -0.10:
        signal = "extreme_short"
        adj    = +15
        reason.append(f"Extreme negative funding {rate_pct:.3f}%/8h - heavy shorting")
    else:
        signal = "balanced"
        reason.append(f"Balanced funding {rate_pct:.3f}%/8h - no extreme bias")

    result = {
        "available":      True,
        "funding_rate":   round(rate_pct, 4),
        "open_interest":  oi,
        "signal":         signal,
        "score_adj":      adj,
        "reason":         reason,
    }
    _cache[symbol]    = result
    _cache_ts         = now
    return result


def format_funding_text(symbol: str) -> str:
    d = get_funding_data(symbol)
    if not d["available"]:
        return f"{symbol} is not listed on Binance futures."

    oi = d["open_interest"]
    if oi >= 1e9:
        oi_str = f"{oi/1e9:.2f}B"
    elif oi >= 1e6:
        oi_str = f"{oi/1e6:.1f}M"
    else:
        oi_str = f"{oi:,.0f}"

    label = d["signal"].replace("_", " ").title()
    lines = [
        f"Funding Rates: {symbol}",
        f"Rate: {d['funding_rate']:+.4f}% per 8h",
        f"Open Interest: {oi_str}",
        f"Signal: {label}",
    ]
    for r in d["reason"]:
        lines.append(f"Note: {r}")
    return "\n".join(lines)
