from pathlib import Path
import pandas as pd

from sklearn.model_selection import train_test_split
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import (
    accuracy_score,
    precision_score,
    recall_score,
    f1_score,
    classification_report,
)

from app.model_utils import save_model


DATASET_PATH = Path(__file__).resolve().parent.parent / "data" / "ml_dataset_6h_3pct.csv"


FEATURE_COLUMNS = [
    "current_price",
    "market_cap",
    "market_cap_rank",
    "total_volume",
    "price_change_24h",
    "price_change_7d",
    "volume_to_mcap",
    "final_score",
    "rank_strength",
    "score_x_liquidity",
    "score_x_rank",
    "price_lag_1",
    "price_lag_2",
    "score_lag_1",
    "score_lag_2",
    "volume_to_mcap_lag_1",
    "rank_lag_1",
    "price_return_1",
    "price_return_2",
    "score_delta_1",
    "volume_to_mcap_delta_1",
    "rank_change_1",
]

TARGET_COLUMN = "target_up_6h_3pct"


def load_dataset() -> pd.DataFrame:
    if not DATASET_PATH.exists():
        raise FileNotFoundError(f"Dataset not found: {DATASET_PATH}")

    df = pd.read_csv(DATASET_PATH)
    return df


def prepare_xy(df: pd.DataFrame):
    use_cols = [c for c in FEATURE_COLUMNS if c in df.columns]

    if TARGET_COLUMN not in df.columns:
        raise ValueError(f"Target column missing: {TARGET_COLUMN}")

    X = df[use_cols].copy()
    y = df[TARGET_COLUMN].copy()

    X = X.fillna(0)
    y = y.fillna(0).astype(int)

    return X, y, use_cols


def train_model():
    df = load_dataset()
    X, y, used_features = prepare_xy(df)

    print("Dataset rows:", len(df))
    print("Feature count:", len(used_features))
    print("Positive targets:", int(y.sum()))
    print("Negative targets:", int((y == 0).sum()))
    print()

    X_train, X_test, y_train, y_test = train_test_split(
        X,
        y,
        test_size=0.25,
        random_state=42,
        stratify=y if y.nunique() > 1 else None,
    )

    model = RandomForestClassifier(
        n_estimators=300,
        max_depth=10,
        min_samples_split=8,
        min_samples_leaf=4,
        random_state=42,
        n_jobs=-1,
        class_weight="balanced",
    )

    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)

    acc = accuracy_score(y_test, y_pred)
    prec = precision_score(y_test, y_pred, zero_division=0)
    rec = recall_score(y_test, y_pred, zero_division=0)
    f1 = f1_score(y_test, y_pred, zero_division=0)

    print("=== MODEL METRICS ===")
    print(f"Accuracy : {acc:.4f}")
    print(f"Precision: {prec:.4f}")
    print(f"Recall   : {rec:.4f}")
    print(f"F1 Score : {f1:.4f}")
    print()
    print("=== CLASSIFICATION REPORT ===")
    print(classification_report(y_test, y_pred, zero_division=0))

    importances = pd.DataFrame({
        "feature": used_features,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)

    print("=== TOP FEATURE IMPORTANCES ===")
    print(importances.head(15).to_string(index=False))

    save_model(model, used_features)
    print("\n✅ Model saved successfully.")


if __name__ == "__main__":
    train_model()