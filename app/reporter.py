import pandas as pd

from app.signals import (
    is_excluded_asset,
    classify_risk_v2,
    classify_trend,
    compute_confidence,
    classify_action,
    classify_signal_type,
    is_alert_candidate,
    build_reason,
)


def compute_ai_signal(row):
    prob = row.get("pump_probability_6h", 0)
    score = row.get("final_score", 0)

    if prob >= 0.75 and score >= 0.6:
        return "AI Strong"
    if prob >= 0.65:
        return "AI Watch"
    return "-"



def compute_combined_score(row):
    score = row.get("final_score", 0)
    prob = row.get("pump_probability_6h", 0)

    return (0.6 * score) + (0.4 * prob)


def enrich_level3(df):
    if df.empty:
        return df

    out = df.copy()
    out = out[~out.apply(is_excluded_asset, axis=1)].copy()

    out["risk_level"] = out.apply(classify_risk_v2, axis=1)
    out["trend"] = out.apply(classify_trend, axis=1)
    out["confidence"] = out.apply(compute_confidence, axis=1)
    out["action"] = out.apply(classify_action, axis=1)
    out["signal_type"] = out.apply(classify_signal_type, axis=1)
    out["alert_candidate"] = out.apply(is_alert_candidate, axis=1)
    out["reason"] = out.apply(build_reason, axis=1)

    # 🔥 اضافه شده
    if "pump_probability_6h" in out.columns:
        out["ai_signal"] = out.apply(compute_ai_signal, axis=1)
        out["combined_score"] = out.apply(compute_combined_score, axis=1)
    else:
        out["ai_signal"] = "—"
        out["combined_score"] = out["final_score"]

    return out


def get_top_overall(df, n=10):
    out = enrich_level3(df)

    out = out.sort_values(
        by=["combined_score", "confidence"],
        ascending=[False, False]
    )

    return out.head(n)


def get_top_momentum(df, n=10):
    out = enrich_level3(df)

    out = out[
        (out["price_change_percentage_7d_in_currency"] > 10)
        & (out["price_change_percentage_24h"] > 0)
    ]

    out = out.sort_values(
        by=["combined_score", "confidence"],
        ascending=[False, False]
    )

    return out.head(n)


def get_top_safer(df, n=10):
    out = enrich_level3(df)

    out = out[out["risk_level"].isin(["Low", "Medium"])]

    out = out.sort_values(
        by=["combined_score", "confidence"],
        ascending=[False, False]
    )

    return out.head(n)


def get_alert_candidates(df, n=10):
    out = enrich_level3(df)

    # 🔥 AI هم وارد alert شده
    out = out[
        (out["alert_candidate"] == True)
        | (out.get("pump_probability_6h", 0) >= 0.7)
    ]

    out = out.sort_values(
        by=["pump_probability_6h", "confidence", "final_score"],
        ascending=[False, False, False]
    )

    return out.head(n)


def get_scan_mix(df, n=6):
    out = enrich_level3(df)

    strong_consider = out[out["signal_type"] == "Strong Consider"].head(2)
    breakout_watch = out[out["signal_type"] == "Breakout Watch"].head(2)
    momentum_watch = out[out["signal_type"] == "Momentum Watch"].head(2)

    mixed = pd.concat(
        [strong_consider, breakout_watch, momentum_watch],
        axis=0
    )

    mixed = mixed.drop_duplicates(subset=["symbol"])

    # 🔥 fallback با AI
    if mixed.empty:
        mixed = out.sort_values(
            by=["combined_score", "confidence"],
            ascending=[False, False]
        ).head(n)

    return mixed.head(n)