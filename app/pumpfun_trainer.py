"""
Walk-forward XGBoost trainer for pump.fun pre-launch tokens.

This is a SEPARATE pipeline from app/predictor.py (which trains on top-250
CoinGecko coins). That one is a slow-moving market-cap filter. This one is
the fast-moving meme-launch filter — totally different feature set,
different label, different horizon.

Training data sources (all local SQLite, all populated by the bot itself):
  prelaunch_tokens     — one row per detected mint, with static features
                         (launch_score, has_twitter, name, creator, ...)
  prelaunch_outcomes   — one row per (mint, horizon_min) with the label
  creator_reputation   — per-creator aggregate stats (feature)
  sniper_checks        — holder concentration at check time (feature)

Training philosophy:
  * Temporal split only — older tokens train, newer tokens test
  * Purge gap of `horizon_hours` so labels can't leak across the seam
  * Aborts loudly if the dataset is too small / single-day / has no variety
  * Saves model only when validation metrics beat a trivial baseline

The trained model is loaded by app/live_scorer.py to rank new tokens in
real time. Until enough data has accumulated, live_scorer falls back to
the existing launch_score heuristic.
"""

from pathlib import Path
import sqlite3
import pandas as pd
import numpy as np
import joblib

DB_PATH     = Path(__file__).resolve().parent.parent / "user_data.db"
MODEL_DIR   = Path(__file__).resolve().parent.parent / "models"
MODEL_PATH  = MODEL_DIR / "pumpfun_predictor.pkl"
FEATURES_PATH = MODEL_DIR / "pumpfun_predictor_features.pkl"

# Minimum data requirements before we even attempt training.
# Below these the walk-forward split is meaningless.
MIN_ROWS             = 300
MIN_DISTINCT_DAYS    = 7
MIN_POSITIVE_CLASS   = 20

# Default horizon for the outcome label (must exist in prelaunch_outcomes).
DEFAULT_HORIZON_MIN  = 360   # 6h
DEFAULT_TEST_FRAC    = 0.25
DEFAULT_PURGE_HOURS  = 6


# ──────────────────────────────────────────────────────────────────────────
# Feature assembly
# ──────────────────────────────────────────────────────────────────────────

def _load_joined(horizon_min: int) -> pd.DataFrame:
    """Join tokens + outcomes + creator_rep + sniper into one dataframe."""
    con = sqlite3.connect(DB_PATH, timeout=5)
    # LEFT JOINs on creator_reputation and sniper_checks so tokens without
    # enrichment still appear (features get filled with defaults below).
    df = pd.read_sql("""
        SELECT
            t.mint,
            t.name,
            t.creator,
            t.detected_ts,
            COALESCE(t.initial_mcap_usd, 0) AS initial_mcap_usd,
            COALESCE(t.launch_score, 0)    AS launch_score,
            COALESCE(t.launch_tier, 'COLD') AS launch_tier,
            COALESCE(t.has_twitter, 0)     AS has_twitter,
            COALESCE(t.has_telegram, 0)    AS has_telegram,
            COALESCE(t.has_website, 0)     AS has_website,

            o.mcap_at_snapshot,
            o.peak_mcap_so_far,
            o.graduated_by_then,
            o.return_pct,

            COALESCE(c.total_launches, 0)   AS creator_launches,
            COALESCE(c.graduations, 0)      AS creator_grads,
            COALESCE(c.avg_peak_mcap_usd,0) AS creator_avg_peak,
            COALESCE(c.max_peak_mcap_usd,0) AS creator_max_peak,
            COALESCE(c.tier, 'UNKNOWN')     AS creator_tier,

            COALESCE(s.top1_pct, 0)         AS sniper_top1_pct,
            COALESCE(s.top5_pct, 0)         AS sniper_top5_pct,
            COALESCE(s.top10_pct, 0)        AS sniper_top10_pct,
            COALESCE(s.sniped, 0)           AS sniped_flag
        FROM prelaunch_tokens t
        INNER JOIN prelaunch_outcomes o
                ON o.mint = t.mint AND o.horizon_min = ?
        LEFT  JOIN creator_reputation c ON c.creator = t.creator
        LEFT  JOIN sniper_checks      s ON s.mint    = t.mint
    """, con, params=(horizon_min,))
    con.close()
    return df


