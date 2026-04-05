from pathlib import Path
import pandas as pd

from app.data_store import load_all_data

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_CSV = OUTPUT_DIR / "ml_dataset_6h_3pct.csv"


def build_future_labels(
    df: pd.DataFrame,
    horizon_hours: int = 6,
    up_threshold: float = 0.03,
) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    out["ts"] = pd.to_datetime(out["ts"], errors="coerce")
    out = out.dropna(subset=["ts", "symbol", "current_price"]).copy()

    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()
    out["current_price"] = pd.to_numeric(out["current_price"], errors="coerce")
    out["market_cap_rank"] = pd.to_numeric(out["market_cap_rank"], errors="coerce")
    out["market_cap"] = pd.to_numeric(out["market_cap"], errors="coerce")
    out["total_volume"] = pd.to_numeric(out["total_volume"], errors="coerce")
    out["price_change_24h"] = pd.to_numeric(out["price_change_24h"], errors="coerce")
    out["price_change_7d"] = pd.to_numeric(out["price_change_7d"], errors="coerce")
    out["volume_to_mcap"] = pd.to_numeric(out["volume_to_mcap"], errors="coerce")
    out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce")

    out = out.sort_values(["symbol", "ts"]).reset_index(drop=True)

    labeled_parts = []

    horizon_delta = pd.Timedelta(hours=horizon_hours)

    for symbol, g in out.groupby("symbol", sort=False):
        g = g.sort_values("ts").reset_index(drop=True)

        future_prices = []
        future_returns = []
        targets = []

        times = g["ts"].tolist()
        prices = g["current_price"].tolist()

        j = 0
        n = len(g)

        for i in range(n):
            current_time = times[i]
            target_time = current_time + horizon_delta

            while j < n and times[j] < target_time:
                j += 1

            if j >= n:
                future_prices.append(None)
                future_returns.append(None)
                targets.append(None)
                continue

            future_price = prices[j]
            current_price = prices[i]

            if current_price is None or pd.isna(current_price) or current_price == 0:
                future_prices.append(None)
                future_returns.append(None)
                targets.append(None)
                continue

            future_return = (future_price - current_price) / current_price

            future_prices.append(future_price)
            future_returns.append(future_return)
            targets.append(1 if future_return >= up_threshold else 0)

        g["future_price_6h"] = future_prices
        g["future_return_6h"] = future_returns
        g["target_up_6h_3pct"] = targets

        labeled_parts.append(g)

    if not labeled_parts:
        return pd.DataFrame()

    labeled = pd.concat(labeled_parts, ignore_index=True)
    labeled = labeled.dropna(subset=["future_price_6h", "future_return_6h", "target_up_6h_3pct"]).copy()

    labeled["target_up_6h_3pct"] = labeled["target_up_6h_3pct"].astype(int)

    return labeled


def add_ml_features(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()
    out = out.sort_values(["symbol", "ts"]).reset_index(drop=True)

    # پایه
    out["rank_strength"] = 1.0 / out["market_cap_rank"].clip(lower=1)

    # تعامل‌ها
    out["score_x_liquidity"] = out["final_score"] * out["volume_to_mcap"]
    out["score_x_rank"] = out["final_score"] * out["rank_strength"]

    # lag features
    out["price_lag_1"] = out.groupby("symbol")["current_price"].shift(1)
    out["price_lag_2"] = out.groupby("symbol")["current_price"].shift(2)

    out["score_lag_1"] = out.groupby("symbol")["final_score"].shift(1)
    out["score_lag_2"] = out.groupby("symbol")["final_score"].shift(2)

    out["volume_to_mcap_lag_1"] = out.groupby("symbol")["volume_to_mcap"].shift(1)
    out["rank_lag_1"] = out.groupby("symbol")["market_cap_rank"].shift(1)

    # تغییرات
    out["price_return_1"] = (out["current_price"] - out["price_lag_1"]) / out["price_lag_1"]
    out["price_return_2"] = (out["current_price"] - out["price_lag_2"]) / out["price_lag_2"]

    out["score_delta_1"] = out["final_score"] - out["score_lag_1"]
    out["volume_to_mcap_delta_1"] = out["volume_to_mcap"] - out["volume_to_mcap_lag_1"]
    out["rank_change_1"] = out["rank_lag_1"] - out["market_cap_rank"]

    # پر کردن nullها
    numeric_cols = out.select_dtypes(include=["number"]).columns
    out[numeric_cols] = out[numeric_cols].fillna(0)

    return out


def build_training_dataset(
    horizon_hours: int = 6,
    up_threshold: float = 0.03,
) -> pd.DataFrame:
    raw = load_all_data()
    if raw.empty:
        print("❌ No historical data found.")
        return pd.DataFrame()

    labeled = build_future_labels(
        raw,
        horizon_hours=horizon_hours,
        up_threshold=up_threshold,
    )
    if labeled.empty:
        print("❌ Not enough history yet to create future labels.")
        return pd.DataFrame()

    dataset = add_ml_features(labeled)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dataset.to_csv(OUTPUT_CSV, index=False)

    print(f"✅ ML dataset saved: {OUTPUT_CSV}")
    print(f"Rows: {len(dataset)}")
    print(f"Positive targets: {dataset['target_up_6h_3pct'].sum()}")
    print(f"Negative targets: {(dataset['target_up_6h_3pct'] == 0).sum()}")

    return dataset


if __name__ == "__main__":
    build_training_dataset(horizon_hours=1, up_threshold=0.01)