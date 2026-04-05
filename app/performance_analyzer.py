import pandas as pd
from app.feedback_dataset import build_feedback_dataset


def analyze():
    df = build_feedback_dataset()
    if df.empty:
        return

    print("\n=== PERFORMANCE BY SYMBOL ===")
    print(
        df.groupby("symbol")["target_real"]
        .mean()
        .sort_values(ascending=False)
        .head(10)
    )

    print("\n=== OVERALL ===")
    print("Winrate:", df["target_real"].mean())
    print("Avg return:", df["return_pct"].mean())