def _add_derived_features(df: pd.DataFrame) -> pd.DataFrame:
    """Cheap derived features — no external calls, no ML."""
    out = df.copy()
    if out.empty:
        return out

    # Name characteristics.
    out["name_len"]        = out["name"].fillna("").str.len()
    out["name_word_count"] = out["name"].fillna("").str.strip().str.split().str.len()
    out["name_has_space"]  = out["name"].fillna("").str.contains(r"\s").astype(int)

    # Social density — how many socials are present.
    out["social_count"] = (
        out["has_twitter"] + out["has_telegram"] + out["has_website"]
    ).astype(int)

    # Creator tier one-hot.
    out["creator_is_winner"]  = (out["creator_tier"] == "WINNER").astype(int)
    out["creator_is_rugger"]  = (out["creator_tier"] == "RUGGER").astype(int)
    out["creator_is_unknown"] = (out["creator_tier"] == "UNKNOWN").astype(int)
    out["creator_grad_rate"]  = np.where(
        out["creator_launches"] > 0,
        out["creator_grads"] / out["creator_launches"].clip(lower=1),
        0.0,
    )

    # Launch tier ordinal.
    tier_map = {"COLD": 0, "WARM": 1, "HOT": 2}
    out["launch_tier_ord"] = out["launch_tier"].map(tier_map).fillna(0).astype(int)

    return out


FEATURE_COLUMNS = [
    "launch_score",
    "launch_tier_ord",
    "has_twitter",
    "has_telegram",
    "has_website",
    "social_count",
    "name_len",
    "name_word_count",
    "name_has_space",
    "initial_mcap_usd",
    "creator_launches",
    "creator_grads",
    "creator_avg_peak",
    "creator_max_peak",
    "creator_grad_rate",
    "creator_is_winner",
    "creator_is_rugger",
    "creator_is_unknown",
    "sniper_top1_pct",
    "sniper_top5_pct",
    "sniper_top10_pct",
    "sniped_flag",
]


