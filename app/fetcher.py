import time
import requests
import urllib3
import pandas as pd

from app.config import (
    COINGECKO_BASE_URL,
    COINGECKO_API_KEY,
    VS_CURRENCY,
    PER_PAGE,
    PAGES,
    REQUEST_TIMEOUT,
    REQUEST_DELAY,
    MAX_RETRIES,
    BACKOFF_FACTOR,
)

# SSL verification is disabled due to system-level certificate interception
# (antivirus/VPN SSL inspection). Safe for local development only.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

COINPAPRIKA_BASE_URL = "https://api.coinpaprika.com/v1/tickers"


def _fetch_page_coingecko(session: requests.Session, page: int) -> list:
    url = f"{COINGECKO_BASE_URL}/coins/markets"
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": PER_PAGE,
        "page": page,
        "price_change_percentage": "24h,7d",
    }
    if COINGECKO_API_KEY:
        params["x_cg_demo_api_key"] = COINGECKO_API_KEY

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)

            if response.status_code == 200:
                print(f"[OK] CoinGecko page {page} fetched")
                return response.json()

            if response.status_code == 429:
                wait_time = REQUEST_DELAY * (BACKOFF_FACTOR ** attempt)
                print(f"[429] CoinGecko rate limit page {page}. Waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue

            print(f"[ERROR] CoinGecko page {page} -> {response.status_code}")
            print(response.text[:300].encode("ascii", errors="replace").decode())
            return []

        except requests.RequestException as exc:
            wait_time = REQUEST_DELAY * (BACKOFF_FACTOR ** attempt)
            print(f"[REQUEST ERROR] CoinGecko page {page}: {exc}")
            print(f"Retrying in {wait_time:.1f}s...")
            time.sleep(wait_time)

    print(f"[FAILED] CoinGecko page {page} after retries")
    return []


def _fetch_from_coingecko(session: requests.Session) -> pd.DataFrame:
    print("\n[INFO] TRYING COINGECKO...\n")
    all_data = []

    for page in range(1, PAGES + 1):
        print(f"[INFO] Fetching CoinGecko page {page}...")
        page_data = _fetch_page_coingecko(session, page)

        if not page_data:
            print(f"[WARN] No data for CoinGecko page {page}")
        else:
            all_data.extend(page_data)

        if page < PAGES:
            time.sleep(REQUEST_DELAY)

    if not all_data:
        return pd.DataFrame()

    df = pd.DataFrame(all_data)

    expected_cols = [
        "id", "symbol", "name", "current_price", "market_cap",
        "market_cap_rank", "total_volume", "price_change_percentage_24h",
        "price_change_percentage_7d_in_currency", "circulating_supply",
    ]
    existing_cols = [col for col in expected_cols if col in df.columns]
    return df[existing_cols]


def _fetch_from_coinpaprika(session: requests.Session) -> pd.DataFrame:
    print("\n[INFO] FALLBACK TO COINPAPRIKA...\n")
    try:
        response = session.get(
            COINPAPRIKA_BASE_URL,
            params={"limit": PER_PAGE * PAGES},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

        if not data:
            return pd.DataFrame()

        rows = []
        for item in data:
            usd = item.get("quotes", {}).get("USD", {})
            rows.append({
                "id": item.get("id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "current_price": usd.get("price"),
                "market_cap": usd.get("market_cap"),
                "market_cap_rank": item.get("rank"),
                "total_volume": usd.get("volume_24h"),
                "price_change_percentage_24h": usd.get("percent_change_24h"),
                "price_change_percentage_7d_in_currency": usd.get("percent_change_7d"),
                "circulating_supply": item.get("total_supply"),
            })

        df = pd.DataFrame(rows)
        print(f"[OK] CoinPaprika fetched {len(df)} rows")
        return df

    except requests.RequestException as exc:
        print(f"[FAILED] CoinPaprika error: {exc}")
        return pd.DataFrame()


def fetch_market_data() -> pd.DataFrame:
    print("\n[INFO] START FETCHING MARKET DATA...\n")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    session.verify = False

    df = _fetch_from_coingecko(session)
    if not df.empty:
        print("[OK] Using CoinGecko data\n")
        return df

    df = _fetch_from_coinpaprika(session)
    if not df.empty:
        print("[OK] Using CoinPaprika data\n")
        return df

    print("\n[ERROR] NO DATA RECEIVED FROM ANY API\n")
    return pd.DataFrame()
