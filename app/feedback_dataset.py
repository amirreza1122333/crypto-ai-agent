import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "user_data.db"


def load_closed_alerts():
    conn = sqlite3.connect(DB_PATH)

    df = pd.read_sql("""
        SELECT *
        FROM alert_results
        WHERE status='closed'
    """, conn)

    conn.close()
    return df


def build_feedback_dataset():
    df = load_closed_alerts()
    if df.empty:
        print("[INFO] No closed alerts yet.")
        return pd.DataFrame()

    # target واقعی
    df["target_real"] = df["is_win"]

    print("Rows:", len(df))
    print("Winrate:", df["target_real"].mean())

    return df