import pandas as pd


EXCLUDED_SAFER_KEYWORDS = [
    "trump", "meme", "pepe", "doge", "inu", "shib", "fart", "cat", "frog"
]


def add_risk_labels(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # default values
    df["risk_level"] = "Medium"
    df["bucket"] = "balanced"

    # --- Narrative / Meme detection ---
    narrative_mask = (
        df["name"].str.lower().str.contains("|".join(EXCLUDED_SAFER_KEYWORDS), na=False) |
        df["symbol"].str.lower().str.contains("|".join(EXCLUDED_SAFER_KEYWORDS), na=False)
    )

    # =========================
    # RISK LEVELS
    # =========================

    # --- Low Risk ---
    low_risk_mask = (
        (df["market_cap_rank"] <= 50) &
        (df["volume_to_mcap"] <= 0.30) &
        (df["price_change_percentage_24h"] > -3) &
        (df["price_change_percentage_7d_in_currency"] > -5) &
        (~narrative_mask)
    )
    df.loc[low_risk_mask, "risk_level"] = "Low"

    # --- High Risk ---
    high_risk_mask = (
        (df["market_cap_rank"] > 200) |
        (df["volume_to_mcap"] > 0.80) |
        (df["price_change_percentage_24h"] < -10) |
        (
            (df["price_change_percentage_7d_in_currency"] > 35) &
            (df["market_cap_rank"] > 120)
        ) |
        (narrative_mask)
    )
    df.loc[high_risk_mask, "risk_level"] = "High"

    # =========================
    # BUCKET CLASSIFICATION
    # =========================

    # --- Momentum ---
    momentum_mask = (
        (df["price_change_percentage_7d_in_currency"] > 15) &
        (df["price_change_percentage_24h"] > 0)
    )
    df.loc[momentum_mask, "bucket"] = "momentum"

    # --- Speculative ---
    speculative_mask = (
        (df["market_cap_rank"] > 150) |
        (df["volume_to_mcap"] > 0.60) |
        (narrative_mask)
    )
    df.loc[speculative_mask, "bucket"] = "speculative"

    # --- Safer (override آخر) ---
    safer_mask = (
        (df["market_cap_rank"] <= 80) &
        (df["volume_to_mcap"] <= 0.50) &
        (df["price_change_percentage_24h"] > -3) &
        (df["price_change_percentage_7d_in_currency"] > 0) &
        (~narrative_mask)
    )
    df.loc[safer_mask, "bucket"] = "safer"

    return df