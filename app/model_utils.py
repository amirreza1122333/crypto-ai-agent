from pathlib import Path
import joblib

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"

MODEL_PATH = MODELS_DIR / "pump_predictor.pkl"
FEATURES_PATH = MODELS_DIR / "pump_predictor_features.pkl"


def ensure_models_dir():
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def save_model(model, feature_names):
    ensure_models_dir()
    joblib.dump(model, MODEL_PATH)
    joblib.dump(feature_names, FEATURES_PATH)


def load_model():
    if not MODEL_PATH.exists():
        raise FileNotFoundError(f"Model not found: {MODEL_PATH}")
    return joblib.load(MODEL_PATH)


def load_feature_names():
    if not FEATURES_PATH.exists():
        raise FileNotFoundError(f"Features file not found: {FEATURES_PATH}")
    return joblib.load(FEATURES_PATH)