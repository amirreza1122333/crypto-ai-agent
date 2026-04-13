import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CRYPTOPANIC_URL = "https://cryptopanic.com/api/free/v1/posts/"
CACHE_TTL = 900  # 15 minutes

_cache: dict = {}
_cache_ts: float = 0.0


def _fetch_raw(symbol: str = None) -> list:
    params = {"public": "true", "kind": "news"}
    if symbol:
        params["currencies"] = symbol.upper()
    try:
        r = requests.get(CRYPTOPANIC_URL, params=params, timeout=10, verify=False)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception as e:
        print(f"[NEWS] Fetch failed: {e}")
        return []


def _parse_posts(posts: list) -> dict:
    news_by_coin: dict = {}
    for post in posts:
        currencies = post.get("currencies") or []
        votes      = post.get("votes") or {}
        pos        = int(votes.get("positive", 0) or 0)
        neg        = int(votes.get("negative", 0) or 0)
        title      = post.get("title", "")
        url        = post.get("url", "")

        for currency in currencies:
            code = str(currency.get("code", "")).upper()
            if not code:
                continue
            if code not in news_by_coin:
                news_by_coin[code] = {
                    "count": 0, "positive": 0, "negative": 0,
                    "headlines": [], "urls": [], "sentiment": "neutral",
                }
            d = news_by_coin[code]
            d["count"]    += 1
            d["positive"] += pos
            d["negative"] += neg
            if len(d["headlines"]) < 3:
                d["headlines"].append(title)
                d["urls"].append(url)

    for code, d in news_by_coin.items():
        p, n = d["positive"], d["negative"]
        if p + n > 0:
            if p > n * 1.5:
                d["sentiment"] = "bullish"
            elif n > p * 1.5:
                d["sentiment"] = "bearish"

    return news_by_coin


def fetch_all_news() -> dict:
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts < CACHE_TTL and _cache:
        return _cache
    posts = _fetch_raw()
    _cache = _parse_posts(posts)
    _cache_ts = now
    return _cache


def get_coin_news(symbol: str) -> dict:
    symbol = symbol.upper()
    all_news = fetch_all_news()
    if symbol in all_news:
        return all_news[symbol]
    # Try direct fetch for this specific symbol
    posts = _fetch_raw(symbol)
    if posts:
        parsed = _parse_posts(posts)
        return parsed.get(symbol, _empty())
    return _empty()


def _empty() -> dict:
    return {
        "count": 0, "positive": 0, "negative": 0,
        "headlines": [], "urls": [], "sentiment": "neutral",
    }


def format_news_text(symbol: str) -> str:
    data = get_coin_news(symbol)
    if not data["count"]:
        return f"No recent news found for {symbol.upper()}."

    sentiment_label = {
        "bullish": "Bullish",
        "bearish": "Bearish",
        "neutral": "Neutral",
    }.get(data["sentiment"], "Neutral")

    lines = [
        f"News: {symbol.upper()}",
        f"Articles: {data['count']} | Sentiment: {sentiment_label}",
        "",
    ]
    for i, (headline, url) in enumerate(zip(data["headlines"], data["urls"]), 1):
        lines.append(f"{i}. {headline}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)
