#!/usr/bin/env python3
"""
intelligence.py - Triggered 2x daily by GitHub Actions

For every holding in the portfolio (stocks + ETFs):
  1. Analyst rating changes (last 7 days)
  2. Company news (last 24 hours)
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
    return load_json(RATINGS_F, {})


def save_seen(seen: dict):
    save_json(RATINGS_F, seen)


def rating_key(r: dict) -> str:
    return (
        str(r.get("date", "")) + "|" +
        str(r.get("firm", "")) + "|" +
        str(r.get("from_grade", "")) + "|" +
        str(r.get("to_grade", ""))
    )


def is_meaningful_change(r: dict) -> bool:
    action = r.get("action", "").lower()
    fg     = (r.get("from_grade") or "").strip().lower()
    tg     = (r.get("to_grade")   or "").strip().lower()
    if action == "reit":
        return False
    if fg and tg and fg == tg:
        return False
    return bool(tg)


def check_ratings(ticker: str, name: str, seen: dict, cfg: dict) -> list:
    days_back = cfg.get("finnhub", {}).get("ratings_days_back", 7)
    today     = date.today().isoformat()

    all_ratings = get_analyst_upgrades(ticker, days_back=days_back)
    seen_t      = seen.get(ticker, {})
    new_changes = []

    for r in all_ratings:
        key = rating_key(r)
        if r.get("date") == today and key not in seen_t and is_meaningful_change(r):
            new_changes.append(r)
            log.info(
                "    NEW RATING: " + str(r.get("firm", "")) + " " +
                str(r.get("from_grade", "?")) + " -> " +
                str(r.get("to_grade", "")) + " (" + str(r.get("action", "")) + ")"
            )
        seen_t[key] = True

    seen[ticker] = seen_t

    if new_changes:
        msg = " | ".join(
            str(c.get("firm", "")) + ": " +
            str(c.get("from_grade", "?")) + "->" +
            str(c.get("to_grade", ""))
            for c in new_changes
        )
        append_alert("rating_change", ticker, msg)
        send_email(
            "Rating Change: " + ticker + " - " +
            str(new_changes[0].get("firm", "")) + " -> " +
            str(new_changes[0].get("to_grade", "")),
            rating_change_html(ticker, name, new_changes),
            cfg
        )

    return all_ratings


def main():
    log.info("=== Intelligence Check ===")
    cfg = load_config()

    seen         = load_seen()
    all_holdings = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
    news_days_back = cfg.get("finnhub", {}).get("news_days_back", 1)
    max_news       = cfg.get("finnhub", {}).get("max_news_per_stock", 3)

    if not all_holdings:
        log.info("No holdings configured.")
        return

    log.info("Checking " + str(len(all_holdings)) + " holdings...")

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

        log.info("  -- " + ticker + " --")

        entry = {
            "ticker":      ticker,
            "name":        name,
            "ratings":     [],
            "new_ratings": [],
            "news":        []
        }

        # Analyst ratings
        log.info("    Ratings...")
        all_ratings          = check_ratings(ticker, name, seen, cfg)
        entry["ratings"]     = all_ratings[:10]
        entry["new_ratings"] = [
            r for r in all_ratings
            if r.get("date") == date.today().isoformat()
            and is_meaningful_change(r)
        ]
        total_new_ratings += len(entry["new_ratings"])

        # News
        log.info("    News...")
        news          = get_company_news(ticker, days_back=news_days_back, max_articles=max_news)
        entry["news"] = news
        total_news   += len(news)
        log.info("    " + str(len(news)) + " article(s)")

        intel_data["holdings"].append(entry)

    save_seen(seen)
    log.info("Seen-ratings saved -> " + str(RATINGS_F))

    save_json(INTEL_F, intel_data)
    log.info("Intelligence saved -> " + str(INTEL_F))

    holdings_with_news = [h for h in intel_data["holdings"] if h.get("news")]
    if holdings_with_news:
        run_label = datetime.utcnow().strftime("%H:%M UTC")
        log.info("--- Sending news digest (" + str(total_news) + " articles) ---")
        send_email(
            "News Digest - " + run_label,
            news_digest_html(holdings_with_news, run_label),
            cfg
        )
        append_alert(
            "news", "",
            "News digest: " + str(total_news) + " article(s) across " +
            str(len(holdings_with_news)) + " holding(s)"
        )
    else:
        log.info("  No news articles found - skipping news email")

    summary = (
        "Intel run complete: " + str(len(all_holdings)) + " holdings, " +
        str(total_new_ratings) + " new rating change(s), " +
        str(total_news) + " news article(s)"
    )
    append_alert("intel_run", "", summary)
    log.info("=== " + summary + " ===")


if __name__ == "__main__":
    main()
