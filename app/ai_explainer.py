import pandas as pd

from app.claude_ai import analyze_coin


def _describe_24h(change_24h: float) -> str:
    if change_24h >= 15:
        return "Very strong 24h price expansion"
    if change_24h >= 5:
        return "Positive 24h continuation"
    if change_24h >= 0:
        return "Mild positive 24h movement"
    if change_24h > -5:
        return "Minor 24h pullback"
    return "Heavy 24h weakness"


def _describe_7d(change_7d: float) -> str:
    if change_7d >= 40:
        return "Explosive 7d momentum"
    if change_7d >= 20:
        return "Strong 7d momentum"
    if change_7d >= 10:
        return "Healthy 7d uptrend"
    if change_7d >= 0:
        return "Moderately positive 7d trend"
    return "Negative 7d trend"


def _describe_liquidity(volume_to_mcap: float) -> str:
    if volume_to_mcap >= 1.0:
        return "Extremely high turnover relative to market cap"
    if volume_to_mcap >= 0.3:
        return "Elevated trading activity"
    if volume_to_mcap >= 0.08:
        return "Healthy liquidity profile"
    return "Relatively calm trading activity"


def _describe_rank(rank: float) -> str:
    if rank <= 20:
        return "Large-cap profile"
    if rank <= 100:
        return "Established mid-to-large cap profile"
    if rank <= 200:
        return "Mid-cap profile"
    return "Smaller-cap profile"


def _rule_based(row: pd.Series) -> str:
    parts = [
        _describe_7d(row["price_change_percentage_7d_in_currency"]),
        _describe_24h(row["price_change_percentage_24h"]),
        _describe_liquidity(row["volume_to_mcap"]),
        _describe_rank(row["market_cap_rank"]),
        f'Classified as {row["risk_level"]} risk / {row["bucket"]}',
    ]
    return " | ".join(parts)


def build_explanation(row: pd.Series) -> str:
    ai_text = analyze_coin(row.to_dict())
    if ai_text:
        return ai_text
    return _rule_based(row)


def add_explanations(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["explanation"] = df.apply(build_explanation, axis=1)
    return df