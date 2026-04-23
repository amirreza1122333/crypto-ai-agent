"""
Live ML inference for pump.fun tokens — with graceful fallback.

This is the runtime wrapper around the XGBoost model produced by
app/pumpfun_trainer.py. It's designed so that the prelaunch_tracker can
call it on every detection without caring whether a trained model actually
exists yet:

  * If models/pumpfun_predictor.pkl exists → load once, use for inference.
  * If not → return None, caller uses the hand-tuned launch_score instead.

This is the seam that makes Phase 3 "arrive automatically" — the moment
the trainer produces a model (weeks from now, once outcome data
accumulates), every new token starts getting scored by it, with zero code
changes anywhere else.

Inference cost: microseconds per call. No network, no database read.
"""

from pathlib import Path
from typing import Optional
import joblib
import pandas as pd

from app.creator_reputation import get_creator_stats
from app.sniper_detector    import get_last_check

MODEL_PATH    = Path(__file__).resolve().parent.parent / "models" / "pumpfun_predictor.pkl"
FEATURES_PATH = Path(__file__).resolve().parent.parent / "models" / "pumpfun_predictor_features.pkl"

_model = None
_features = None
_loaded = False


def _load_model() -> bool:
    """Lazy one-time load. Returns True on success."""
    global _model, _features, _loaded
    if _loaded:
        return _model is not None
    _loaded = True
    try:
        if MODEL_PATH.exists() and FEATURES_PATH.exists():
            _model = joblib.load(MODEL_PATH)
            _features = joblib.load(FEATURES_PATH)
            print(f"[LIVE_SCORER] Loaded ML model ({len(_features)} features)")
            return True
    except Exception as e:
        print(f"[LIVE_SCORER] Failed to load model: {e}")
    print("[LIVE_SCORER] No trained model yet — falling back to heuristic score")
    return False


def model_is_available() -> bool:
    """Has a trained model been loaded successfully?"""
    if not _loaded:
        _load_model()
    return _model is not None


def _row_from_context(ctx: dict) -> dict:
    """Build the feature row from detection-time context + live lookups.

    `ctx` should include whatever the caller has cheaply available:
      name, creator, initial_mcap_usd, launch_score, launch_tier,
      has_twitter, has_telegram, has_website, (optionally) mint.
    """
    name = (ctx.get("name") or "").strip()
    creator = ctx.get("creator", "") or ""
    mint = ctx.get("mint", "")

    row = {
        "launch_score":     int(ctx.get("launch_score", 0) or 0),
        "launch_tier_ord":  {"COLD": 0, "WARM": 1, "HOT": 2}.get(
                                ctx.get("launch_tier", "COLD"), 0),
        "has_twitter":      int(bool(ctx.get("has_twitter", 0))),
        "has_telegram":     int(bool(ctx.get("has_telegram", 0))),
        "has_website":      int(bool(ctx.get("has_website", 0))),
        "social_count":     int(bool(ctx.get("has_twitter", 0)))
                            + int(bool(ctx.get("has_telegram", 0)))
                            + int(bool(ctx.get("has_website", 0))),
        "name_len":         len(name),
        "name_word_count":  len(name.strip().split()) if name else 0,
        "name_has_space":   1 if " " in name else 0,
        "initial_mcap_usd": float(ctx.get("initial_mcap_usd", 0) or 0),
    }

    # Creator reputation lookup (SQLite, ~1ms).
    cstats = get_creator_stats(creator) if creator else {}
    tier = cstats.get("tier", "UNKNOWN")
    launches = int(cstats.get("total_launches", 0) or 0)
    grads    = int(cstats.get("graduations",    0) or 0)
    row["creator_launches"]    = launches
    row["creator_grads"]       = grads
    row["creator_avg_peak"]    = float(cstats.get("avg_peak_mcap_usd", 0) or 0)
    row["creator_max_peak"]    = float(cstats.get("max_peak_mcap_usd", 0) or 0)
    row["creator_grad_rate"]   = (grads / launches) if launches > 0 else 0.0
    row["creator_is_winner"]   = 1 if tier == "WINNER"  else 0
    row["creator_is_rugger"]   = 1 if tier == "RUGGER"  else 0
    row["creator_is_unknown"]  = 1 if tier == "UNKNOWN" else 0

    # Sniper check (may not have run yet at detection time — default 0).
    scheck = get_last_check(mint) if mint else {}
    row["sniper_top1_pct"]  = float(scheck.get("top1_pct",  0) or 0)
    row["sniper_top5_pct"]  = float(scheck.get("top5_pct",  0) or 0)
    row["sniper_top10_pct"] = float(scheck.get("top10_pct", 0) or 0)
    row["sniped_flag"]      = 1 if scheck.get("sniped") else 0
    return row


def score(ctx: dict) -> Optional[float]:
    """Return P(token hits target) in [0, 1], or None if no model loaded.

    Callers should treat None as "use the existing heuristic score instead."
    """
    if not _load_model():
        return None
    try:
        row = _row_from_context(ctx)
        frame = pd.DataFrame([row])
        # Align columns to training order, zero-fill missing.
        for c in _features:
            if c not in frame.columns:
                frame[c] = 0
        frame = frame[_features].fillna(0).astype(float)
        prob = float(_model.predict_proba(frame)[0, 1])
        return prob
    except Exception as e:
        print(f"[LIVE_SCORER] Inference error: {e}")
        return None


def adjust_tier(heuristic_tier: str, ctx: dict,
                promote_threshold: float = 0.65,
                demote_threshold: float = 0.15) -> tuple:
    """Combine heuristic tier with ML probability.

    Returns (final_tier, ml_prob_or_None). Behavior:
      * No model loaded → return heuristic tier unchanged, prob=None.
      * ML prob ≥ promote_threshold → promote to HOT.
      * ML prob ≤ demote_threshold AND heuristic==HOT → demote to WARM
        (catches false positives on heuristic where ML strongly disagrees).
      * Otherwise → keep heuristic tier.
    """
    prob = score(ctx)
    if prob is None:
        return heuristic_tier, None
    if prob >= promote_threshold and heuristic_tier != "HOT":
        return "HOT", prob
    if prob <= demote_threshold and heuristic_tier == "HOT":
        return "WARM", prob
    return heuristic_tier, prob
