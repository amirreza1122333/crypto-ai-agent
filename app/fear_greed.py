import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

FEAR_GREED_URL = "https://api.alternative.me/fng/"
CACHE_TTL = 3600  # 1 hour

_cache: dict = {}
_cache_ts: float = 0.0


def get_fear_greed() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if _cache and now - _cache_ts < CACHE_TTL:
        return _cache

    try:
        r = requests.get(
            FEAR_GREED_URL,
            params={"limit": 1},
            timeout=10,
            verify=False,
        )
        data = r.json().get("data", [{}])[0]
        result = {
            "value": int(data.get("value", 50)),
            "label": data.get("value_classification", "Neutral"),
        }
        _cache    = result
        _cache_ts = now
        return result
    except Exception as e:
        print(f"[FG] Error: {e}")
        return {"value": 50, "label": "Neutral"}


def fear_greed_context(value: int) -> str:
    """Return market context string based on F&G value."""
    if value <= 20:   return "Extreme Fear - historically good buy zone"
    if value <= 40:   return "Fear - cautious but potential opportunity"
    if value <= 60:   return "Neutral - no strong market bias"
    if value <= 80:   return "Greed - market optimistic, watch for tops"
    return "Extreme Greed - high risk of correction"


def format_fear_greed() -> str:
    data  = get_fear_greed()
    value = data["value"]
    label = data["label"]
    bar   = "#" * (value // 10) + "-" * (10 - value // 10)
    ctx   = fear_greed_context(value)
    return (
        f"Fear & Greed Index\n"
        f"{value}/100  [{bar}]\n"
        f"Status: {label}\n"
        f"Context: {ctx}"
    )
