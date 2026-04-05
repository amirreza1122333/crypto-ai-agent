import pandas as pd
from app.config import MIN_MARKET_CAP, MIN_VOLUME


STABLECOINS = {
    "usdt", "usdc", "dai", "fdusd", "tusd", "usde", "usdd", "pyusd", "gusd", "usdp",
    "usd1", "susds", "usdx", "busd", "frax", "lusd", "crvusd", "rlusd"
}


def clean_and_filter_data(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    required_cols = [
        "id",
        "symbol",
        "name",
        "current_price",
        "market_cap",
        "market_cap_rank",
        "total_volume",
        "price_change_percentage_24h",
        "price_change_percentage_7d_in_currency",
    ]
    df = df.dropna(subset=required_cols)

    df = df[df["current_price"] > 0]
    df = df[df["market_cap"] >= MIN_MARKET_CAP]
    df = df[df["total_volume"] >= MIN_VOLUME]
    df = df[df["market_cap_rank"] > 0]

    # حذف استیبل‌کوین‌ها با symbol و id
    df = df[~df["symbol"].str.lower().isin(STABLECOINS)]
    df = df[~df["id"].str.lower().isin(STABLECOINS)]

    # حذف استیبل‌کوین‌ها با name
    stable_name_keywords = ["usd", "stable", "dollar"]
    df = df[
        ~df["name"].str.lower().str.contains("|".join(stable_name_keywords), na=False)
    ]

    df["volume_to_mcap"] = df["total_volume"] / df["market_cap"]

    df = df.replace([float("inf"), float("-inf")], pd.NA)
    df = df.dropna(subset=["volume_to_mcap"])

    df = df.sort_values(by="market_cap_rank", ascending=True).reset_index(drop=True)

    return df