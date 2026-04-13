import time
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CACHE_TTL = 1800  # 30 minutes
HEADERS   = {"User-Agent": "CryptoAIBot/1.0"}

_cache:    dict  = {}
_cache_ts: dict  = {}

POSITIVE_WORDS = {"moon", "buy", "bullish", "pump", "gem", "gain", "up",
                  "breakout", "launch", "new", "ath", "rally", "long", "hold"}
NEGATIVE_WORDS = {"dump", "sell", "bearish", "rug", "scam", "dead", "down",
                  "crash", "avoid", "ponzi", "fraud", "exit", "short"}

SUBREDDITS = ["CryptoCurrency", "CryptoMoonShots", "SatoshiStreetBets", "altcoin"]


def _search_reddit(symbol: str, subreddit: str) -> list:
    try:
        r = requests.get(
            f"https://www.reddit.com/r/{subreddit}/search.json",
            headers=HEADERS,
            params={"q": symbol, "sort": "new", "limit": 10, "t": "day", "restrict_sr": "true"},
            timeout=8,
            verify=False,
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("children", [])
    except Exception:
        pass
    return []


def get_social_data(symbol: str) -> dict:
    symbol = symbol.upper()
    now    = time.time()

    if symbol in _cache_ts and now - _cache_ts[symbol] < CACHE_TTL:
        return _cache[symbol]

    total_mentions = 0
    pos_count      = 0
    neg_count      = 0
    top_posts      = []

    for sub in SUBREDDITS[:2]:
        posts = _search_reddit(symbol, sub)
        total_mentions += len(posts)
        for post in posts:
            data  = post.get("data", {})
            title = (data.get("title", "") + " " + data.get("selftext", "")).lower()
            pos_count += sum(1 for w in POSITIVE_WORDS if w in title)
            neg_count += sum(1 for w in NEGATIVE_WORDS if w in title)
            if len(top_posts) < 3 and data.get("title"):
                top_posts.append({
                    "title": data["title"],
                    "score": data.get("score", 0),
                    "url":   f"https://reddit.com{data.get('permalink', '')}",
                })
        time.sleep(0.5)

    sentiment = "neutral"
    if total_mentions > 0:
        if pos_count > neg_count * 1.5:
            sentiment = "bullish"
        elif neg_count > pos_count * 1.5:
            sentiment = "bearish"

    result = {
        "mentions":         total_mentions,
        "sentiment":        sentiment,
        "positive_signals": pos_count,
        "negative_signals": neg_count,
        "top_posts":        top_posts,
    }
    _cache[symbol]    = result
    _cache_ts[symbol] = now
    return result


def format_social_text(symbol: str) -> str:
    data = get_social_data(symbol)
    if not data["mentions"]:
        return f"No Reddit mentions found for {symbol.upper()} in the last 24h."

    sentiment_label = {"bullish": "Bullish", "bearish": "Bearish", "neutral": "Neutral"}.get(
        data["sentiment"], "Neutral"
    )
    lines = [
        f"Reddit: {symbol.upper()}",
        f"Mentions: {data['mentions']} | Sentiment: {sentiment_label}",
        f"Positive signals: {data['positive_signals']} | Negative: {data['negative_signals']}",
        "",
    ]
    for i, post in enumerate(data["top_posts"], 1):
        lines.append(f"{i}. {post['title']} (score: {post['score']})")
    return "\n".join(lines)
