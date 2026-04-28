# Crypto AI Agent

> ## ⛔ STOP — AI ASSISTANT, READ THIS FIRST
>
> **If you are an LLM (Claude, GPT, Copilot, etc.) opening this repo with
> a fresh context window: you must read this file *before* doing anything
> else.** This project has accumulated several non-obvious rules; ignoring
> them re-introduces bugs that have already been fixed once.
>
> **Hard rules (do not violate without explicit user permission):**
>
> 1. **Never use `sklearn.train_test_split` (random shuffle) on
>    `data/ml_dataset_6h_3pct.csv` or any successor.** Labels look 6h
>    into the future — random splits leak future info into training.
>    Always use `app/predictor.py::temporal_split` (walk-forward + purge
>    gap). This is the bug the most recent uncommitted change fixed.
> 2. **`HORIZON_HOURS` and `up_threshold` in `predictor.py` must match
>    `dataset_builder.build_future_labels()`.** They are coupled.
> 3. **`app/claude_ai.py` calls Groq, not Claude** (commit `d8ade19`,
>    free-tier swap). Don't be fooled by the filename.
> 4. **Pump.fun integration must use the WebSocket feed**, not REST —
>    REST is Cloudflare-blocked. Use browser-style headers if you ever
>    do hit pump.fun HTTP.
> 5. **Use `datetime.now(timezone.utc)`**, not `datetime.utcnow()`
>    (deprecated, commit `98d919f`).
> 6. **Never commit `.env`.** It contains live Telegram, Helius, and
>    Groq tokens.
> 7. **TLS verification defaults to ON** (`verify=SSL_VERIFY` from
>    `app.config`). Never write `verify=False`. Users opt into insecure
>    mode via `INSECURE_SSL=true` in `.env` — only for dev networks
>    that intercept certs.
>
> **Then read these sections of this README, in order:**
> - "What it does (today)" — what the project actually is now
> - "Architecture (high level)" — the dataflow diagram
> - "Working notes (read this before changing things)" — the full gotcha list
> - "Known gaps / things worth doing next" — the real punch list
>
> A condensed version of these rules lives in `CLAUDE.md` at the repo
> root, which Claude Code auto-loads into every session. If you are
> Claude Code and that file did not load, alert the user — the repo's
> safety net is broken.

---

A Python toolkit that combines **mid/large-cap market analysis** (CoinGecko) with a
**Solana memecoin / pump.fun launch radar** and exposes everything through a
**Telegram bot** plus a small FastAPI service.

The project has grown well past its original scope. This README is the source
of truth for what it actually contains today — old descriptions that only
mentioned 3 files were stale.

---

## What it does (today)

Three loosely-coupled product surfaces sharing the same `app/` package:

1. **CoinGecko market scanner & ranker**
   Fetches top-N coins, filters, scores, classifies risk, saves snapshots,
   exposes results via FastAPI and Telegram.

2. **ML pump predictor (6h / +3%)**
   Trains a Random Forest on historical snapshots to predict whether a coin
   will move +3% within 6 hours. Walk-forward split with a purge gap (no
   lookahead bias).

3. **Solana pre-launch / pump.fun tracker**
   WebSocket-driven listener for new token creations, milestone alerts,
   "imminent launch" radar, graduation-zone tracking, rising-token velocity,
   creator reputation / rug detection (Helius), sniper detection.

A Telegram bot stitches these together into commands that any user can hit:
`/scan`, `/brain`, `/news`, `/whales`, `/gradzone`, `/imminent`, `/rising`,
`/preorder`, `/upcoming`, `/portfolio`, `/alert`, etc.

---

## Architecture (high level)

