import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"


def load_users() -> dict:
    if not USERS_FILE.exists():
        return {}
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_users(users: dict) -> None:
    USERS_FILE.write_text(
        json.dumps(users, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def ensure_user(chat_id: int) -> dict:
    users = load_users()
    key = str(chat_id)

    if key not in users:
        users[key] = {
            "alerts_enabled": True,
            "gems_enabled": True,
            "min_score": 0.55,
            "scan_limit": 5,
            "favorite_bucket": "any",
        }
        save_users(users)

    return users[key]


def update_user(chat_id: int, **kwargs) -> dict:
    users = load_users()
    key = str(chat_id)

    if key not in users:
        users[key] = {
            "alerts_enabled": True,
            "gems_enabled": True,
            "min_score": 0.55,
            "scan_limit": 5,
            "favorite_bucket": "any",
        }

    users[key].update(kwargs)
    save_users(users)
    return users[key]


def all_users() -> dict:
    return load_users()