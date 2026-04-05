from app.fetcher import fetch_market_data
from app.filters import clean_and_filter_data
from app.scorer import score_coins
from app.reporter import get_top_overall, get_top_momentum, get_top_safer
from app.saver import save_snapshot
from app.risk import add_risk_labels
from app.ai_explainer import add_explanations


def main():
    df_raw = fetch_market_data()

    if df_raw.empty:
        print("No data fetched from CoinGecko. Try again later or reduce request rate.")
        return

    print("\nTotal raw coins:", len(df_raw))

    df_filtered = clean_and_filter_data(df_raw)
    print("Total filtered coins:", len(df_filtered))

    if df_filtered.empty:
        print("No coins left after filtering.")
        return

    df_scored = score_coins(df_filtered)
    df_labeled = add_risk_labels(df_scored)
    df_final = add_explanations(df_labeled)

    saved_path = save_snapshot(df_final)
    print(f"\nSnapshot saved to: {saved_path}")

    print("\n--- TOP OVERALL ---")
    print(get_top_overall(df_final, 10).to_string(index=False))

    print("\n--- TOP MOMENTUM ---")
    print(get_top_momentum(df_final, 10).to_string(index=False))

    print("\n--- TOP SAFER LARGE CAPS ---")
    print(get_top_safer(df_final, 10).to_string(index=False))

    print("\n--- TOP 10 WITH EXPLANATIONS ---")
    cols = [
        "name",
        "symbol",
        "final_score",
        "risk_level",
        "bucket",
        "explanation",
    ]
    print(df_final[cols].head(10).to_string(index=False))


if __name__ == "__main__":
    main()