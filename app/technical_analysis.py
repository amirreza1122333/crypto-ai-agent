import pandas as pd
from app.data_store import load_all_data

_ta_cache = {}
_ta_cache_ts = 0
TA_CACHE_TTL = 600  # 10 minutes


def _compute_rsi(prices: pd.Series, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = prices.diff().dropna()
    gain = deltas.where(deltas > 0, 0.0).rolling(period).mean()
    loss = (-deltas.where(deltas < 0, 0.0)).rolling(period).mean()
    last_loss = loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = gain.iloc[-1] / last_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def _detect_pattern(prices: pd.Series) -> str:
    if len(prices) < 6:
        return "insufficient data"
    p = prices.values
    # Higher highs + higher lows = uptrend
    highs = [p[i] > p[i-2] for i in range(2, len(p), 2)]
    lows  = [p[i] > p[i-2] for i in range(1, len(p), 2)]
    if sum(highs) >= len(highs) * 0.7 and sum(lows) >= len(lows) * 0.7:
        return "uptrend"
    if sum(highs) <= len(highs) * 0.3 and sum(lows) <= len(lows) * 0.3:
        return "downtrend"
    # Consolidation: recent range < 5%
    recent = prices.iloc[-6:]
    spread = (recent.max() - recent.min()) / (recent.mean() + 1e-10)
    if spread < 0.05:
        return "consolidation"
    return "choppy"


def analyze_coin(group: pd.DataFrame) -> dict:
    group = group.sort_values("ts").reset_index(drop=True)
    prices  = pd.to_numeric(group["current_price"],  errors="coerce").fillna(0)
    volumes = pd.to_numeric(group["total_volume"],   errors="coerce").fillna(0)
    scores  = pd.to_numeric(group.get("final_score", pd.Series(dtype=float)), errors="coerce").fillna(0)

    result = {
        "rsi": 50.0,
        "ma_signal": "neutral",
        "volume_spike": False,
        "volume_ratio": 1.0,
        "pattern": "insufficient data",
        "score_trend": "stable",
        "tech_score": 50,
        "tech_reason": [],
    }

    if len(prices) < 3:
        return result

    # RSI
    if len(prices) >= 15:
        rsi = _compute_rsi(prices)
        result["rsi"] = rsi
        if rsi < 30:
            result["tech_score"] += 20
            result["tech_reason"].append(f"RSI oversold ({rsi:.0f}) - bounce zone")
        elif rsi < 45:
            result["tech_score"] += 8
            result["tech_reason"].append(f"RSI low ({rsi:.0f}) - recovery potential")
        elif rsi > 75:
            result["tech_score"] -= 12
            result["tech_reason"].append(f"RSI overbought ({rsi:.0f}) - overextended")
        elif rsi > 60:
            result["tech_score"] += 5

    # MA crossover
    if len(prices) >= 10:
        ma5  = prices.rolling(5).mean().iloc[-1]
        ma10 = prices.rolling(10).mean().iloc[-1]
        if pd.notna(ma5) and pd.notna(ma10) and ma10 > 0:
            if ma5 > ma10 * 1.01:
                result["ma_signal"] = "bullish"
                result["tech_score"] += 15
                result["tech_reason"].append("Golden cross (MA5 > MA10)")
            elif ma5 < ma10 * 0.99:
                result["ma_signal"] = "bearish"
                result["tech_score"] -= 12
                result["tech_reason"].append("Death cross (MA5 < MA10)")

    # Volume spike
    if len(volumes) >= 4:
        avg_vol  = volumes.iloc[:-1].mean()
        last_vol = volumes.iloc[-1]
        if avg_vol > 0:
            ratio = last_vol / avg_vol
            result["volume_ratio"] = round(ratio, 2)
            if ratio >= 3.0:
                result["volume_spike"] = True
                result["tech_score"] += 20
                result["tech_reason"].append(f"Volume spike {ratio:.1f}x avg")
            elif ratio >= 2.0:
                result["volume_spike"] = True
                result["tech_score"] += 10
                result["tech_reason"].append(f"High volume {ratio:.1f}x avg")

    # Chart pattern
    if len(prices) >= 6:
        pattern = _detect_pattern(prices.iloc[-10:])
        result["pattern"] = pattern
        if pattern == "uptrend":
            result["tech_score"] += 10
            result["tech_reason"].append("Uptrend pattern confirmed")
        elif pattern == "downtrend":
            result["tech_score"] -= 10
            result["tech_reason"].append("Downtrend pattern detected")
        elif pattern == "consolidation":
            result["tech_score"] += 5
            result["tech_reason"].append("Consolidation (potential breakout)")

    # Score trend (is the AI score improving?)
    if len(scores) >= 3 and scores.iloc[-1] > 0:
        delta = scores.iloc[-1] - scores.iloc[-3]
        if delta > 0.05:
            result["score_trend"] = "improving"
            result["tech_score"] += 8
            result["tech_reason"].append("Score improving over last 3 scans")
        elif delta < -0.05:
            result["score_trend"] = "degrading"
            result["tech_score"] -= 8

    result["tech_score"] = max(0, min(100, result["tech_score"]))
    return result


def get_technical_signals(symbols: list = None) -> dict:
    import time
    global _ta_cache, _ta_cache_ts

    now = time.time()
    if now - _ta_cache_ts < TA_CACHE_TTL and _ta_cache:
        if symbols is None:
            return _ta_cache
        return {s.upper(): _ta_cache[s.upper()] for s in symbols if s.upper() in _ta_cache}

    try:
        df = load_all_data()
        if df.empty:
            return {}

        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
        df = df.dropna(subset=["ts"]).sort_values("ts")
        df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()

        results = {}
        for sym, group in df.groupby("symbol"):
            if symbols and sym not in [s.upper() for s in symbols]:
                continue
            results[sym] = analyze_coin(group.tail(50))

        _ta_cache = results
        _ta_cache_ts = now
        return results

    except Exception as e:
        print(f"[TA] Error: {e}")
        return {}
