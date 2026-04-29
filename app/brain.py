"""
Brain module — orchestrates all intelligence signals into a single
brain_score, brain_signal, and brain_reason for each coin.

Default signal weights (calm regime):
    Technical Analysis (RSI, MA, pattern, volume): 30%
    AI prediction (pump_probability_6h):           20%
    News sentiment (CryptoPanic):                  20%
    Social sentiment (Reddit):                     15%
    Whale / volume activity:                       10%
    Memory / consistency tracking:                  5%

Dynamic weighting (when quant_core is available and entropy > 0.50):
    Reduce TA/ML weight; reallocate to whales + news. Rationale: during
    high-dispersion regimes short-horizon predictive models lose edge,
    while flow/fundamental signals stay informative.

Confidence haircut (entropy > 0.70):
    brain_score multiplied by 0.9 to mark the output as low-confidence.

The C++ extension `quant_core` is OPTIONAL. If it isn't built, brain.py
falls back to the original static weights with no behavior change.
Build it from the project root with:
    python setup_quant.py build_ext --inplace
"""
import time
from dataclasses import dataclass, field

from app.technical_analysis import get_technical_signals
from app.news_scanner       import get_coin_news
from app.social_scanner     import get_social_data
from app.whale_tracker      import get_whale_signal
from app.memory_store       import get_memory_score, init_memory_table, update_coin_memory
from app.funding_rates      import get_funding_data
from app.fear_greed         import get_fear_greed

# ----------------------------------------------------------------------
# Optional native MC extension. Built via setup_quant.py at project root.
# If not compiled, brain.py degrades to the original static weights with
# no behavior change.
# ----------------------------------------------------------------------
try:
    import quant_core  # type: ignore
    QUANT_AVAILABLE = True
except ImportError:
    quant_core = None  # type: ignore
    QUANT_AVAILABLE = False
    print(
        "[BRAIN] quant_core native extension not found — using static weights. "
        "Run `python setup_quant.py build_ext --inplace` from the project root to enable dynamic weighting."
    )


CACHE_TTL = 300  # 5 minutes
_brain_cache:    dict  = {}
_brain_cache_ts: float = 0.0

# Sentiment → score mapping
_SENTIMENT_SCORE = {"bullish": 75, "neutral": 50, "bearish": 25}

# ----------------------------------------------------------------------
# Weight schedules — each schedule must sum to 1.0
# ----------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    # News is pinned at 0.0 because the CryptoPanic free tier was killed
    # and the source is short-circuited in news_scanner. The freed 0.20
    # split evenly between TA (the strongest single signal in calm regimes)
    # and Whale (smart-money flow). Flip news back on if/when a replacement
    # provider is wired up.
    "ta":     0.40,
    "ai":     0.20,
    "news":   0.00,
    "social": 0.15,
    "whale":  0.20,
    "memory": 0.05,
}

VOLATILE_WEIGHTS = {
    # High-dispersion regime: short-horizon predictive signals (TA / ML)
    # get punished; flow signals dominate. With news now dead, the freed
    # 0.25 mostly goes to whale (the canonical "flow stays informative"
    # anchor when models break) with a small bump to social for the
    # narrative component. Net effect: whale weight doubles when entropy
    # crosses the volatile threshold (20% calm → 40% volatile).
    "ta":     0.20,
    "ai":     0.15,
    "news":   0.00,
    "social": 0.20,
    "whale":  0.40,
    "memory": 0.05,
}

assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
assert abs(sum(VOLATILE_WEIGHTS.values()) - 1.0) < 1e-9

# Entropy thresholds (output of quant_core.calculate_entropy is in [0, 1])
ENTROPY_VOLATILE_THRESHOLD     = 0.50  # weight shift kicks in above this
ENTROPY_HIGH_UNCERTAINTY_LIMIT = 0.70  # confidence haircut above this

# Number of MC paths per call. The native side runs ~100k paths in <50ms.
N_PATHS = 100_000


@dataclass
class QuantStatus:
    """Aggregator status reported alongside brain_score."""
    entropy: float = 0.0
    volatile: bool = False
    high_uncertainty: bool = False
    weights: dict = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    available: bool = QUANT_AVAILABLE


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _label(score: int) -> str:
    if score >= 75: return "Strong Buy"
    if score >= 62: return "Buy"
    if score >= 50: return "Watch"
    if score >= 38: return "Neutral"
    return "Caution"


