import time

from app.fetcher import fetch_market_data
from app.filters import clean_and_filter_data
from app.scorer import score_coins
from app.risk import add_risk_labels
from app.ai_explainer import add_explanations

from app.data_store import init_db, save_snapshot


def run_pipeline():
    df_raw = fetch_market_data()
    if df_raw is None or df_raw.empty:
        print("[ERROR] No raw data")
        return None

    df_filtered = clean_and_filter_data(df_raw)
    if df_filtered is None or df_filtered.empty:
        print("[ERROR] No filtered data")
        return None

    df_scored = score_coins(df_filtered)
    if df_scored is None or df_scored.empty:
        print("[ERROR] No scored data")
        return None

    df_labeled = add_risk_labels(df_scored)
    df_final = add_explanations(df_labeled)

    return df_final


def run_once():
    df = run_pipeline()
    if df is not None:
        save_snapshot(df)
        print(f"[OK] Snapshot saved: {len(df)} rows")
    else:
        print("[ERROR] Pipeline failed")


def loop(interval_seconds=900):
    while True:
        print("[INFO] Collecting market snapshot...")
        run_once()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    init_db()
    loop()