from pathlib import Path
from datetime import datetime
import pandas as pd


def save_snapshot(df: pd.DataFrame, prefix: str = "scored_coins") -> str:
    output_dir = Path("data/raw")
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_path = output_dir / f"{prefix}_{timestamp}.csv"

    df.to_csv(file_path, index=False, encoding="utf-8-sig")
    return str(file_path)