def build_pumpfun_training_dataset(horizon_min: int = DEFAULT_HORIZON_MIN,
                                    up_threshold_pct: float = 100.0
                                    ) -> pd.DataFrame:
    """Return a training-ready DataFrame with features + target column.

    Target = 1 if return_pct >= up_threshold_pct (default: 2x within horizon).
    Falls back to graduated_by_then when return_pct is missing.
    """
    raw = _load_joined(horizon_min)
    if raw.empty:
        return raw

    df = _add_derived_features(raw)

    # Label: primary = return_pct >= threshold. Fallback = graduated flag.
    df["target"] = np.where(
        df["return_pct"].notna(),
        (df["return_pct"] >= up_threshold_pct).astype(int),
        df["graduated_by_then"].fillna(0).astype(int),
    )

    # Convert detected_ts → pandas datetime for temporal splitting.
    df["ts"] = pd.to_datetime(df["detected_ts"], unit="s", errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    return df


# ──────────────────────────────────────────────────────────────────────────
# Training
# ──────────────────────────────────────────────────────────────────────────

def _temporal_split(df: pd.DataFrame,
                    test_frac: float = DEFAULT_TEST_FRAC,
                    purge_hours: int = DEFAULT_PURGE_HOURS):
    """Oldest (1 - test_frac) for train, remainder for test, with purge gap."""
    n = len(df)
    split_idx = max(1, int(n * (1 - test_frac)))
    train_end = df["ts"].iloc[split_idx - 1]
    purge_until = train_end + pd.Timedelta(hours=purge_hours)
    train_mask = df["ts"] <= train_end
    test_mask  = df["ts"] >= purge_until
    if int(test_mask.sum()) < 5:
        # Fall back to plain temporal split — still correct, just no purge.
        train_mask = pd.Series([i < split_idx for i in range(n)], dtype=bool)
        test_mask  = pd.Series([i >= split_idx for i in range(n)], dtype=bool)
    return train_mask, test_mask


def train_pumpfun_model(horizon_min: int = DEFAULT_HORIZON_MIN,
                        up_threshold_pct: float = 100.0) -> dict:
    """End-to-end training. Returns a report dict with metrics.

    Refuses to save a model unless validation metrics beat a trivial
    baseline (base rate) — no point shipping a model that predicts worse
    than 'always say no'.
    """
    df = build_pumpfun_training_dataset(horizon_min, up_threshold_pct)

    report = {"ok": False, "reason": "", "rows": len(df)}

    if len(df) < MIN_ROWS:
        report["reason"] = (
            f"Not enough rows: have {len(df)}, need >={MIN_ROWS}. "
            "Keep the bot running — outcomes accumulate automatically."
        )
        print(f"[TRAINER] ABORT: {report['reason']}")
        return report

    distinct_days = df["ts"].dt.date.nunique()
    if distinct_days < MIN_DISTINCT_DAYS:
        report["reason"] = (
            f"Only {distinct_days} distinct day(s) of data, need "
            f">={MIN_DISTINCT_DAYS}. Walk-forward split would not generalize."
        )
        print(f"[TRAINER] ABORT: {report['reason']}")
        return report

    if int(df["target"].sum()) < MIN_POSITIVE_CLASS:
        report["reason"] = (
            f"Only {int(df['target'].sum())} positive labels. Need "
            f">={MIN_POSITIVE_CLASS} to train responsibly."
        )
        print(f"[TRAINER] ABORT: {report['reason']}")
        return report

    try:
        import xgboost as xgb
    except ImportError:
        report["reason"] = "xgboost not installed. pip install xgboost"
        print(f"[TRAINER] ABORT: {report['reason']}")
        return report

    from sklearn.metrics import (
        accuracy_score, precision_score, recall_score,
        f1_score, roc_auc_score,
    )

    used_cols = [c for c in FEATURE_COLUMNS if c in df.columns]
    X = df[used_cols].fillna(0).astype(float)
    y = df["target"].astype(int)

    train_mask, test_mask = _temporal_split(df)
    X_train, X_test = X[train_mask], X[test_mask]
    y_train, y_test = y[train_mask], y[test_mask]

    print(f"[TRAINER] Train: {len(X_train)} rows  Test: {len(X_test)} rows")
    print(f"[TRAINER] Positive rate — train={y_train.mean():.3f} test={y_test.mean():.3f}")

    if y_train.nunique() < 2 or y_test.nunique() < 2:
        report["reason"] = "Train or test split has only one class — cannot evaluate."
        print(f"[TRAINER] ABORT: {report['reason']}")
        return report

    pos = int(y_train.sum())
    neg = int((y_train == 0).sum())
    scale_pos_weight = max(1.0, neg / max(pos, 1))

    model = xgb.XGBClassifier(
        n_estimators=400,
        max_depth=5,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        scale_pos_weight=scale_pos_weight,
        tree_method="hist",
        eval_metric="auc",
        random_state=42,
        n_jobs=-1,
    )

    model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)

    y_prob = model.predict_proba(X_test)[:, 1]
    y_pred = (y_prob >= 0.5).astype(int)

    metrics = {
        "accuracy":  float(accuracy_score(y_test, y_pred)),
        "precision": float(precision_score(y_test, y_pred, zero_division=0)),
        "recall":    float(recall_score(y_test, y_pred, zero_division=0)),
        "f1":        float(f1_score(y_test, y_pred, zero_division=0)),
        "roc_auc":   float(roc_auc_score(y_test, y_prob)),
    }
    base_rate = float(y_test.mean())

    print("[TRAINER] Metrics:", metrics)
    print(f"[TRAINER] Base rate (positive class in test): {base_rate:.3f}")

    # Sanity check: beat the base-rate classifier on precision.
    if metrics["precision"] < base_rate + 0.05:
        report["ok"] = False
        report["reason"] = (
            f"Model precision {metrics['precision']:.3f} barely beats base "
            f"rate {base_rate:.3f}. Not saving — would be noise in production."
        )
        report["metrics"] = metrics
        print(f"[TRAINER] SKIP SAVE: {report['reason']}")
        return report

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    joblib.dump(used_cols, FEATURES_PATH)

    importances = pd.DataFrame({
        "feature": used_cols,
        "importance": model.feature_importances_,
    }).sort_values("importance", ascending=False)
    print("[TRAINER] Top features:")
    print(importances.head(10).to_string(index=False))

    report["ok"]        = True
    report["metrics"]   = metrics
    report["base_rate"] = base_rate
    report["features"]  = used_cols
    report["top_features"] = importances.head(10).to_dict(orient="records")
    print(f"[TRAINER] Saved → {MODEL_PATH}")
    return report


if __name__ == "__main__":
    train_pumpfun_model()
