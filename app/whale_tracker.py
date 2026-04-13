import time
import pandas as pd
from app.data_store import load_all_data

CACHE_TTL = 600  # 10 minutes
_cache: dict = {}
_cache_ts: float = 0.0


def _analyze_whale_activity(group: pd.DataFrame) -> dict:
    group = group.sort_values("ts").reset_index(drop=True)
    prices  = pd.to_numeric(group["current_price"],  errors="coerce").fillna(0)
    volumes = pd.to_numeric(group["total_volume"],   errors="coerce").fillna(0)
    caps    = pd.to_numeric(group.get("market_cap", pd.Series(dtype=float)), errors="coerce").fillna(0)

    result = {
        "whale_signal":      "normal",
        "volume_anomaly":    False,
        "volume_ratio":      1.0,
        "price_spike":       False,
        "price_spike_pct":   0.0,
        "accumulation":      False,
        "distribution":      False,
        "whale_score":       50,
        "whale_reason":      [],
    }

    if len(volumes) < 4:
        return result

    # Volume anomaly: compare last reading to rolling average of prior readings
    avg_vol  = volumes.iloc[:-1].mean()
    last_vol = volumes.iloc[-1]
    if avg_vol > 0:
        ratio = last_vol / avg_vol
        result["volume_ratio"] = round(ratio, 2)
        if ratio >= 5.0:
            result["volume_anomaly"] = True
            result["whale_score"] += 25
            result["whale_reason"].append(f"Massive volume spike {ratio:.1f}x avg - possible whale entry")
        elif ratio >= 3.0:
            result["volume_anomaly"] = True
            result["whale_score"] += 15
            result["whale_reason"].append(f"Volume spike {ratio:.1f}x avg - unusual activity")
        elif ratio >= 2.0:
            result["whale_score"] += 8
            result["whale_reason"].append(f"Elevated volume {ratio:.1f}x avg")
        elif ratio < 0.3:
            result["whale_score"] -= 10
            result["whale_reason"].append("Very low volume - no interest")

    # Price spike detection: large move in last observation
    if len(prices) >= 3:
        prev_price = prices.iloc[-2]
        last_price = prices.iloc[-1]
        if prev_price > 0:
            pct = (last_price - prev_price) / prev_price * 100
            result["price_spike_pct"] = round(pct, 2)
            if pct >= 15:
                result["price_spike"] = True
                result["whale_score"] += 15
                result["whale_reason"].append(f"Price spike +{pct:.1f}% in last period")
            elif pct <= -15:
                result["price_spike"] = True
                result["whale_score"] -= 15
                result["whale_reason"].append(f"Price drop {pct:.1f}% in last period")

    # Accumulation: rising volume + rising price over last 5 periods
    if len(prices) >= 5 and len(volumes) >= 5:
        price_up  = prices.iloc[-1] > prices.iloc[-5]
        vol_up    = volumes.iloc[-3:].mean() > volumes.iloc[-6:-3].mean() if len(volumes) >= 6 else False
        price_down = prices.iloc[-1] < prices.iloc[-5]

        if price_up and vol_up:
            result["accumulation"] = True
            result["whale_score"] += 12
            result["whale_reason"].append("Accumulation pattern: rising price + rising volume")
        elif price_down and vol_up:
            result["distribution"] = True
            result["whale_score"] -= 12
            result["whale_reason"].append("Distribution pattern: falling price + rising volume")

    # Volume/market-cap ratio: unusual for large volume relative to mcap
    if len(caps) >= 2 and caps.iloc[-1] > 0 and last_vol > 0:
        vol_mcap_ratio = last_vol / caps.iloc[-1]
        if vol_mcap_ratio > 0.5:
            result["whale_score"] += 10
            result["whale_reason"].append(f"Vol/MCap ratio {vol_mcap_ratio:.2f} - very high turnover")
        elif vol_mcap_ratio > 0.2:
            result["whale_score"] += 5
            result["whale_reason"].append(f"Vol/MCap ratio {vol_mcap_ratio:.2f} - high turnover")

    # Derive overall signal
    score = result["whale_score"]
    if result["accumulation"] and result["volume_anomaly"]:
        result["whale_signal"] = "whale_accumulation"
    elif result["distribution"] and result["volume_anomaly"]:
        result["whale_signal"] = "whale_dump"
    elif result["volume_anomaly"]:
        result["whale_signal"] = "volume_anomaly"
    elif score >= 70:
        result["whale_signal"] = "bullish"
    elif score <= 30:
        result["whale_signal"] = "bearish"

    result["whale_score"] = max(0, min(100, result["whale_score"]))
    return result


def get_whale_signals(symbols: list = None) -> dict:
    global _cache, _cache_ts
    now = time.time()

    if now - _cache_ts < CACHE_TTL and _cache:
        if symbols is None:
            return _cache
        return {s.upper(): _cache[s.upper()] for s in symbols if s.upper() in _cache}

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
            results[sym] = _analyze_whale_activity(group.tail(30))

        _cache = results
        _cache_ts = now
        return results

    except Exception as e:
        print(f"[WHALE] Error: {e}")
        return {}


def get_whale_signal(symbol: str) -> dict:
    symbol = symbol.upper()
    signals = get_whale_signals([symbol])
    return signals.get(symbol, {
        "whale_signal": "normal",
        "volume_anomaly": False,
        "volume_ratio": 1.0,
        "price_spike": False,
        "price_spike_pct": 0.0,
        "accumulation": False,
        "distribution": False,
        "whale_score": 50,
        "whale_reason": [],
    })


def format_whale_text(symbol: str) -> str:
    data = get_whale_signal(symbol)
    signal = data["whale_signal"]
    label_map = {
        "whale_accumulation": "Whale Accumulation",
        "whale_dump":         "Whale Dump",
        "volume_anomaly":     "Volume Anomaly",
        "bullish":            "Bullish Activity",
        "bearish":            "Bearish Activity",
        "normal":             "Normal",
    }
    label = label_map.get(signal, "Normal")

    lines = [
        f"Whale Tracker: {symbol.upper()}",
        f"Signal: {label} | Score: {data['whale_score']}/100",
        f"Volume Ratio: {data['volume_ratio']:.2f}x avg",
    ]
    if data["price_spike"]:
        lines.append(f"Price Move: {data['price_spike_pct']:+.1f}%")
    if data["whale_reason"]:
        lines.append("")
        for r in data["whale_reason"]:
            lines.append(f"- {r}")
    return "\n".join(lines)
