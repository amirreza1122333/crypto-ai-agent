from io import StringIO
import pandas as pd


def dataframe_to_csv_text(df: pd.DataFrame) -> str:
    buffer = StringIO()
    df.to_csv(buffer, index=False, encoding="utf-8-sig")
    return buffer.getvalue()