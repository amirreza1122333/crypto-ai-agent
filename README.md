# 🚀 Crypto AI Agent

An AI-powered cryptocurrency market analyzer and ranking system built with Python, FastAPI, and machine learning techniques.

---

## 🧠 Overview

This project analyzes real-time cryptocurrency market data and ranks coins based on custom scoring logic.
It helps identify high-potential assets by combining market metrics, data analysis, and lightweight machine learning.

---

## 🎯 Why This Project Matters

The crypto market is highly volatile and difficult to analyze manually.
This system automates market scanning and highlights coins with strong potential based on data-driven insights.

---

## ⚙️ Features

* 📊 Real-time crypto data collection (CoinGecko API)
* 🤖 AI-based scoring and ranking system
* 📈 Volume-to-market-cap analysis
* ⚠️ Risk classification (High / Medium / Low)
* 🔔 Telegram bot integration for alerts
* 🌐 FastAPI backend for structured access

---

## 🏗️ Architecture

```
User
 ↓
FastAPI (API Layer)
 ↓
Data Fetch (CoinGecko API)
 ↓
Data Processing (Pandas / NumPy)
 ↓
Scoring Engine (ML Logic)
 ↓
Output
 ├── API Response
 └── Telegram Bot Alerts
```

---

## 🧪 Scoring Logic

The ranking system evaluates coins using multiple factors:

* Volume-to-Market Cap ratio
* Price change trends (24h / 7d)
* Market cap ranking strength
* Normalized scoring across features

These features are combined into a final score used to rank and classify assets.

---

## 📊 Example Output

```
Top Ranked Coins:
1. BTC | Score: 0.91 | Risk: Low
2. SOL | Score: 0.84 | Risk: Medium
3. RNDR | Score: 0.79 | Risk: High
```

---

## 📁 Project Structure

```
crypto-ai-agent/
│
├── app/
│   ├── api.py              # FastAPI endpoints
│   ├── scoring.py          # AI scoring logic
│   ├── telegram_bot.py     # Telegram integration
│
├── data/                   # Data files
├── models/                 # Trained models
├── requirements.txt        # Dependencies
├── README.md
```

---

## ▶️ Setup & Installation

### 1. Clone the repository

```
git clone https://github.com/amirreza1122333/crypto-ai-agent.git
cd crypto-ai-agent
```

### 2. Install dependencies

```
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file:

```
TELEGRAM_BOT_TOKEN=your_token_here
API_BASE_URL=http://127.0.0.1:8000
```

---

## ▶️ Running the Project

### Start API:

```
uvicorn app.api:app --reload
```

### Run Telegram Bot:

```
python -m app.telegram_bot
```

---

## 📡 Example Usage

### API Endpoint:

```
GET /scan
```

Returns:

* Ranked coins
* Scores
* Risk classification

---

## 🚀 Future Improvements

* Backtesting engine
* Web dashboard (UI)
* Advanced ML models
* Cloud deployment
* Performance analytics

---

## 🧩 Skills Demonstrated

* Backend Development (FastAPI)
* Machine Learning Integration
* Data Analysis (Pandas / NumPy)
* API Design
* Real-time Data Processing
* Automation & Bot Development

---

## 📌 Notes

This project is built for educational and portfolio purposes.
Sensitive data such as API keys and tokens are excluded from the repository.

---