def _annualized_vol(coin_data: dict) -> float:
    """
    Annualized volatility for the Monte Carlo input.

    Preference order:
        1. Rolling realized vol from `technical_analysis._compute_realized_vol`,
           injected into coin_data as `realized_volatility`. This is the std-dev
           of log returns over the last ~50 snapshots, annualized.
        2. Fallback proxy: abs(price_change_24h)/100 treated as a one-day move
           and annualized via sqrt(365). Used when the TA pipeline doesn't
           have enough history yet (new symbol, fresh DB, etc.).

    Always returns a positive float capped at 5.0 (500%/yr) to keep the
    downstream MC well-conditioned.
    """
    rv = coin_data.get("realized_volatility")
    if rv is not None:
        try:
            rv_f = float(rv)
            if rv_f > 0.0:
                return min(rv_f, 5.0)
        except (TypeError, ValueError):
            pass

    # Fallback: 24h-percentage proxy.
    pct = abs(float(coin_data.get("price_change_24h", 0) or 0))
    daily_vol = max(pct / 100.0, 0.05)
    return daily_vol * (365.0 ** 0.5)


def _quant_status(coin_data: dict) -> QuantStatus:
    """
    Compute entropy via quant_core (if compiled) and choose a weight schedule.
    Falls back to DEFAULT_WEIGHTS on any failure — never raises into the
    aggregator hot path.
    """
    if not QUANT_AVAILABLE:
        return QuantStatus()

    try:
        price = float(coin_data.get("current_price", 0) or 0)
        if price <= 0:
            return QuantStatus()
        vol = _annualized_vol(coin_data)
        entropy = float(quant_core.calculate_entropy(price, vol, N_PATHS))
    except Exception as e:
        print(f"[BRAIN] quant_core call failed: {e!r} — falling back to static weights")
        return QuantStatus()

    is_volatile = entropy > ENTROPY_VOLATILE_THRESHOLD
    is_high_unc = entropy > ENTROPY_HIGH_UNCERTAINTY_LIMIT
    weights = dict(VOLATILE_WEIGHTS if is_volatile else DEFAULT_WEIGHTS)
    return QuantStatus(
        entropy=entropy,
        volatile=is_volatile,
        high_uncertainty=is_high_unc,
        weights=weights,
        available=True,
    )


