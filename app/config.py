import os
from dotenv import load_dotenv

load_dotenv()

COINGECKO_BASE_URL = os.getenv("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")
COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
VS_CURRENCY = os.getenv("VS_CURRENCY", "usd")
PER_PAGE = int(os.getenv("PER_PAGE", 250))
PAGES = int(os.getenv("PAGES", 2))
MIN_MARKET_CAP = float(os.getenv("MIN_MARKET_CAP", 10000000))
MIN_VOLUME = float(os.getenv("MIN_VOLUME", 1000000))

REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", 20))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", 2.5))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 4))
BACKOFF_FACTOR = float(os.getenv("BACKOFF_FACTOR", 2))

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")