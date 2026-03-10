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
    INTEL_F,
    get_stock_data, get_analyst_upgrades, get_morningstar_data,
    append_alert, send_email,
    rating_change_html, log
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
    log.info("=== Intelligence run ===")
    cfg        = load_config()
    intel_data = load_json(INTEL_F, {"holdings": []})
    existing   = {h["ticker"]: h for h in intel_data.get("holdings", [])}
    today      = datetime.utcnow().strftime("%Y-%m-%d")
    ratings_days_back = cfg.get("finnhub", {}).get("ratings_days_back", 7)

    all_holdings = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
    updated = []

    for h in all_holdings:
        ticker = (h.get("ticker") or "").strip()
        name   = h.get("name", ticker)
        isin   = h.get("isin", "")
        if not ticker:
            continue

        entry = existing.get(ticker, {"ticker": ticker, "name": name, "ratings": [], "news": []})
        entry["name"] = name

        # Morningstar ratings
        if isin:
            log.info("  Morningstar: " + ticker)
            ms = get_morningstar_data(ticker, isin)
            if ms:
                entry["star_rating"]    = ms.get("star_rating")
                entry["analyst_rating"] = ms.get("analyst_rating")

        # Broker upgrades/downgrades (US only)
        log.info("  Broker ratings: " + ticker)
        new_ratings = get_analyst_upgrades(ticker, days_back=ratings_days_back)
        if new_ratings:
            seen_keys = {
                (r["date"], r["firm"], r["to_grade"])
                for r in entry.get("ratings", [])
            }
            truly_new = [
                r for r in new_ratings
                if (r["date"], r["firm"], r["to_grade"]) not in seen_keys
            ]
            entry["ratings"]     = (new_ratings + entry.get("ratings", []))[:50]
            entry["new_ratings"] = [r for r in truly_new if r["date"] == today]

            for r in entry["new_ratings"]:
                log.info(
                    "  NEW RATING: " + ticker + " " +
                    r.get("firm", "") + " " + r.get("to_grade", "")
                )
                send_email(
                    "[RATING] " + ticker + " " + r.get("action", "").upper() +
                    " -> " + r.get("to_grade", ""),
                    rating_change_html(ticker, name, [r]),
                    cfg
                )
        else:
            entry["new_ratings"] = []

        updated.append(entry)

    save_json(INTEL_F, {"holdings": updated, "updated": today})
    log.info("Intelligence saved -> " + str(INTEL_F))
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
