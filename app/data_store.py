import sqlite3
from pathlib import Path
import pandas as pd
from datetime import datetime

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "market_history.db"


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT,
        symbol TEXT,
        name TEXT,
        current_price REAL,
        market_cap REAL,
        market_cap_rank INTEGER,
        total_volume REAL,
        price_change_24h REAL,
        price_change_7d REAL,
        volume_to_mcap REAL,
        final_score REAL,
        risk_level TEXT,
        bucket TEXT
    )
    """)

    conn.commit()
    conn.close()


def save_snapshot(df: pd.DataFrame):
    if df is None or df.empty:
        return

    conn = sqlite3.connect(DB_PATH)

    ts = datetime.utcnow().isoformat()

    df = df.copy()
    df["ts"] = ts
    df["price_change_24h"] = df["price_change_percentage_24h"]
    df["price_change_7d"] = df["price_change_percentage_7d_in_currency"]

    cols = [
        "ts",
        "symbol",
        "name",
        "current_price",
        "market_cap",
        "market_cap_rank",
        "total_volume",
        "price_change_24h",
        "price_change_7d",
        "volume_to_mcap",
        "final_score",
        "risk_level",
        "bucket",
    ]

    existing_cols = [c for c in cols if c in df.columns]
    df = df[existing_cols]

    df.to_sql("market_snapshots", conn, if_exists="append", index=False)
    conn.close()


def load_all_data():
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql("SELECT * FROM market_snapshots", conn)
    conn.close()
    return df


def load_symbol_data(symbol: str):
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql(
        "SELECT * FROM market_snapshots WHERE symbol=? ORDER BY ts ASC",
        conn,
        params=(symbol.upper(),)
    )
    conn.close()
    return df


def load_latest_snapshots_per_symbol(limit_per_symbol: int = 3) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    query = f"""
    WITH ranked AS (
        SELECT
            *,
            ROW_NUMBER() OVER (
                PARTITION BY UPPER(symbol)
                ORDER BY ts DESC, id DESC
            ) AS rn
        FROM market_snapshots
    )
    SELECT *
    FROM ranked
    WHERE rn <= {int(limit_per_symbol)}
    ORDER BY UPPER(symbol) ASC, ts DESC, id DESC
    """
    df = pd.read_sql(query, conn)
    conn.close()
    return df