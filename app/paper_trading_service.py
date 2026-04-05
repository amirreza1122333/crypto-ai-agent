import requests
from app.paper_trader import get_open_positions

API_URL = "http://127.0.0.1:8000"


def fetch_scan_results():
    try:
        r = requests.get(f"{API_URL}/scan?limit=50", timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


def fetch_top_overall_results():
    try:
        r = requests.get(f"{API_URL}/top-overall?limit=50", timeout=30)
        r.raise_for_status()
        return r.json().get("results", [])
    except Exception:
        return []


def fetch_market_results():
    """
    ترکیب scan و top-overall برای پیدا کردن symbol
    """
    combined = []
    seen = set()

    for item in fetch_scan_results() + fetch_top_overall_results():
        symbol = str(item.get("symbol", "")).upper().strip()
        if not symbol:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        combined.append(item)

    return combined


def find_coin(symbol: str, results: list[dict]):
    symbol = symbol.upper().strip()
    for c in results:
        if str(c.get("symbol", "")).upper().strip() == symbol:
            return c
    return None


def build_position_snapshot(chat_id: int):
    positions = get_open_positions(chat_id)
    if not positions:
        return []

    results = fetch_market_results()
    out = []

    for p in positions:
        symbol = str(p["symbol"]).upper().strip()
        coin = find_coin(symbol, results)

        # اگر در market data نبود، باز هم از دیتای خود پوزیشن استفاده کن
        current_price = float(p["entry_price"])
        current_signal = p.get("entry_signal", "-")
        current_prob = float(p.get("entry_prob", 0) or 0)
        name = p.get("name", symbol)

        if coin:
            current_price = float(coin.get("current_price", p["entry_price"]) or p["entry_price"])
            current_signal = coin.get("signal_type", current_signal)
            current_prob = float(coin.get("pump_probability_6h", current_prob) or current_prob)
            name = coin.get("name", name)

        entry_price = float(p["entry_price"] or 0)
        qty = float(p["quantity"] or 0)

        pnl = (current_price - entry_price) * qty
        return_pct = ((current_price - entry_price) / entry_price) if entry_price else 0.0

        out.append({
            **p,
            "symbol": symbol,
            "name": name,
            "current_price": current_price,
            "current_signal": current_signal,
            "current_prob": current_prob,
            "pnl": pnl,
            "return_pct": return_pct,
        })

    return out