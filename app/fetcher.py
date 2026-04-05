import time
import requests
import pandas as pd
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from app.config import (
    COINGECKO_BASE_URL,
    VS_CURRENCY,
    PER_PAGE,
    PAGES,
    REQUEST_TIMEOUT,
    REQUEST_DELAY,
    MAX_RETRIES,
    BACKOFF_FACTOR,
)

DEFAULT_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

COINCAP_BASE_URL = "https://api.coincap.io/v2/assets"


def _fetch_page_coingecko(session: requests.Session, page: int) -> list:
    url = f"{COINGECKO_BASE_URL}/coins/markets"
    params = {
        "vs_currency": VS_CURRENCY,
        "order": "market_cap_desc",
        "per_page": PER_PAGE,
        "page": page,
        "price_change_percentage": "24h,7d",
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = session.get(
                url,
                params=params,
                timeout=REQUEST_TIMEOUT,
                verify=False,  # فقط برای تست لوکال
            )

            if response.status_code == 200:
                print(f"[OK] CoinGecko page {page} fetched")
                return response.json()

            if response.status_code == 429:
                wait_time = REQUEST_DELAY * (BACKOFF_FACTOR ** attempt)
                print(f"[429] CoinGecko rate limit page {page}. Waiting {wait_time:.1f}s...")
                time.sleep(wait_time)
                continue

            print(f"[ERROR] CoinGecko page {page} -> {response.status_code}")
            print(response.text[:500])
            return []

        except requests.RequestException as exc:
            wait_time = REQUEST_DELAY * (BACKOFF_FACTOR ** attempt)
            print(f"[REQUEST ERROR] CoinGecko page {page}: {exc}")
            print(f"Retrying in {wait_time:.1f}s...")
            time.sleep(wait_time)

    print(f"[FAILED] CoinGecko page {page} after retries")
    return []


def _fetch_from_coingecko(session: requests.Session) -> pd.DataFrame:
    print("\n🚀 TRYING COINGECKO...\n")
    all_data = []

    for page in range(1, PAGES + 1):
        print(f"📄 Fetching CoinGecko page {page}...")
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
        "id",
        "symbol",
        "name",
        "current_price",
        "market_cap",
        "market_cap_rank",
        "total_volume",
        "price_change_percentage_24h",
        "price_change_percentage_7d_in_currency",
        "circulating_supply",
    ]

    existing_cols = [col for col in expected_cols if col in df.columns]
    return df[existing_cols]


def _fetch_from_coincap(session: requests.Session) -> pd.DataFrame:
    print("\n🟡 FALLBACK TO COINCAP...\n")

    try:
        response = session.get(
            COINCAP_BASE_URL,
            timeout=REQUEST_TIMEOUT,
            verify=False,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data", [])

        if not data:
            return pd.DataFrame()

        rows = []
        for i, item in enumerate(data, start=1):
            rows.append({
                "id": item.get("id"),
                "symbol": item.get("symbol"),
                "name": item.get("name"),
                "current_price": float(item.get("priceUsd")) if item.get("priceUsd") else None,
                "market_cap": float(item.get("marketCapUsd")) if item.get("marketCapUsd") else None,
                "market_cap_rank": i,
                "total_volume": float(item.get("volumeUsd24Hr")) if item.get("volumeUsd24Hr") else None,
                "price_change_percentage_24h": float(item.get("changePercent24Hr")) if item.get("changePercent24Hr") else None,
                "price_change_percentage_7d_in_currency": None,
                "circulating_supply": float(item.get("supply")) if item.get("supply") else None,
            })

        df = pd.DataFrame(rows)
        print(f"[OK] CoinCap fetched {len(df)} rows")
        return df

    except requests.RequestException as exc:
        print(f"[FAILED] CoinCap error: {exc}")
        return pd.DataFrame()


def fetch_market_data() -> pd.DataFrame:
    print("\n🚀 START FETCHING MARKET DATA...\n")

    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)

    # اول CoinGecko
    df = _fetch_from_coingecko(session)
    if not df.empty:
        print("✅ Using CoinGecko data\n")
        return df

    # fallback
    df = _fetch_from_coincap(session)
    if not df.empty:
        print("✅ Using CoinCap data\n")
        return df

    print("\n❌ NO DATA RECEIVED FROM ANY API\n")
    return pd.DataFrame()