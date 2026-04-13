"""
Brain module - orchestrates all intelligence signals into a single
brain_score, brain_signal, and brain_reason for each coin.

Signal sources and weights:
  - Technical Analysis (RSI, MA, pattern, volume): 30%
  - AI prediction (pump_probability_6h):           20%
  - News sentiment (CryptoPanic):                  20%
  - Social sentiment (Reddit):                     15%
  - Whale / volume activity:                       10%
  - Memory / consistency tracking:                  5%
"""
import time

from app.technical_analysis import get_technical_signals
from app.news_scanner       import get_coin_news
from app.social_scanner     import get_social_data
from app.whale_tracker      import get_whale_signal
from app.memory_store       import get_memory_score, init_memory_table, update_coin_memory
from app.funding_rates      import get_funding_data
from app.fear_greed         import get_fear_greed

CACHE_TTL = 300  # 5 minutes
_brain_cache:    dict  = {}
_brain_cache_ts: float = 0.0

# Sentiment → score mapping
_SENTIMENT_SCORE = {"bullish": 75, "neutral": 50, "bearish": 25}

# Signal thresholds for brain_signal label
def _label(score: int) -> str:
    if score >= 75: return "Strong Buy"
    if score >= 62: return "Buy"
    if score >= 50: return "Watch"
    if score >= 38: return "Neutral"
    return "Caution"


def analyze_coin_brain(symbol: str, coin_data: dict = None) -> dict:
    """
    Full brain analysis for a single symbol.
    coin_data is optional dict from the scan pipeline
    (provides pump_probability_6h, signal_type, final_score, etc.)
    """
    symbol = symbol.upper()
    coin_data = coin_data or {}

    reasons = []
    total   = 0.0   # weighted sum
    weight  = 0.0   # total weight accumulated

    # ------------------------------------------------------------------
    # 1. Technical Analysis  (weight 0.30)
    # ------------------------------------------------------------------
    try:
        ta_all = get_technical_signals([symbol])
        ta = ta_all.get(symbol, {})
        ta_score = float(ta.get("tech_score", 50))
        ta_reasons = ta.get("tech_reason", [])
        total  += ta_score * 0.30
        weight += 0.30
        for r in ta_reasons[:2]:
            reasons.append(f"TA: {r}")
    except Exception:
        ta_score = 50
        total  += 50 * 0.30
        weight += 0.30

    # ------------------------------------------------------------------
    # 2. AI Prediction  (weight 0.20)
    # ------------------------------------------------------------------
    prob = float(coin_data.get("pump_probability_6h", 0) or 0)
    ai_signal = str(coin_data.get("ai_signal", ""))
    if prob > 0:
        ai_score = prob * 100
        total    += ai_score * 0.20
        weight   += 0.20
        if prob >= 0.70:
            reasons.append(f"AI: {prob:.0%} pump probability (strong)")
        elif prob >= 0.55:
            reasons.append(f"AI: {prob:.0%} pump probability")
    else:
        total  += 50 * 0.20
        weight += 0.20

    # ------------------------------------------------------------------
    # 3. News sentiment  (weight 0.20)
    # ------------------------------------------------------------------
    try:
        news = get_coin_news(symbol)
        news_sent  = news.get("sentiment", "neutral")
        news_score = _SENTIMENT_SCORE.get(news_sent, 50)
        news_count = news.get("count", 0)
        total  += news_score * 0.20
        weight += 0.20
        if news_count > 0:
            reasons.append(f"News: {news_count} articles, {news_sent} sentiment")
    except Exception:
        total  += 50 * 0.20
        weight += 0.20

    # ------------------------------------------------------------------
    # 4. Social / Reddit  (weight 0.15)
    # ------------------------------------------------------------------
    try:
        social      = get_social_data(symbol)
        social_sent = social.get("sentiment", "neutral")
        soc_score   = _SENTIMENT_SCORE.get(social_sent, 50)
        mentions    = social.get("mentions", 0)
        total  += soc_score * 0.15
        weight += 0.15
        if mentions > 0:
            reasons.append(f"Reddit: {mentions} mentions, {social_sent}")
    except Exception:
        total  += 50 * 0.15
        weight += 0.15

    # ------------------------------------------------------------------
    # 5. Whale / volume activity  (weight 0.10)
    # ------------------------------------------------------------------
    try:
        whale      = get_whale_signal(symbol)
        wh_score   = float(whale.get("whale_score", 50))
        wh_signal  = whale.get("whale_signal", "normal")
        wh_reasons = whale.get("whale_reason", [])
        total  += wh_score * 0.10
        weight += 0.10
        if wh_signal not in ("normal",):
            reasons.append(f"Whale: {wh_signal.replace('_', ' ').title()}")
        elif wh_reasons:
            reasons.append(f"Volume: {wh_reasons[0]}")
    except Exception:
        total  += 50 * 0.10
        weight += 0.10

    # ------------------------------------------------------------------
    # 6. Memory / consistency  (weight 0.05)
    # ------------------------------------------------------------------
    try:
        mem      = get_memory_score(symbol)
        mem_sc   = float(mem.get("memory_score", 50))
        mem_reas = mem.get("memory_reason", [])
        total  += mem_sc * 0.05
        weight += 0.05
        for r in mem_reas[:1]:
            reasons.append(f"Memory: {r}")
    except Exception:
        total  += 50 * 0.05
        weight += 0.05

    # ------------------------------------------------------------------
    # 7. Funding rates (bonus/penalty on top, no weight - modifier only)
    # ------------------------------------------------------------------
    funding_signal = "no_data"
    funding_rate   = 0.0
    try:
        funding = get_funding_data(symbol)
        if funding.get("available"):
            funding_signal = funding.get("signal", "neutral")
            funding_rate   = funding.get("funding_rate", 0.0)
            adj            = funding.get("score_adj", 0)
            if adj != 0:
                brain_score_adj = adj   # applied after weight calc
                for r in funding.get("reason", [])[:1]:
                    reasons.append(f"Funding: {r}")
    except Exception:
        brain_score_adj = 0

    # ------------------------------------------------------------------
    # 8. Fear & Greed market context (modifier only, no weight)
    # ------------------------------------------------------------------
    fg_value = 50
    try:
        fg       = get_fear_greed()
        fg_value = fg.get("value", 50)
        # Extreme fear boosts score (buy opportunity), extreme greed penalizes
        if fg_value <= 20:
            reasons.append(f"Market in Extreme Fear ({fg_value}) - historically good entry")
        elif fg_value >= 80:
            reasons.append(f"Market in Extreme Greed ({fg_value}) - correction risk")
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Final score
    # ------------------------------------------------------------------
    brain_score = int(round(total / weight)) if weight > 0 else 50

    # Apply funding rate modifier
    try:
        brain_score += brain_score_adj
    except NameError:
        pass

    # Fear & Greed modifier
    if fg_value <= 20:
        brain_score += 5
    elif fg_value >= 80:
        brain_score -= 5

    # Penalty: if base scan score is very low
    base_score = float(coin_data.get("final_score", 0) or 0)
    if base_score > 0 and base_score < 0.40:
        brain_score = max(0, brain_score - 10)
        reasons.append(f"Low base score ({base_score:.2f}) - penalty applied")

    # Update memory with this cycle's data
    try:
        update_coin_memory(
            symbol,
            score  = base_score or brain_score / 100,
            signal = str(coin_data.get("signal_type", "")),
            price  = float(coin_data.get("current_price", 0) or 0),
        )
    except Exception:
        pass

    return {
        "symbol":        symbol,
        "brain_score":   brain_score,
        "brain_signal":  _label(brain_score),
        "brain_reason":  reasons[:5],   # top 5 reasons
        "ta_score":      int(ta_score),
        "ai_prob":       round(prob, 3),
        "news_sent":     news.get("sentiment", "neutral") if "news" in dir() else "neutral",
        "social_sent":   social.get("sentiment", "neutral") if "social" in dir() else "neutral",
        "whale_signal":   whale.get("whale_signal", "normal") if "whale" in dir() else "normal",
        "funding_signal": funding_signal,
        "funding_rate":   funding_rate,
        "fear_greed":     fg_value,
    }