# ----------------------------------------------------------------------
# Main analysis
# ----------------------------------------------------------------------
def analyze_coin_brain(symbol: str, coin_data: dict = None) -> dict:
    """
    Full brain analysis for a single symbol.
    coin_data is an optional dict from the scan pipeline that provides
    pump_probability_6h, signal_type, final_score, current_price, etc.
    """
    symbol = symbol.upper()
    coin_data = coin_data or {}

    # Pull TA early. The TA module is cached for 10 min and computes
    # rolling realized volatility — we feed that into _quant_status as the
    # Monte Carlo vol input, falling back to the 24h-pct proxy when the TA
    # pipeline doesn't have enough history.
    try:
        ta_all = get_technical_signals([symbol])
    except Exception:
        ta_all = {}
    ta = ta_all.get(symbol, {})

    realized_vol = float(ta.get("realized_vol_annualized", 0.0) or 0.0)
    if realized_vol > 0:
        coin_data = {**coin_data, "realized_volatility": realized_vol}

    qstatus = _quant_status(coin_data)
    weights = qstatus.weights

    reasons = []
    total   = 0.0   # weighted sum
    weight  = 0.0   # accumulated weight

    # 1. Technical Analysis (TA already fetched above; reuse the dict)
    try:
        ta_score = float(ta.get("tech_score", 50))
        ta_reasons = ta.get("tech_reason", [])
        total  += ta_score * weights["ta"]
        weight += weights["ta"]
        for r in ta_reasons[:2]:
            reasons.append(f"TA: {r}")
    except Exception:
        ta_score = 50
        total  += 50 * weights["ta"]
        weight += weights["ta"]

    # 2. AI Prediction
    prob = float(coin_data.get("pump_probability_6h", 0) or 0)
    if prob > 0:
        ai_score = prob * 100
        total    += ai_score * weights["ai"]
        weight   += weights["ai"]
        if prob >= 0.70:
            reasons.append(f"AI: {prob:.0%} pump probability (strong)")
        elif prob >= 0.55:
            reasons.append(f"AI: {prob:.0%} pump probability")
    else:
        total  += 50 * weights["ai"]
        weight += weights["ai"]

    # 3. News sentiment
    news = {}
    try:
        news = get_coin_news(symbol)
        news_sent  = news.get("sentiment", "neutral")
        news_score = _SENTIMENT_SCORE.get(news_sent, 50)
        news_count = news.get("count", 0)
        total  += news_score * weights["news"]
        weight += weights["news"]
        if news_count > 0:
            reasons.append(f"News: {news_count} articles, {news_sent} sentiment")
    except Exception:
        total  += 50 * weights["news"]
        weight += weights["news"]

    # 4. Social / Reddit
    social = {}
    try:
        social      = get_social_data(symbol)
        social_sent = social.get("sentiment", "neutral")
        soc_score   = _SENTIMENT_SCORE.get(social_sent, 50)
        mentions    = social.get("mentions", 0)
        total  += soc_score * weights["social"]
        weight += weights["social"]
        if mentions > 0:
            reasons.append(f"Reddit: {mentions} mentions, {social_sent}")
    except Exception:
        total  += 50 * weights["social"]
        weight += weights["social"]

    # 5. Whale / volume activity
    whale = {}
    try:
        whale      = get_whale_signal(symbol)
        wh_score   = float(whale.get("whale_score", 50))
        wh_signal  = whale.get("whale_signal", "normal")
        wh_reasons = whale.get("whale_reason", [])
        total  += wh_score * weights["whale"]
        weight += weights["whale"]
        if wh_signal not in ("normal",):
            reasons.append(f"Whale: {wh_signal.replace('_', ' ').title()}")
        elif wh_reasons:
            reasons.append(f"Volume: {wh_reasons[0]}")
    except Exception:
        total  += 50 * weights["whale"]
        weight += weights["whale"]

    # 6. Memory / consistency
    try:
        mem      = get_memory_score(symbol)
        mem_sc   = float(mem.get("memory_score", 50))
        mem_reas = mem.get("memory_reason", [])
        total  += mem_sc * weights["memory"]
        weight += weights["memory"]
        for r in mem_reas[:1]:
            reasons.append(f"Memory: {r}")
    except Exception:
        total  += 50 * weights["memory"]
        weight += weights["memory"]

    # 7. Funding rates (modifier on top, no weight)
    funding_signal = "no_data"
    funding_rate   = 0.0
    brain_score_adj = 0
    try:
        funding = get_funding_data(symbol)
        if funding.get("available"):
            funding_signal = funding.get("signal", "neutral")
            funding_rate   = funding.get("funding_rate", 0.0)
            brain_score_adj = funding.get("score_adj", 0)
            for r in funding.get("reason", [])[:1]:
                reasons.append(f"Funding: {r}")
    except Exception:
        pass

    # 8. Fear & Greed market context (modifier, no weight)
    fg_value = 50
    try:
        fg       = get_fear_greed()
        fg_value = fg.get("value", 50)
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
    brain_score += brain_score_adj
    if fg_value <= 20:
        brain_score += 5
    elif fg_value >= 80:
        brain_score -= 5

    base_score = float(coin_data.get("final_score", 0) or 0)
    if base_score > 0 and base_score < 0.40:
        brain_score = max(0, brain_score - 10)
        reasons.append(f"Low base score ({base_score:.2f}) - penalty applied")

    # Quant-driven annotations + confidence haircut
    if qstatus.high_uncertainty:
        before = brain_score
        brain_score = int(round(brain_score * 0.9))
        reasons.append(
            f"High simulated dispersion (entropy={qstatus.entropy:.2f}) - "
            f"confidence reduced ({before} → {brain_score})"
        )
    elif qstatus.volatile:
        reasons.append(
            f"Volatile regime (entropy={qstatus.entropy:.2f}) - "
            f"weights shifted toward whales/news"
        )

    # Update memory store with this cycle's data
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
        "symbol":         symbol,
        "brain_score":    brain_score,
        "brain_signal":   _label(brain_score),
        "brain_reason":   reasons[:5],
        "ta_score":       int(ta_score),
        "ai_prob":        round(prob, 3),
        "news_sent":      news.get("sentiment", "neutral"),
        "social_sent":    social.get("sentiment", "neutral"),
        "whale_signal":   whale.get("whale_signal", "normal"),
        "funding_signal": funding_signal,
        "funding_rate":   funding_rate,
        "fear_greed":     fg_value,
        # quant fields (always present; zero/false when extension unavailable)
        "entropy":          round(qstatus.entropy, 3),
        "quant_volatile":   qstatus.volatile,
        "quant_active":     qstatus.available,
        "weights_used":     weights,
        "realized_vol":     round(realized_vol, 3),  # 0.0 when TA had no history
        "vol_source":       "realized" if realized_vol > 0 else "24h_proxy",
    }


def get_brain_report(coin_list: list) -> dict:
    """
    coin_list: list of dicts from the scan pipeline, each with 'symbol' key.
    Returns a dict keyed by symbol with brain analysis.
    """
    global _brain_cache, _brain_cache_ts
    now = time.time()

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
    if data.get("quant_active"):
        regime = "VOLATILE" if data.get("quant_volatile") else "calm"
        rv = data.get("realized_vol", 0.0)
        src = data.get("vol_source", "24h_proxy")
        if rv > 0:
            lines.append(f"Quant: entropy={data['entropy']:.2f} ({regime}) | vol={rv:.0%} ({src})")
        else:
            lines.append(f"Quant: entropy={data['entropy']:.2f} ({regime}) | vol={src}")
    return "\n".join(lines)
