# CLAUDE.md — auto-loaded context for this repo

You are working in **crypto_ai_agent** — a Python toolkit covering three
loosely-coupled surfaces sharing the `app/` package:

1. CoinGecko market scanner & ranker
2. ML pump predictor (6h / +3%) — Random Forest, walk-forward split
3. Solana / pump.fun pre-launch radar (WebSocket-driven)

All three are stitched together by **`app/telegram_bot.py`** (main entry
point, long-poll loop, multi-threaded background workers). A FastAPI
layer lives in `app/api.py`.

**Read [README.md](README.md) for the full file map, dataflow diagram,
and run instructions.** This file only captures the rules that have
caused bugs in the past.

---

## Hard rules — do not violate

These exist because each one was a real bug we already fixed once.

1. **No `sklearn.train_test_split` on the ML dataset.**
   Labels look 6h into the future; random shuffle = lookahead bias.
   Use `app/predictor.py::temporal_split` (walk-forward + purge gap).

2. **`HORIZON_HOURS` and `up_threshold` are coupled across two files.**
   `app/predictor.py` and `app/dataset_builder.py::build_future_labels()`
   must agree. Currently 6h / 0.03. The CLI default in
   `dataset_builder.__main__` is **wrong on purpose** (1h / 0.01 for
   quick testing) — pass explicit args when regenerating for training.

3. **`app/claude_ai.py` calls Groq, not Claude** (commit `d8ade19`).
   Don't trust the filename; `import` calls go to `groq>=0.9.0`.
   `GROQ_API_KEY` is the env var.

4. **Pump.fun integration uses the WebSocket trade feed**
   (`subscribeTokenTrade`). REST is Cloudflare-blocked — do not regress.
   If you must hit pump.fun HTTP for any reason, use browser-style
   headers (commit `22fdc13`).

5. **DEX chart links → DEX Screener, not GeckoTerminal** (commit `eada77b`).

6. **`datetime.now(timezone.utc)`, never `datetime.utcnow()`** (commit `98d919f`).

7. **`.env` is gitignored and contains live secrets** (Telegram bot
   token, Helius API key, Groq API key). Never commit it. Never echo
   its contents into chat. If asked to "set up env", scaffold without
   the live values.

8. **TLS verification defaults to ON.** Use `verify=SSL_VERIFY` (from
   `app.config`) on every `requests.*` call — never `verify=False`.
   Users opt into insecure mode via `INSECURE_SSL=true` in `.env`.

---

## Conventions

- Persisted timestamps in CSVs / sqlite are stored as strings. Always
  `pd.to_datetime(..., errors="coerce")` when reading.
- Symbols are normalized to upper-case stripped strings in
  `dataset_builder`. Match that anywhere you join on symbol.
- `telegram_bot.py` runs multiple polling loops in threads. Prefer
  sqlite or `memory_store` over module-level globals for shared state.
- Model files pickle the model **and** the feature list together via
  `model_utils.save_model`. If you change `predictor.FEATURE_COLUMNS`,
  retrain — don't hand-edit the pickle.

---

## Where to look first

| Need | File |
| --- | --- |
| User-visible bot behaviour | `app/telegram_bot.py` |
| Add/change an HTTP endpoint | `app/api.py` |
| ML model training | `app/predictor.py` + `app/dataset_builder.py` |
| Live ML inference | `app/predict_live.py` |
| Multi-signal score fusion | `app/brain.py` |
| Pump.fun WebSocket logic | `app/prelaunch_tracker.py` (~80KB, biggest file) |
| Secondary scout bot | `app/scout_bot.py` + `app/launch_scout.py` |
| Solana on-chain enrichment | `app/helius_enricher.py` |
| Per-user persistence | `user_data.db` (sqlite) |
| Historical snapshots | `data/market_history.db` (sqlite) |

---

## Status snapshot (update this when it drifts)

- Branch: `main`
- Uncommitted: `app/predictor.py` (walk-forward split — verify it trains
  end-to-end before committing), TLS-verify rollout across 9 files,
  prelaunch_tracker velocity + ingest-task fixes, predictor logging,
  CLAUDE.md/README updates, and `.claude/settings.local.json`.
- No automated tests exist. Verify changes by running the relevant entry
  point manually.
- `data/raw/` and `data/users.json` are NOT dead — `saver.py` writes
  `data/raw/`, `user_store.py` reads/writes `users.json`. Don't delete.

---

If you finish reading this, you are ready to work. If a rule above
seems wrong for the current task, **ask the user before bypassing it** —
do not silently override.
