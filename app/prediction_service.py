import pandas as pd

from app.model_utils import load_model, load_feature_names
from app.data_store import load_latest_snapshots_per_symbol
from app.feedback_dataset import build_feedback_dataset


def get_dynamic_threshold():
    df = build_feedback_dataset()

    if df.empty or len(df) < 20:
        return 0.70

    wins = df[df["target_real"] == 1]
    if wins.empty:
        return 0.70

    avg_return = float(wins["return_pct"].mean() or 0)

    threshold = min(max(0.65 + avg_return, 0.60), 0.85)
    return threshold


def _ensure_numeric(df: pd.DataFrame):
    num_cols = [
        "current_price",
        "market_cap",
        "market_cap_rank",
        "total_volume",
        "volume_to_mcap",
        "final_score",
    ]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
    return df


def build_live_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["symbol"] = out["symbol"].astype(str).str.upper().str.strip()

    out["price_change_24h"] = pd.to_numeric(
        out.get("price_change_percentage_24h", 0), errors="coerce"
    ).fillna(0)

    out["price_change_7d"] = pd.to_numeric(
        out.get("price_change_percentage_7d_in_currency", 0), errors="coerce"
    ).fillna(0)

    out = _ensure_numeric(out)

    out["rank_strength"] = 1.0 / out["market_cap_rank"].clip(lower=1)
    out["score_x_liquidity"] = out["final_score"] * out["volume_to_mcap"]
    out["score_x_rank"] = out["final_score"] * out["rank_strength"]

    lag_cols = [
        "price_lag_1", "price_lag_2", "score_lag_1", "score_lag_2",
        "volume_to_mcap_lag_1", "rank_lag_1",
        "price_return_1", "price_return_2",
        "score_delta_1", "volume_to_mcap_delta_1", "rank_change_1"
    ]
    for c in lag_cols:
        out[c] = 0.0

    hist = load_latest_snapshots_per_symbol(3)

    if hist is not None and not hist.empty:
        hist["symbol"] = hist["symbol"].astype(str).str.upper().str.strip()
        hist = _ensure_numeric(hist)

        rows = []
        for sym, g in hist.groupby("symbol"):
            g = g.sort_values(["ts", "id"], ascending=[False, False]).reset_index(drop=True)

            r = {"symbol": sym}

            if len(g) >= 1:
                r["price_lag_1"] = g.loc[0, "current_price"]
                r["score_lag_1"] = g.loc[0, "final_score"]
                r["volume_to_mcap_lag_1"] = g.loc[0, "volume_to_mcap"]
                r["rank_lag_1"] = g.loc[0, "market_cap_rank"]
            else:
                r["price_lag_1"] = 0.0
                r["score_lag_1"] = 0.0
                r["volume_to_mcap_lag_1"] = 0.0
                r["rank_lag_1"] = 0.0

            if len(g) >= 2:
                r["price_lag_2"] = g.loc[1, "current_price"]
                r["score_lag_2"] = g.loc[1, "final_score"]
            else:
                r["price_lag_2"] = 0.0
                r["score_lag_2"] = 0.0

            rows.append(r)

        lag_df = pd.DataFrame(rows)
        out = out.merge(lag_df, on="symbol", how="left", suffixes=("", "_hist"))

        for c in [
            "price_lag_1",
            "price_lag_2",
            "score_lag_1",
            "score_lag_2",
            "volume_to_mcap_lag_1",
            "rank_lag_1",
        ]:
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

        out["price_return_1"] = (
            (out["current_price"] - out["price_lag_1"]) /
            out["price_lag_1"].replace(0, pd.NA)
        ).fillna(0.0)

        out["price_return_2"] = (
            (out["current_price"] - out["price_lag_2"]) /
            out["price_lag_2"].replace(0, pd.NA)
        ).fillna(0.0)

        out["score_delta_1"] = (out["final_score"] - out["score_lag_1"]).fillna(0.0)
        out["volume_to_mcap_delta_1"] = (out["volume_to_mcap"] - out["volume_to_mcap_lag_1"]).fillna(0.0)
        out["rank_change_1"] = (out["rank_lag_1"] - out["market_cap_rank"]).fillna(0.0)

    return out


def add_prediction(df: pd.DataFrame) -> pd.DataFrame:
    try:
        model = load_model()
        features = load_feature_names()
    except FileNotFoundError as e:
        print(f"[WARN] Model not found, skipping predictions: {e}")
        df = df.copy()
        df["pump_probability_6h"] = 0.0
        df["prediction_label"] = "No Model"
        return df

    threshold = get_dynamic_threshold()

    out = build_live_features(df.copy())

    for c in features:
        if c not in out.columns:
            print(f"[WARN] Missing feature '{c}', filling with 0.0")
            out[c] = 0.0

    X = out[features].fillna(0)
    probs = model.predict_proba(X)[:, 1]

    out["pump_probability_6h"] = probs
    out["prediction_label"] = out["pump_probability_6h"].apply(
        lambda p: "Likely Up" if p >= threshold else "Weak"
    )

    return out