```
                       ┌────────────────────────────────────┐
                       │           app/telegram_bot.py      │
                       │   (long-poll loop, all commands)   │
                       └───────────────┬────────────────────┘
                                       │
       ┌───────────────────────────────┼────────────────────────────────────┐
       ▼                               ▼                                    ▼
  Market scoring               ML prediction                       Pump.fun radar
  ───────────────              ──────────────                      ──────────────
  fetcher.py                   dataset_builder.py                  prelaunch_tracker.py
  filters.py                   predictor.py                        launch_scout.py
  scorer.py                    predict_live.py                     dex_scanner.py
  risk.py                      auto_trainer.py                     helius_enricher.py
  reporter.py                  model_utils.py                      sniper_detector.py
  saver.py                     models/pump_predictor.pkl           creator_reputation.py
                                                                   pumpfun_trainer.py
       │                               │                                    │
       └───────────────────────────────┴────────────────────────────────────┘
                                       │
                                  Brain layer
                                  ───────────
                                  brain.py — fuses signals into one score:
                                    technical 30% / AI 20% / news 20%
                                    / social 15% / whales 10% / memory 5%
                                  Sources: technical_analysis, news_scanner,
                                  social_scanner, whale_tracker, memory_store,
                                  funding_rates, fear_greed
                                       │
                                       ▼
                                  AI layer
                                  ────────
                                  claude_ai.py  — chat / coin explainer / news
                                                  sentiment (currently routed to
                                                  Groq, despite the filename;
                                                  see commit d8ade19)
                                  ai_explainer.py
                                       │
                                       ▼
                                  Persistence
                                  ───────────
                                  data/market_history.db   (sqlite, snapshots)
                                  data/ml_dataset_6h_3pct.csv
                                  user_data.db             (watchlist, alerts,
                                                            portfolio, paper trades)
                                  models/pump_predictor.pkl
```

Two FastAPI surfaces:
- `app/api.py` — main HTTP API (`/scan`, predictions, etc.)
- `app/scout_bot.py` — secondary Telegram bot for the pre-launch scout

---

## Component map (by file)

### Entry points
| File | Purpose |
| --- | --- |
| `app/telegram_bot.py` | Main user-facing bot. Polls Telegram, dispatches all commands, runs background workers (gem alerts, price alerts, brain alerts, daily report). |
| `app/scout_bot.py` | Secondary Telegram bot focused on pre-launch alerts. |
| `app/api.py` | FastAPI HTTP layer. |
| `app/main.py` | One-shot CLI: fetch → filter → score → save snapshot → print top tables. |
| `app/predictor.py` | Train the RF model on the labeled CSV. Run as `python -m app.predictor`. |
| `app/predict_live.py` | Run the trained model against live data. |
| `app/dataset_builder.py` | Build the labeled CSV from `market_history.db`. |
| `app/auto_trainer.py` | Convenience wrapper for periodic retraining. |

### CoinGecko scoring pipeline
`fetcher.py` → `filters.py` → `scorer.py` → `risk.py` → `reporter.py` → `saver.py`
Inputs: CoinGecko REST. Output: scored DataFrame + sqlite snapshot.

### ML prediction
- `dataset_builder.py` — generates `target_up_6h_3pct` labels using a 6h forward-look horizon, then adds lag/delta/interaction features.
- `predictor.py` — trains a `RandomForestClassifier` with **walk-forward temporal split + purge gap** (no `train_test_split` shuffle — that was a lookahead-bias bug, fixed).
- `model_utils.py` — `save_model` / `load_model` (pickles model + feature list together).
- `models/pump_predictor.pkl` — current trained model.

### Pump.fun / Solana pre-launch
- `prelaunch_tracker.py` — biggest file (~80KB). WebSocket consumer for pump.fun events, holds the in-memory token registry, alert dispatch, multiple "phase" trackers (preorder / upcoming / imminent / rising / graduation zone / rising-velocity), DB tables, broadcast helpers.
- `launch_scout.py` — pre-launch scoring & filtering used by the scout bot.
- `dex_scanner.py` — scans new pools (DEX Screener / GeckoTerminal Tier 0).
- `helius_enricher.py` — Solana RPC enrichment via Helius (creator history, holder concentration → rug risk).
- `sniper_detector.py`, `creator_reputation.py`, `pumpfun_trainer.py` — supporting heuristics & ML.

### Brain / signal fusion
`brain.py` orchestrates: `technical_analysis`, `news_scanner`, `social_scanner`, `whale_tracker`, `memory_store`, `funding_rates`, `fear_greed`.
Weights: TA 30 / AI 20 / News 20 / Social 15 / Whales 10 / Memory 5.
5-min in-memory cache.

