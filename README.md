# 📊 Portfolio Intelligence Monitor

**Free 24/7 stock & ETF monitoring via GitHub Actions + Finnhub.**  
No server. No cost. Runs while your computer is off.

## Data source: Finnhub.io

All market data comes from [Finnhub](https://finnhub.io) — one free API key covers everything:

| Data | Finnhub endpoint | Used by |
|---|---|---|
| Real-time price + day change % | `/quote` | Price digest |
| Company name, sector, currency | `/stock/profile2` | Price digest |
| P/E, beta, 52w high/low, EPS | `/stock/metric` | Price digest |
| Analyst consensus (buy/hold/sell) | `/stock/recommendation` | Price digest |
| Analyst price target | `/stock/price-target` | Price digest |
| Analyst upgrades/downgrades | `/stock/upgrade-downgrade` | Intelligence |
| Company news headlines | `/company-news` | Intelligence |
| FX rates for EUR conversion | `/forex/rates` | Both |

**Free tier: 60 calls/minute.** The scripts sleep 1 second between every call.

---

## How it works

```
GitHub Actions (free minutes)
  price_digest.yml   → 07:45 · 12:00 · 15:15 · 17:00 CET (Mon-Fri)
    5 Finnhub calls per stock × 1 s gap = ~100 s for 20 stocks
    • Live EUR prices  • >3% movement alert  • Digest email
    → commits docs/data/snapshot.json

  intelligence.yml   → 07:30 · 17:00 CET (Mon-Fri)
    2 Finnhub calls per stock × 1 s gap = ~40 s for 20 stocks
    • Analyst rating changes → separate highlighted email
    • Company news headlines
    → commits docs/data/intelligence.json + alerts.json

GitHub Pages → docs/index.html (dashboard reads the JSON files)
```

---

## Setup

### 1. Get free Finnhub API key
Register at [finnhub.io/register](https://finnhub.io/register) — 30 seconds, no card needed.

### 2. Push to GitHub
```bash
git init && git add . && git commit -m "init"
git remote add origin https://github.com/YOU/YOUR_REPO.git
git push -u origin main
```

### 3. Edit portfolio_config.json
Replace sample stocks with your own. Each holding:
```json
{ "ticker": "ASML.AS", "name": "ASML Holding", "shares": 10, "finnhub_symbol": "AMS:ASML" }
```

US stocks don't need `finnhub_symbol` — just use the ticker directly (e.g. `AAPL`).

**EU exchange prefixes for Finnhub:**

| Exchange | Prefix | Example |
|---|---|---|
| Amsterdam | `AMS:` | `AMS:ASML` |
| Frankfurt/XETRA | `XETRA:` | `XETRA:SAP` |
| Paris | `EPA:` | `EPA:MC` |
| Milan | `MIL:` | `MIL:ENI` |
| London | `LON:` | `LON:SHEL` |
| Swiss | `SWX:` | `SWX:NESN` |
| Brussels | `EBR:` | `EBR:ABI` |
| Copenhagen | `CPH:` | `CPH:NOVO-B` |

If a stock shows "No price data", look up its exact symbol at finnhub.io and add it as `finnhub_symbol`.

### 4. Add GitHub Secrets
Repo → Settings → Secrets → Actions:

| Secret | Value |
|---|---|
| `FINNHUB_API_KEY` | Your Finnhub key |
| `EMAIL_FROM` | Gmail address |
| `EMAIL_PASSWORD` | Gmail App Password (Account → Security → 2FA → App Passwords) |
| `EMAIL_TO` | Where to receive alerts |

### 5. Enable GitHub Pages
Settings → Pages → Source: `main` branch, `/docs` folder.  
Dashboard: `https://YOU.github.io/YOUR_REPO/`

---

## Rating Change Alerts

New rating changes (upgrades/downgrades/initiations) found today trigger an **immediate separate email** with a purple header showing firm, old rating → new rating. Reiterations of the same grade are silently skipped. The seen-ratings history is committed to the repo so there are never duplicate alerts across runs.
