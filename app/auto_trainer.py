import time
from app.dataset_builder import build_training_dataset
from app.predictor import train_model


def loop():
    while True:
        try:
            print("🔄 Auto retraining...")

            df = build_training_dataset()
            if not df.empty:
                train_model()

        except Exception as e:
            print("Training error:", e)

        time.sleep(3600 * 6)  # هر 6 ساعت