### AI layer
- `claude_ai.py` — despite the name, **currently calls Groq** (free tier, switched from Gemini in commit `d8ade19`). Used for chat, per-coin explainer, news sentiment.
- `ai_explainer.py` — adds a human-readable `explanation` column to the scored DataFrame.

### Trading / portfolio (paper)
`paper_trader.py`, `paper_trading_service.py`, `portfolio.py`, `performance_tracker.py`, `performance_analyzer.py`, `price_alerts.py`.

### State
- `data/market_history.db` — historical CoinGecko snapshots (sqlite).
- `data/ml_dataset_6h_3pct.csv` — labeled training set.
- `user_data.db` — Telegram users, watchlists, price alerts, paper trades, portfolio holdings.
- `data/users.json`, `data/raw/` — older artifacts.

---

## Running things

### Install
```bash
python -m venv .venv
.\.venv\Scripts\activate     # Windows
pip install -r requirements.txt
```

### Configure `.env`
Required keys (see `app/config.py` and `claude_ai.py` for the full list):
```
TELEGRAM_BOT_TOKEN=...
SCOUT_BOT_TOKEN=...
HELIUS_API_KEY=...
GROQ_API_KEY=...
COINGECKO_API_KEY=          # optional
API_BASE_URL=http://127.0.0.1:8000
INSECURE_SSL=false          # default. Set true ONLY on dev networks where
                            # certs cannot be validated (corporate proxy /
                            # antivirus SSL inspection). Disables MITM
                            # protection on every requests.* call.
```
`.env` is gitignored. **Never commit it.**

### Common commands
```bash
# One-shot scan + scored tables
python -m app.main

# FastAPI service
uvicorn app.api:app --reload

# Telegram bot (main)
python -m app.telegram_bot

# Pre-launch scout bot
python -m app.scout_bot

# Build labeled training CSV from market_history.db
python -m app.dataset_builder

# Train RF model
python -m app.predictor

# Score live data with the trained model
python -m app.predict_live
```

---

## Working notes (read this before changing things)

These are the non-obvious rules. Most are bugs we already fixed once.

### ML / predictor
- **Never use `train_test_split` (random shuffle) on this dataset.** Labels look 6h into the future; a random split leaks future info into training. Use `predictor.temporal_split` — walk-forward with a purge gap of `HORIZON_HOURS`.
- `HORIZON_HOURS` and `up_threshold` in `predictor.py` **must match** the values used in `dataset_builder.build_future_labels()`. They are coupled; change them together.
- `dataset_builder.__main__` calls `build_training_dataset(horizon_hours=1, up_threshold=0.01)` — that's a quick-test default and does **not** match `predictor.py`'s 6h/3% setup. If you regenerate the CSV from CLI, pass `horizon_hours=6, up_threshold=0.03` or the model trains on the wrong labels.
- A new dataset needs **multiple days of `ts` history** before training. `temporal_split` will abort with `[ABORT]` if there's <2 unique timestamps. That's intentional — don't "fix" it by going back to random splits.
- The model file pickles the model **and** the feature list together via `model_utils.save_model`. If you add/remove a feature in `predictor.FEATURE_COLUMNS`, retrain — don't hand-edit the pickle.

### Telegram bot
- `telegram_bot.py` runs **multiple polling loops in threads** (gem alerts, price alerts, brain alerts, prelaunch listener, daily report). Be careful adding state — prefer sqlite or `memory_store` over module-level globals.
- The bot uses **long polling** (`getUpdates` with `offset`), not webhooks. Don't accidentally introduce blocking calls in the main loop — long-running work goes in background threads.
- `_alerted_gems` is an in-memory dedupe set. If you restart the bot, recently-alerted gems can re-fire. That's known and acceptable.
- `claude_ai.py` is named for Claude but **currently calls Groq**. If you rename or migrate it, update all callers (`telegram_bot.py` imports `chat as ai_chat`).

