#!/usr/bin/env python3
"""
saturday_summary.py - Triggered every Saturday at 10:00 CET

Sends weekly roundup email:
  - Portfolio total + week-over-week change
  - Top movers of the week
  - Analyst rating changes from past 5 days
  - News from past 5 days
  - Next week: earnings, dividends, splits
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, date

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, load_json, save_json,
    SNAPSHOT_F, INTEL_F, DATA_DIR,
    send_email, append_alert,
    saturday_summary_html, next_week_calendar_html,
    get_earnings_calendar, get_dividends, get_stock_splits,
    log
)

WEEK_OPEN_F = DATA_DIR / "week_open.json"


def next_weekday_range():
    today       = date.today()
    days_to_mon = (7 - today.weekday()) % 7 or 7
    next_mon    = today + timedelta(days=days_to_mon)
    next_fri    = next_mon + timedelta(days=4)
    return next_mon.isoformat(), next_fri.isoformat()


def fmt_date(d: str) -> str:
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
    except Exception:
        return d


def build_week_movements(snapshot: dict, week_open: dict) -> list:
    now_map = {
        s["ticker"]: s
        for s in snapshot.get("stocks", []) + snapshot.get("etfs", [])
        if "error" not in s and s.get("price_eur")
    }
    open_map = {
        s["ticker"]: s
        for s in week_open.get("stocks", []) + week_open.get("etfs", [])
        if "error" not in s and s.get("price_eur")
    }
    moves = []
    for ticker, now in now_map.items():
        opened = open_map.get(ticker)
        if not opened:
            continue
        p_now  = now["price_eur"]
        p_open = opened["price_eur"]
        if p_open == 0:
            continue
        moves.append({
            "ticker":   ticker,
            "name":     now.get("name", ticker),
            "from_eur": round(p_open, 2),
            "to_eur":   round(p_now,  2),
            "move_pct": round((p_now - p_open) / p_open * 100, 2),
        })
    return sorted(moves, key=lambda x: abs(x["move_pct"]), reverse=True)


def fetch_next_week_calendar(cfg: dict) -> dict:
    next_mon, next_fri = next_weekday_range()
    all_holdings       = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]

    earnings_all  = []
    dividends_all = []
    splits_all    = []

    log.info("  Fetching calendar for next week: " + next_mon + " -> " + next_fri)

    for h in all_holdings:
        ticker = (h.get("ticker") or "").strip()
        name   = h.get("name", ticker)
        if not ticker:
            continue

        log.info("    " + ticker)

        for e in get_earnings_calendar(ticker, from_date=next_mon, to_date=next_fri):
            earnings_all.append(dict(e, ticker=ticker, name=name))

        for d in get_dividends(ticker, from_date=next_mon, to_date=next_fri):
            dividends_all.append(dict(d, ticker=ticker, name=name))

        for s in get_stock_splits(ticker, from_date=next_mon, to_date=next_fri):
            splits_all.append(dict(s, ticker=ticker, name=name))

    earnings_all.sort(key=lambda x: x.get("date", ""))
    dividends_all.sort(key=lambda x: x.get("ex_date", ""))
    splits_all.sort(key=lambda x: x.get("date", ""))

    log.info(
        "  Calendar: " + str(len(earnings_all)) + " earnings, " +
        str(len(dividends_all)) + " dividends, " +
        str(len(splits_all)) + " splits"
    )
    return {
        "earnings":  earnings_all,
        "dividends": dividends_all,
        "splits":    splits_all,
        "next_mon":  next_mon,
        "next_fri":  next_fri,
    }


def main():
    log.info("=== Saturday Weekly Summary ===")
    cfg = load_config()

    snapshot   = load_json(SNAPSHOT_F, {"stocks": [], "etfs": [], "total_eur": 0})
    week_open  = load_json(WEEK_OPEN_F, {})
    intel_data = load_json(INTEL_F, {"holdings": []})

    if week_open:
        snapshot["week_start_eur"] = week_open.get("total_eur")
        log.info(
            "  Week: EUR " + "{:,.2f}".format(snapshot["week_start_eur"]) +
            "  ->  EUR " + "{:,.2f}".format(snapshot["total_eur"])
        )
    else:
        log.info("  No week_open.json - week change omitted")

    week_movements = build_week_movements(snapshot, week_open) if week_open else []
    log.info(str(len(week_movements)) + " movers computed")

    log.info("--- Fetching next-week calendar ---")
    calendar = fetch_next_week_calendar(cfg)
    next_mon = calendar["next_mon"]
    next_fri = calendar["next_fri"]

    log.info("--- Building and sending Saturday email ---")
    past_html = saturday_summary_html(snapshot, intel_data, week_movements)
    cal_html  = next_week_calendar_html(calendar, fmt_date(next_mon), fmt_date(next_fri))

    footer_marker = "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
    combined_html = past_html.replace(footer_marker, cal_html + footer_marker, 1)

    now_label = datetime.utcnow().strftime("%d %b %Y")
    sent = send_email(
        "Weekly Summary - " + now_label,
        combined_html,
        cfg
    )

    if sent:
        append_alert(
            "weekly_summary", "",
            "Weekly summary sent - " +
            str(len(calendar["earnings"])) + " earnings, " +
            str(len(calendar["dividends"])) + " dividends, " +
            str(len(calendar["splits"])) + " splits next week"
        )

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