def get_brain_report(coin_list: list) -> dict:
    """
    coin_list: list of dicts from the scan pipeline, each with 'symbol' key.
    Returns dict keyed by symbol with brain analysis.
    """
    global _brain_cache, _brain_cache_ts
    now = time.time()

    # Rebuild if cache expired
    if now - _brain_cache_ts > CACHE_TTL:
        _brain_cache = {}
        _brain_cache_ts = now

    results = {}
    for coin in coin_list:
        sym = str(coin.get("symbol", "")).upper()
        if not sym:
            continue
        if sym not in _brain_cache:
            _brain_cache[sym] = analyze_coin_brain(sym, coin)
        results[sym] = _brain_cache[sym]

    return results


def format_brain_text(symbol: str, coin_data: dict = None) -> str:
    data = analyze_coin_brain(symbol, coin_data or {})
    score  = data["brain_score"]
    signal = data["brain_signal"]
    bar    = "#" * (score // 10) + "-" * (10 - score // 10)

    lines = [
        f"Brain Analysis: {symbol.upper()}",
        f"Score: {score}/100  [{bar}]",
        f"Signal: {signal}",
        "",
    ]
    if data["brain_reason"]:
        lines.append("Signals detected:")
        for r in data["brain_reason"]:
            lines.append(f"  - {r}")
    else:
        lines.append("No strong signals found.")

    funding_label = data.get("funding_signal", "no_data").replace("_", " ").title()
    fg_val        = data.get("fear_greed", 50)
    funding_rate  = data.get("funding_rate", 0.0)

    lines += [
        "",
        f"TA: {data['ta_score']}/100 | AI: {data['ai_prob']:.0%}",
        f"News: {data['news_sent']} | Reddit: {data['social_sent']}",
        f"Whale: {data['whale_signal'].replace('_', ' ').title()}",
    ]
    if data.get("funding_signal", "no_data") != "no_data":
        lines.append(f"Funding: {funding_rate:+.4f}%/8h ({funding_label})")
    lines.append(f"Fear & Greed: {fg_val}/100")
    return "\n".join(lines)