### Pump.fun tracker
- `prelaunch_tracker.py` previously used a pump.fun REST endpoint that started returning Cloudflare blocks; the graduation-zone scanner was rewritten to use the WebSocket trade feed (`subscribeTokenTrade`). Don't regress to REST without checking it works.
- Browser-style headers are required for any direct pump.fun HTTP calls (commit `22fdc13`).
- DEX chart links should point to **DEX Screener**, not GeckoTerminal (commit `eada77b`).

### General
- Persisted timestamps in CSVs / sqlite are stored as strings. Always `pd.to_datetime(..., errors="coerce")` when reading; never assume the dtype.
- Symbols are normalized to upper-case stripped strings in the dataset builder. Do the same anywhere you join on symbol.
- Use `datetime.now(timezone.utc)` — `datetime.utcnow()` is deprecated (commit `98d919f`).

---

## Known gaps / things worth doing next

Not promises, just the punch list as of the current branch:

- The uncommitted change in `app/predictor.py` (walk-forward split) has not been committed yet. Verify it trains end-to-end on real data before committing.
- `temporal_split` will crash if the row at `split_idx - 1` has `NaT` for `ts`. Unlikely in practice but ungated.
- `claude_ai.py` filename misleads — either rename to `ai_chat.py` / `groq_ai.py`, or actually wire Claude in alongside Groq.
- README's old "Future Improvements" list (backtester, web dashboard, cloud deploy) is still open.
- No automated tests anywhere in the repo.

---

## Project layout (current, not the old 3-file fiction)

```
crypto_ai_agent/
├── app/
│   ├── api.py                  FastAPI HTTP layer
│   ├── telegram_bot.py         Main bot (entry point)
│   ├── scout_bot.py            Pre-launch scout bot
│   ├── main.py                 One-shot CLI scan
│   │
│   ├── fetcher.py              CoinGecko fetch
│   ├── filters.py              Filter out junk coins
│   ├── scorer.py               Score / rank logic
│   ├── risk.py                 Risk classification
│   ├── reporter.py             Top-N selectors
│   ├── saver.py                Snapshot persistence
│   ├── data_store.py           Snapshot loader for ML
│   │
│   ├── dataset_builder.py      Build labeled training CSV
│   ├── predictor.py            Train RF model (walk-forward split)
│   ├── predict_live.py         Live inference
│   ├── auto_trainer.py         Periodic retrain wrapper
│   ├── model_utils.py          Save/load model + features
│   │
│   ├── brain.py                Multi-signal fusion
│   ├── technical_analysis.py
│   ├── news_scanner.py
│   ├── social_scanner.py
│   ├── whale_tracker.py
│   ├── memory_store.py
│   ├── funding_rates.py
│   ├── fear_greed.py
│   │
│   ├── claude_ai.py            AI chat (currently Groq, not Claude)
│   ├── ai_explainer.py
│   │
│   ├── prelaunch_tracker.py    Pump.fun WebSocket + alerts
│   ├── launch_scout.py
│   ├── dex_scanner.py
│   ├── helius_enricher.py
│   ├── sniper_detector.py
│   ├── creator_reputation.py
│   ├── pumpfun_trainer.py
│   │
│   ├── paper_trader.py
│   ├── paper_trading_service.py
│   ├── portfolio.py
│   ├── performance_tracker.py
│   ├── performance_analyzer.py
│   ├── price_alerts.py
│   ├── prediction_service.py
│   ├── live_scorer.py
│   ├── alert_worker.py
│   ├── collector.py
│   ├── feedback_dataset.py
│   ├── user_store.py
│   ├── signals.py
│   ├── export_utils.py
│   └── config.py
│
├── data/
│   ├── market_history.db       (snapshots, sqlite)
│   ├── ml_dataset_6h_3pct.csv  (labeled training set)
│   ├── users.json              (per-user prefs, written by user_store.py)
│   └── raw/                    (timestamped CSV snapshots, written by saver.py)
│
├── models/
│   ├── pump_predictor.pkl
│   └── pump_predictor_features.pkl
│
├── user_data.db                Per-user state (sqlite)
├── requirements.txt
├── .env                        (gitignored, has live secrets)
└── README.md
```
