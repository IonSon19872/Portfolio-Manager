#!/usr/bin/env python3
"""
intelligence.py -- Triggered 2x daily by GitHub Actions
========================================================
For every holding in the portfolio (stocks + ETFs):

  1. Analyst rating check (Finnhub /stock/upgrade-downgrade)
     - Fetches changes from the last 7 days. 
     - Compares against ratings_history.json (persisted in the repo).
     - If a GENUINELY NEW change is found today -> sends a SEPARATE
       highlighted email immediately (one per ticker with new changes).
     - Stores seen rating keys so we never double-alert.

  2. Company news (Finnhub /company-news)
     - Fetches news from the past 24 hours.
     - Stored in intelligence.json for the dashboard to display.
     - Note: Finnhub free tier has best news coverage for US-listed stocks;
       European stocks may have fewer articles.

Finnhub calls per stock: 2  (upgrade-downgrade + company-news)
With 1 s throttle: 20 stocks x 2 calls ~ 40 s total.
"""

import sys
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, save_json, load_json,
    INTEL_F, RATINGS_F,
    get_analyst_upgrades, get_company_news,
    append_alert, send_email,
    rating_change_html, news_digest_html, log
)


def load_seen() -> dict:
    """Load seen rating keys: {ticker: {key: True}}"""
    return load_json(RATINGS_F, {})


def save_seen(seen: dict):
    save_json(RATINGS_F, seen)


def rating_key(r: dict) -> str:
    """Unique key for a rating entry -- used to detect duplicates."""
    return f"{r['date']}|{r['firm']}|{r['from_grade']}|{r['to_grade']}"


def is_meaningful_change(r: dict) -> bool:
    """Only alert on actual grade changes, not reiterations of the same grade."""
    action = r.get("action", "").lower()
    fg     = (r.get("from_grade") or "").strip().lower()
    tg     = (r.get("to_grade")   or "").strip().lower()
    if action == "reit":
        return False                     # reiteration -- skip
    if fg and tg and fg == tg:
        return False                     # same grade -- skip
    return bool(tg)                      # must have a target grade


def check_ratings(ticker: str, finnhub_sym: str, name: str,
                  seen: dict, cfg: dict) -> list:
    """
    Fetch rating changes, compare with seen history.
    Sends email for new meaningful changes. Returns list of new changes.
    """
    api_key   = cfg["finnhub"]["api_key"]
    days_back = cfg["finnhub"].get("ratings_days_back", 7)
    today     = date.today().isoformat()

    all_ratings = get_analyst_upgrades(finnhub_sym, api_key, days_back)
    seen_t      = seen.get(ticker, {})
    new_changes = []

    for r in all_ratings:
        key = rating_key(r)
        # Only send alert for TODAY's ratings we haven't seen yet
        if r.get("date") == today and key not in seen_t and is_meaningful_change(r):
            new_changes.append(r)
            log.info(f"    [RATING] NEW: {r['firm']} {r.get('from_grade','?')} -> {r['to_grade']} ({r['action']})")
        seen_t[key] = True   # mark as seen regardless (avoid future re-alert)

    seen[ticker] = seen_t

    if new_changes:
        msg = " | ".join(
            f"{c['firm']}: {c.get('from_grade','?')}->{c['to_grade']}"
            for c in new_changes
        )
        append_alert("rating_change", ticker, msg)
        send_email(
            f"[RATING] Rating Change: {ticker} -- {new_changes[0]['firm']} -> {new_changes[0]['to_grade']}",
            rating_change_html(ticker, name, new_changes),
            cfg
        )

    # Return ALL recent ratings (not just today's) for the dashboard display
    return all_ratings


def main():
    log.info("======== Intelligence Check (Finnhub) ========")
    cfg     = load_config()
    api_key = cfg["finnhub"]["api_key"]

    if not api_key:
        log.error("FINNHUB_API_KEY not set. Add it as a GitHub Secret.")
        import sys; sys.exit(1)

    seen            = load_seen()
    all_holdings    = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
    news_days_back  = cfg["finnhub"].get("news_days_back", 1)
    max_news        = cfg["finnhub"].get("max_news_per_stock", 3)

    if not all_holdings:
        log.info("No holdings configured.")
        return

    log.info(f"Checking {len(all_holdings)} holdings...")

    intel_data = {
        "generated": datetime.utcnow().isoformat(),
        "holdings":  []
    }
    total_new_ratings = 0
    total_news        = 0

    for h in all_holdings:
        ticker = (h.get("ticker") or "").strip()
        name   = h.get("name", ticker)
        if not ticker:
            continue

        finnhub_sym = h.get("finnhub_symbol") or __import__("shared").to_finnhub_symbol(ticker)
        log.info(f"  -- {ticker}  ({finnhub_sym}) --")

        entry = {"ticker": ticker, "finnhub_symbol": finnhub_sym,
                 "name": name, "ratings": [], "new_ratings": [], "news": []}

        # -- Call 1: Analyst ratings ----------------------------------------
        log.info("    Ratings...")
        all_ratings = check_ratings(ticker, finnhub_sym, name, seen, cfg)
        entry["ratings"]     = all_ratings[:10]    # last 10 for display
        entry["new_ratings"] = [r for r in all_ratings
                                if r.get("date") == date.today().isoformat()
                                and is_meaningful_change(r)]
        total_new_ratings += len(entry["new_ratings"])

        # -- Call 2: Company news -------------------------------------------
        log.info("    News...")
        news = get_company_news(finnhub_sym, api_key, news_days_back, max_news)
        entry["news"] = news
        total_news   += len(news)
        log.info(f"    {len(news)} article(s)")

        intel_data["holdings"].append(entry)

    save_seen(seen)
    log.info(f"Seen-ratings saved -> {RATINGS_F}")

    save_json(INTEL_F, intel_data)
    log.info(f"Intelligence saved -> {INTEL_F}")

    # -- Send news digest email if any articles were found -----------------
    holdings_with_news = [h for h in intel_data["holdings"] if h.get("news")]
    if holdings_with_news:
        run_label = datetime.utcnow().strftime("%H:%M UTC")
        log.info(f"-- Sending news digest ({total_news} articles) --")
        send_email(
            f"[NEWS] News Digest -- {run_label}",
            news_digest_html(holdings_with_news, run_label),
            cfg
        )
        append_alert("news", "", f"News digest: {total_news} article(s) across {len(holdings_with_news)} holding(s)")
    else:
        log.info("  No news articles found -- skipping news email")

    summary = (f"Intel run complete: {len(all_holdings)} holdings, "
               f"{total_new_ratings} new rating change(s), "
               f"{total_news} news article(s)")
    append_alert("intel_run", "", summary)
    log.info(f"======== {summary} ========")


if __name__ == "__main__":
    main()
