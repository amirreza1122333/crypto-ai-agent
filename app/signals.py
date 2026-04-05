import math


def _safe_num(value, default=0.0):
    try:
        if value is None:
            return float(default)
        if isinstance(value, float) and math.isnan(value):
            return float(default)
        return float(value)
    except Exception:
        return float(default)


# -----------------------------
# FILTER
# -----------------------------
EXCLUDED_SYMBOLS = {
    "XAUT", "PAXG", "USDT", "USDC", "FDUSD", "TUSD",
    "USDE", "DAI", "BUSD", "USDP", "GUSD", "LUSD",
    "EURC", "PYUSD"
}

EXCLUDED_NAMES = {
    "Tether Gold", "PAX Gold", "Tether USDt", "USD Coin",
    "First Digital USD", "TrueUSD", "Ethena USDe",
    "Dai", "Binance USD", "PayPal USD"
}


def is_excluded_asset(row):
    symbol = str(row.get("symbol", "")).upper().strip()
    name = str(row.get("name", "")).strip()
    return symbol in EXCLUDED_SYMBOLS or name in EXCLUDED_NAMES


# -----------------------------
# TREND
# -----------------------------
def classify_trend(row):
    ch24 = _safe_num(row.get("price_change_percentage_24h"))
    ch7 = _safe_num(row.get("price_change_percentage_7d_in_currency"))

    if ch24 > 0 and ch7 > 0:
        if ch24 >= 4 and ch7 >= 10:
            return "Bullish"
        return "Positive"
    if ch24 > 0 or ch7 > 0:
        return "Neutral"
    return "Weak"


# -----------------------------
# RISK
# -----------------------------
def classify_risk_v2(row):
    mcap = _safe_num(row.get("market_cap"))
    vol = _safe_num(row.get("total_volume"))
    vtm = _safe_num(row.get("volume_to_mcap"))
    bucket = str(row.get("bucket", "")).lower()

    if bucket == "safer" and mcap >= 1_000_000_000 and vol >= 10_000_000:
        return "Low"

    if mcap >= 300_000_000 and vol >= 3_000_000 and vtm >= 0.01:
        return "Medium"

    return "High"


# -----------------------------
# CONFIDENCE
# -----------------------------
def compute_confidence(row):
    score = _safe_num(row.get("final_score", row.get("score", 0)))
    ch24 = _safe_num(row.get("price_change_percentage_24h"))
    ch7 = _safe_num(row.get("price_change_percentage_7d_in_currency"))
    vol = _safe_num(row.get("total_volume"))
    mcap = _safe_num(row.get("market_cap"))
    vtm = _safe_num(row.get("volume_to_mcap"))

    conf = int((score * 85) + 8)

    if ch24 > 0 and ch7 > 0:
        conf += 6
    elif ch24 < 0 and ch7 < 0:
        conf -= 6

    if vol >= 10_000_000:
        conf += 3
    if vtm >= 0.03:
        conf += 3
    if mcap < 100_000_000:
        conf -= 5

    return max(1, min(conf, 99))


# -----------------------------
# ACTION
# -----------------------------
def classify_action(row):
    score = _safe_num(row.get("final_score", row.get("score", 0)))
    trend = str(row.get("trend", ""))
    risk = str(row.get("risk_level", "High"))
    confidence = int(_safe_num(row.get("confidence", 0)))

    if (
        score >= 0.62
        and confidence >= 72
        and trend in ["Bullish", "Positive"]
        and risk in ["Low", "Medium"]
    ):
        return "Consider"

    if (
        score < 0.44
        or trend == "Weak"
        or confidence < 42
        or (risk == "High" and score < 0.54)
    ):
        return "Avoid"

    return "Watch"


# -----------------------------
# SIGNAL TYPE (LEVEL 3)
# -----------------------------
def classify_signal_type(row):
    score = _safe_num(row.get("final_score", row.get("score", 0)))
    trend = str(row.get("trend", ""))
    risk = str(row.get("risk_level", "High"))
    confidence = int(_safe_num(row.get("confidence", 0)))
    ch24 = _safe_num(row.get("price_change_percentage_24h"))
    ch7 = _safe_num(row.get("price_change_percentage_7d_in_currency"))
    vtm = _safe_num(row.get("volume_to_mcap"))

    if (
        score >= 0.70
        and confidence >= 80
        and trend == "Bullish"
        and risk in ["Low", "Medium"]
    ):
        return "Strong Consider"

    if (
        trend in ["Bullish", "Positive"]
        and ch24 > 4
        and ch7 > 8
        and risk in ["Low", "Medium"]
    ):
        return "Breakout Watch"

    if (
        trend in ["Bullish", "Positive", "Neutral"]
        and score >= 0.60
        and confidence >= 68
        and risk in ["Low", "Medium"]
    ):
        return "Momentum Watch"

    if risk == "High" and ch24 > 6 and vtm >= 0.03:
        return "Risky Pump"

    return "Avoid"


def is_alert_candidate(row):
    signal_type = str(row.get("signal_type", ""))
    action = str(row.get("action", ""))
    confidence = int(_safe_num(row.get("confidence", 0)))

    if signal_type == "Strong Consider":
        return True
    if signal_type == "Breakout Watch" and confidence >= 75:
        return True
    if action == "Consider" and confidence >= 76:
        return True
    return False


# -----------------------------
# REASON
# -----------------------------
def build_reason(row):
    ch24 = _safe_num(row.get("price_change_percentage_24h"))
    ch7 = _safe_num(row.get("price_change_percentage_7d_in_currency"))
    vtm = _safe_num(row.get("volume_to_mcap"))
    rank = _safe_num(row.get("market_cap_rank"), 9999)
    risk = str(row.get("risk_level", ""))

    parts = []

    if ch7 > 10:
        parts.append("strong 7d momentum")
    elif ch7 > 0:
        parts.append("positive 7d trend")
    else:
        parts.append("negative 7d trend")

    if ch24 > 5:
        parts.append("strong 24h move")
    elif ch24 > 0:
        parts.append("mild 24h move")
    else:
        parts.append("weak 24h move")

    if vtm > 0.05:
        parts.append("very high liquidity")
    elif vtm > 0.02:
        parts.append("healthy liquidity")
    else:
        parts.append("low liquidity")

    if rank < 50:
        parts.append("large-cap")
    elif rank < 300:
        parts.append("mid-cap")
    else:
        parts.append("small-cap")

    parts.append(f"{risk} risk")

    return " | ".join(parts)