import pandas as pd
import numpy as np


def clip_series(series: pd.Series, low_q=0.05, high_q=0.95) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    low = s.quantile(low_q)
    high = s.quantile(high_q)

    if high <= low:
        return s

    return s.clip(lower=low, upper=high)


def min_max_normalize(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0)
    min_val = s.min()
    max_val = s.max()

    if max_val == min_val:
        return pd.Series([0.5] * len(s), index=s.index)

    return (s - min_val) / (max_val - min_val)


def score_coins(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # -----------------------------
    # Clean numeric columns
    # -----------------------------
    numeric_cols = [
        "price_change_percentage_24h",
        "price_change_percentage_7d_in_currency",
        "volume_to_mcap",
        "market_cap_rank",
    ]

    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["price_change_percentage_24h"] = df["price_change_percentage_24h"].fillna(0)
    df["price_change_percentage_7d_in_currency"] = df["price_change_percentage_7d_in_currency"].fillna(0)
    df["volume_to_mcap"] = df["volume_to_mcap"].fillna(0)
    df["market_cap_rank"] = df["market_cap_rank"].fillna(df["market_cap_rank"].max() if df["market_cap_rank"].notna().any() else 9999)

    # -----------------------------
    # Robust clipping to avoid outlier distortion
    # -----------------------------
    df["chg24_clipped"] = clip_series(df["price_change_percentage_24h"], 0.05, 0.95)
    df["chg7_clipped"] = clip_series(df["price_change_percentage_7d_in_currency"], 0.05, 0.95)
    df["volume_to_mcap_capped"] = df["volume_to_mcap"].clip(upper=1.5)
    df["volume_to_mcap_clipped"] = clip_series(df["volume_to_mcap_capped"], 0.05, 0.95)

    # -----------------------------
    # Normalize factors
    # -----------------------------
    df["norm_24h"] = min_max_normalize(df["chg24_clipped"])
    df["norm_7d"] = min_max_normalize(df["chg7_clipped"])
    df["norm_volume_to_mcap"] = min_max_normalize(df["volume_to_mcap_clipped"])

    # Lower rank is better, but reduce the weight of this effect
    rank_strength = df["market_cap_rank"].max() - df["market_cap_rank"]
    df["norm_rank_strength"] = min_max_normalize(rank_strength)

    # -----------------------------
    # Soft bonuses / penalties
    # -----------------------------
    df["trend_bonus"] = 0.0
    df.loc[
        (df["price_change_percentage_24h"] > 0) &
        (df["price_change_percentage_7d_in_currency"] > 0),
        "trend_bonus"
    ] = 0.04

    df.loc[
        (df["price_change_percentage_24h"] > 4) &
        (df["price_change_percentage_7d_in_currency"] > 10),
        "trend_bonus"
    ] = 0.07

    df["liquidity_bonus"] = 0.0
    df.loc[df["volume_to_mcap"] >= 0.02, "liquidity_bonus"] = 0.03
    df.loc[df["volume_to_mcap"] >= 0.05, "liquidity_bonus"] = 0.05

    # Softer penalty than before
    df["rank_penalty"] = 0.0
    df.loc[df["market_cap_rank"] > 300, "rank_penalty"] = 0.04
    df.loc[df["market_cap_rank"] > 500, "rank_penalty"] = 0.08

    # Penalty for clearly weak momentum
    df["weakness_penalty"] = 0.0
    df.loc[
        (df["price_change_percentage_24h"] < 0) &
        (df["price_change_percentage_7d_in_currency"] < 0),
        "weakness_penalty"
    ] = 0.06

    # -----------------------------
    # Final score
    # -----------------------------
    df["final_score"] = (
    0.27 * df["norm_24h"] +
    0.27 * df["norm_7d"] +
    0.20 * df["norm_volume_to_mcap"] +
    0.16 * df["norm_rank_strength"] +
    0.6 * df["trend_bonus"] +
    0.6 * df["liquidity_bonus"] -
    df["rank_penalty"] -
    df["weakness_penalty"]
)

    # Keep scores in a sensible range
    df["final_score"] = df["final_score"].clip(lower=0.01, upper=0.99)

    df = df.sort_values(by="final_score", ascending=False).reset_index(drop=True)
    return df