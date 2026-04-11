import pandas as pd

from app.api import get_cached_pipeline
from app.reporter import enrich_level3
from app.model_utils import load_model, load_feature_names
from app.data_store import load_latest_snapshots_per_symbol


PROB_THRESHOLD = 0.70


def _safe_numeric(series_or_value, default=0.0):
    return pd.to_numeric(series_or_value, errors="coerce").fillna(default)


def build_live_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()

    out["price_change_24h"] = pd.to_numeric(
        out.get("price_change_percentage_24h", 0), errors="coerce"
    ).fillna(0)

    out["price_change_7d"] = pd.to_numeric(
        out.get("price_change_percentage_7d_in_currency", 0), errors="coerce"
    ).fillna(0)

    out["current_price"] = pd.to_numeric(out.get("current_price", 0), errors="coerce").fillna(0)
    out["market_cap"] = pd.to_numeric(out.get("market_cap", 0), errors="coerce").fillna(0)
    out["market_cap_rank"] = pd.to_numeric(out.get("market_cap_rank", 9999), errors="coerce").fillna(9999)
    out["total_volume"] = pd.to_numeric(out.get("total_volume", 0), errors="coerce").fillna(0)
    out["volume_to_mcap"] = pd.to_numeric(out.get("volume_to_mcap", 0), errors="coerce").fillna(0)
    out["final_score"] = pd.to_numeric(out.get("final_score", 0), errors="coerce").fillna(0)

    out["rank_strength"] = 1.0 / out["market_cap_rank"].clip(lower=1)
    out["score_x_liquidity"] = out["final_score"] * out["volume_to_mcap"]
    out["score_x_rank"] = out["final_score"] * out["rank_strength"]

    # default lag values
    out["price_lag_1"] = 0.0
    out["price_lag_2"] = 0.0
    out["score_lag_1"] = 0.0
    out["score_lag_2"] = 0.0
    out["volume_to_mcap_lag_1"] = 0.0
    out["rank_lag_1"] = 0.0
    out["price_return_1"] = 0.0
    out["price_return_2"] = 0.0
    out["score_delta_1"] = 0.0
    out["volume_to_mcap_delta_1"] = 0.0
    out["rank_change_1"] = 0.0

    # load historical lag data from DB
    hist = load_latest_snapshots_per_symbol(limit_per_symbol=3)
    if hist is not None and not hist.empty:
        hist["symbol"] = hist["symbol"].astype(str).str.upper().str.strip()
        hist["current_price"] = pd.to_numeric(hist.get("current_price", 0), errors="coerce").fillna(0)
        hist["final_score"] = pd.to_numeric(hist.get("final_score", 0), errors="coerce").fillna(0)
        hist["volume_to_mcap"] = pd.to_numeric(hist.get("volume_to_mcap", 0), errors="coerce").fillna(0)
        hist["market_cap_rank"] = pd.to_numeric(hist.get("market_cap_rank", 9999), errors="coerce").fillna(9999)

        lag_rows = []
        for symbol, g in hist.groupby("symbol", sort=False):
            g = g.sort_values(["ts", "id"], ascending=[False, False]).reset_index(drop=True)

            row = {"symbol": symbol}

            if len(g) >= 1:
                row["price_lag_1"] = float(g.loc[0, "current_price"])
                row["score_lag_1"] = float(g.loc[0, "final_score"])
                row["volume_to_mcap_lag_1"] = float(g.loc[0, "volume_to_mcap"])
                row["rank_lag_1"] = float(g.loc[0, "market_cap_rank"])
            else:
                row["price_lag_1"] = 0.0
                row["score_lag_1"] = 0.0
                row["volume_to_mcap_lag_1"] = 0.0
                row["rank_lag_1"] = 0.0

            if len(g) >= 2:
                row["price_lag_2"] = float(g.loc[1, "current_price"])
                row["score_lag_2"] = float(g.loc[1, "final_score"])
            else:
                row["price_lag_2"] = 0.0
                row["score_lag_2"] = 0.0

            lag_rows.append(row)

        lag_df = pd.DataFrame(lag_rows)
        out = out.merge(lag_df, on="symbol", how="left", suffixes=("", "_hist"))

        for col in [
            "price_lag_1",
            "price_lag_2",
            "score_lag_1",
            "score_lag_2",
            "volume_to_mcap_lag_1",
            "rank_lag_1",
        ]:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

        out["price_return_1"] = (
            (out["current_price"] - out["price_lag_1"]) / out["price_lag_1"].replace(0, pd.NA)
        ).fillna(0.0)

        out["price_return_2"] = (
            (out["current_price"] - out["price_lag_2"]) / out["price_lag_2"].replace(0, pd.NA)
        ).fillna(0.0)

        out["score_delta_1"] = (out["final_score"] - out["score_lag_1"]).fillna(0.0)
        out["volume_to_mcap_delta_1"] = (out["volume_to_mcap"] - out["volume_to_mcap_lag_1"]).fillna(0.0)
        out["rank_change_1"] = (out["rank_lag_1"] - out["market_cap_rank"]).fillna(0.0)

    return out


def predict_live_top(n: int = 10):
    model = load_model()
    feature_names = load_feature_names()

    df = get_cached_pipeline()
    if df is None or df.empty:
        print("[ERROR] No live data available.")
        return

    df = enrich_level3(df)
    df = build_live_features(df)

    for col in feature_names:
        if col not in df.columns:
            df[col] = 0.0

    X = df[feature_names].copy().fillna(0)

    probs = model.predict_proba(X)[:, 1]
    preds = (probs >= PROB_THRESHOLD).astype(int)

    df["pump_probability_6h"] = probs
    df["predicted_up_6h"] = preds

    df["prediction_label"] = df["pump_probability_6h"].apply(
        lambda p: "Likely Up" if p >= PROB_THRESHOLD else "Not Strong Enough"
    )

    df = df.sort_values(
        ["pump_probability_6h", "final_score", "confidence"],
        ascending=[False, False, False]
    ).head(n)

    show_cols = [
        "name",
        "symbol",
        "final_score",
        "trend",
        "risk_level",
        "signal_type",
        "confidence",
        "pump_probability_6h",
        "prediction_label",
    ]

    print(df[show_cols].to_string(index=False))


if __name__ == "__main__":
    predict_live_top()